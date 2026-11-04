#!/usr/bin/env python3
"""
wsstunnel/relay.py — WebSocket 中继服务（VPS 端）

支持多后端（多容器）注册，支持 PTY 二进制帧透明转发。

角色注册机制:
  - "IAM_BACKEND:<token>:<name>:<pty_flag>" → 注册为后端（新格式）
  - "IAM_BACKEND:<token>:<name>"            → 注册为后端（兼容）
  - "IAM_BACKEND:<token>"                    → 注册为后端，自动命名
  - "AUTH:<token>"                           → 注册为前端
  - 不设 token 时保持向后兼容

前端命令路由:
  - USE <name>     → 切换当前后端（之后命令无需 @ 前缀）
  - USE            → 查看当前使用的后端
  - LIST           → 列举所有已连接的后端
  - @name <cmd>    → 临时发给指定后端（不影响当前选择）
  - <cmd>          → 发送给当前后端（USE 选中的 / 第一个注册的）

数据流:
  前端发送命令/二进制 → relay → 指定后端
  后端输出文本/二进制 → relay → 所有前端（文本输出含 [@name] 标签）
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import time
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import websockets

# ── 兼容 websockets 10.x 和 11.0+ 的 API 差异 ──
# 10.x: from websockets.http import Headers; from websockets.server import Response
# 11.0+: websockets.http 模块移除，Headers 移至 websockets.datastructures
#        websockets.server.Response 移除，Response 移至 websockets.http11
try:
    from websockets.http import Headers
    from websockets.server import Response
except ImportError:
    from websockets.datastructures import Headers
    from websockets.http11 import Response

from .security import (
    AuditLogger,
    BruteForceGuard,
    DenyList,
    IPAllowList,
    PermissionError_,
    Role,
    TokenManager,
    require_role,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  静态页面（web/index.html）
# ──────────────────────────────────────────────

_INDEX_HTML: bytes | None = None


def _load_index_html() -> bytes | None:
    """从已知路径加载嵌入式 Web 终端页面。"""
    candidates = [
        # pip 安装后：relay.py 同目录的 web/ 子目录
        os.path.join(os.path.dirname(__file__), "web", "index.html"),
        # 开发模式：项目根目录的 web/
        os.path.join(os.getcwd(), "web", "index.html"),
        # 源码目录（可编辑安装 -e .）
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "web", "index.html"),
    ]
    for path in candidates:
        try:
            with open(path, "rb") as f:
                data = f.read()
            logger.info(f"Loaded web terminal page: {path}")
            return data
        except FileNotFoundError:
            continue
    logger.warning("web/index.html not found — HTTP static serving disabled")
    return None


_INDEX_HTML = _load_index_html()

# 单帧上限（WebSocket 协议层 max_size）。与 client.py 的 _FILE_CHUNK_SIZE
# 存在隐式约束：64KB 分块 Base64 后约 87KB，加协议头仍远低于 1MB，当前安全；
# 若将来调大 _FILE_CHUNK_SIZE，需保证 b64(chunk) + 帧头 < _WS_MAX_FRAME，
# 否则单帧超限会被静默断开且报错不直观（P2-3）。
_WS_MAX_FRAME = 2 ** 20  # 1MB


# ──────────────────────────────────────────────
#  背景图代理（GET /api/bg?url=<urlencoded>&refresh=1）
# ──────────────────────────────────────────────
#
# 为什么需要服务端代理：Web 终端允许用户填写任意图片 URL 作为背景，
# 浏览器直接 <img src=远端URL> 时该图片既不能被页面 JS 读取、也不能可靠
# 写入 CacheStorage；而代理后同源、可缓存（这正是本端点的由来）。
#
# 安全前提：这是公网可达、无鉴权的 GET 端点（页面本身不做鉴权），因此
# 必须自己防 SSRF —— 攻击者可以让 relay 去请求云厂商元数据
# （169.254.169.254）、内网管理口、本机回环服务。
#
# 防护手段（及局限，务必如实告知后来者）：
#   1. 只允许 http/https，拒绝 file:/javascript:/ftp: 等 scheme；
#   2. 解析主机名，只要有一个解析结果落在私有/环回/链路本地/保留/组播
#      地址段就返回 403（每次重定向都重新校验，防 302 绕过）；
#   3. 不跟随无限重定向（最多 3 次）、限制总大小 8MB 与 8s 超时。
# 局限（TOCTOU / DNS rebinding）：校验时解析一次、httpx 真正连接时又解析
# 一次；恶意 DNS 可以在两次解析之间返回公网 IP 再换成 127.0.0.1。彻底
# 修法是"校验后把 IP 钉死再连"（需自行处理 Host/SNI），当前未做 —— 因为
# 该端点只返回图片给浏览器，不把响应体返回给攻击者，利用面有限。若要
# 加固，应在 _bg_download 里用解析到的 IP 直连并显式设置 Host 头。

# 总大小上限：背景图 8MB 已远超合理值，超出按 502 处理（不落盘，防磁盘打满）。
_BG_MAX_BYTES = 8 * 1024 * 1024
# 连接 + 读取超时（秒）。
_BG_TIMEOUT = 8.0
# 最多跟随的重定向次数（每次目标都重新走 SSRF 校验）。
_BG_MAX_REDIRECTS = 3
# 命中缓存也照样给浏览器一天缓存，减少往返。
_BG_CACHE_CONTROL = "public, max-age=86400"
_BG_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_BG_REASON = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    502: "Bad Gateway",
}
# ipaddress 的 is_private 覆盖 10/8、172.16/12、192.168/16、127/8、
# 169.254/16、fc00::/7、fe80::/10 等，但运营商级 NAT 段 100.64.0.0/10
# 在部分 Python 版本里不算 private，需单独判定。
_BG_EXTRA_BLOCKED_NETS = (ipaddress.ip_network("100.64.0.0/10"),)

# 198.18.0.0/15 是 RFC 2544 benchmark 保留段，现实中没有设备会用它；
# 但 Clash / Surge 等代理的 fake-IP 模式会把**所有**域名解析成该段地址
# （本机实测 picsum.photos → 198.18.4.174）。Python 的 is_private 把它
# 判成私网，若一并拒绝，开着代理的机器上背景图代理会完全不可用。
# 放行它不构成内网 SSRF：该段没有任何真实内网服务，连接实际由本地代理
# 软件接管；真正的内网段（10/8、127/8、169.254/16 等）仍然全部拦截。
_BG_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")


class _BgError(Exception):
    """背景图代理的内部错误：``status`` 直接映射为 HTTP 状态码。

    Args:
        status: 返回给浏览器的 HTTP 状态码（400/403/502）。
        message: 简短错误说明（会作为 text/plain 响应体）。
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _bg_ip_blocked(ip_str: str) -> bool:
    """判断 IP 字面量是否属于禁止访问的内网/保留地址段。

    用于 SSRF 防护：命中任一条件即拒绝（含 IPv4-mapped IPv6 形式，
    如 ``::ffff:127.0.0.1``）。

    Args:
        ip_str: IP 地址字符串（v4 或 v6）。

    Returns:
        ``True`` 表示应当拒绝。
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        # 不是合法 IP：调用方应把它当主机名去解析，这里按"可疑"处理。
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # 代理 fake-IP 段：放行（见 _BG_FAKE_IP_NET 处注释）
    if ip.version == 4 and ip in _BG_FAKE_IP_NET:
        return False
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    return any(ip in net for net in _BG_EXTRA_BLOCKED_NETS)


async def _bg_resolve_hosts(host: str) -> list[str]:
    """把主机名解析成 IP 列表；主机名本身就是 IP 字面量时直接返回。

    Args:
        host: 不含端口的主机名或 IP 字面量（IPv6 不带方括号）。

    Returns:
        IP 字符串列表（可能为空，由调用方判定）。
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [host]
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise _BgError(502, f"cannot resolve host: {host}") from e
    # 去重但保持顺序，便于日志排查。
    hosts: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in hosts:
            hosts.append(addr)
    return hosts


async def _bg_validate_url(url: str) -> str:
    """校验背景图目标 URL，合法则原样返回，否则抛 :class:`_BgError`。

    校验内容：scheme 仅 http/https、必须有主机名、端口合法，且解析出的
    所有 IP 都不能是内网/环回/链路本地/保留地址（403）。

    Args:
        url: 待校验的绝对 URL。

    Returns:
        规范化后（原样）的 URL。

    Raises:
        _BgError: 400 表示 URL 本身不合法；403 表示目标指向受保护地址；
            502 表示域名解析失败。
    """
    if not url:
        raise _BgError(400, "missing url")
    try:
        parsed = urlparse(url)
        port = parsed.port  # 端口非法（如 :abc）在这里抛 ValueError
    except ValueError as e:
        raise _BgError(400, "invalid url") from e
    if parsed.scheme not in ("http", "https"):
        raise _BgError(400, "only http/https allowed")
    host = parsed.hostname
    if not host:
        raise _BgError(400, "missing host")
    if port is not None and not (1 <= port <= 65535):
        raise _BgError(400, "invalid port")
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise _BgError(403, "private address blocked")
    hosts = await _bg_resolve_hosts(host)
    if not hosts:
        raise _BgError(502, "cannot resolve host")
    if any(_bg_ip_blocked(addr) for addr in hosts):
        raise _BgError(403, "private address blocked")
    return url


async def _bg_download(url: str) -> tuple[bytes, str]:
    """下载背景图，返回 ``(图片字节, MIME)``。

    仅接受 200 且 Content-Type 为 ``image/*`` 的响应；手工跟随最多
    :data:`_BG_MAX_REDIRECTS` 次重定向（每次目标都重新做 SSRF 校验）；
    流式累加并限制总大小，超限立即中止。

    Args:
        url: 已经过/待校验的图片 URL。

    Returns:
        ``(body, mime)`` 二元组。

    Raises:
        _BgError: 400/403（URL 不合法或被 SSRF 防护拒绝）、
            502（网络失败、非 200、非图片、空响应、超限、重定向过多）。
    """
    timeout = httpx.Timeout(_BG_TIMEOUT)
    current = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        # 初始请求 + 最多 3 次重定向 = 最多 4 次请求。
        for hop in range(_BG_MAX_REDIRECTS + 1):
            current = await _bg_validate_url(current)
            try:
                async with client.stream(
                    "GET",
                    current,
                    headers={
                        "User-Agent": "wsstunnel-bg-proxy/1.0",
                        "Accept": "image/*",
                    },
                ) as resp:
                    if resp.status_code in _BG_REDIRECT_STATUSES:
                        if hop >= _BG_MAX_REDIRECTS:
                            raise _BgError(502, "too many redirects")
                        location = resp.headers.get("location")
                        if not location:
                            raise _BgError(502, "redirect without location")
                        current = urljoin(current, location)
                        continue
                    if resp.status_code != 200:
                        raise _BgError(502, f"upstream HTTP {resp.status_code}")
                    ctype = (
                        resp.headers.get("content-type", "")
                        .split(";")[0]
                        .strip()
                        .lower()
                    )
                    if not ctype.startswith("image/"):
                        raise _BgError(502, "not an image")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > _BG_MAX_BYTES:
                            raise _BgError(502, "image too large")
                        chunks.append(chunk)
                    if not total:
                        raise _BgError(502, "empty response")
                    return b"".join(chunks), ctype
            except _BgError:
                raise
            except (httpx.HTTPError, OSError) as e:
                # 带上异常类型：部分 httpx 异常 str() 为空（如代理握手失败），
                # 只写 {e} 会得到 "download failed: " 这种无法排查的消息。
                raise _BgError(
                    502, f"download failed: {type(e).__name__}: {e}"
                ) from e
    raise _BgError(502, "too many redirects")


def _bg_cache_dir() -> str:
    """返回磁盘缓存目录：``WSSTUNNEL_BG_CACHE`` 优先，否则 ``~/.wsstunnel/bg-cache``。"""
    env_dir = os.environ.get("WSSTUNNEL_BG_CACHE", "").strip()
    if env_dir:
        return env_dir
    return os.path.join(os.path.expanduser("~"), ".wsstunnel", "bg-cache")


def _bg_cache_paths(url: str) -> tuple[str, str]:
    """返回 ``(图片本体路径, 元数据路径)``。

    文件名是 ``sha256(url)`` 的十六进制：同一 URL 稳定命中，不同 URL 不冲突，
    也不会有路径穿越/长文件名问题。元数据是同名 ``.json``。
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    base = os.path.join(_bg_cache_dir(), digest)
    return base, base + ".json"


def _bg_cache_read(url: str) -> tuple[bytes, str] | None:
    """读取缓存，任一环节不可信则返回 ``None``（等价于未命中）。

    元数据缺失/损坏、MIME 不是图片、本体为空或超限都按未命中处理，
    宁可重新下载也不把坏数据回给浏览器。
    """
    body_path, meta_path = _bg_cache_paths(url)
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        mime = str(meta.get("mime") or "")
        if not mime.startswith("image/"):
            return None
        with open(body_path, "rb") as f:
            body = f.read()
    except (OSError, ValueError):
        return None
    if not body or len(body) > _BG_MAX_BYTES:
        return None
    return body, mime


def _bg_cache_write(url: str, body: bytes, mime: str) -> None:
    """写缓存（图片本体 + ``.json`` 元数据），失败只告警、不影响响应。

    写临时文件再 ``os.replace``，避免并发请求读到写了一半的文件；任何
    ``OSError``（目录不可写、磁盘满等）都退化为"本次不缓存"。
    """
    body_path, meta_path = _bg_cache_paths(url)
    suffix = f".{os.getpid()}.{time.time_ns()}.tmp"
    tmp_body = body_path + suffix
    tmp_meta = meta_path + suffix
    try:
        os.makedirs(os.path.dirname(body_path), exist_ok=True)
        with open(tmp_body, "wb") as f:
            f.write(body)
        with open(tmp_meta, "w", encoding="utf-8") as f:
            json.dump({"url": url, "mime": mime, "ts": time.time()}, f)
        os.replace(tmp_body, body_path)
        os.replace(tmp_meta, meta_path)
    except OSError as e:
        logger.warning(f"bg cache write failed ({url}): {e}")
        for tmp in (tmp_body, tmp_meta):
            try:
                os.unlink(tmp)
            except OSError:
                pass


async def _bg_get_image(url: str, refresh: bool = False) -> tuple[bytes, str, bool]:
    """取背景图：优先磁盘缓存，未命中/强制刷新则下载并回写缓存。

    Args:
        url: 图片 URL。
        refresh: ``True`` 时跳过读缓存，强制重新下载并覆盖旧缓存。

    Returns:
        ``(body, mime, from_cache)`` 三元组。
    """
    if not refresh:
        cached = _bg_cache_read(url)
        if cached is not None:
            return cached[0], cached[1], True
    body, mime = await _bg_download(url)
    _bg_cache_write(url, body, mime)
    return body, mime, False


def _bg_make_response(
    status: int, headers: Headers, body: bytes, legacy: bool
) -> Response | tuple[int, Headers, bytes]:
    """按 websockets 代际组装响应：legacy 三元组 / asyncio Response。"""
    if legacy:
        # legacy 消费方执行 AbortHandshake(*resp)，要求三元组
        return (status, headers, body)
    return Response(status, _BG_REASON.get(status, "OK"), headers, body)


def _bg_error_response(
    status: int, message: str, legacy: bool
) -> Response | tuple[int, Headers, bytes]:
    """组装简短的 text/plain 错误响应（不缓存）。"""
    headers = Headers()
    headers["Content-Type"] = "text/plain; charset=utf-8"
    headers["Cache-Control"] = "no-store"
    body = (message + "\n").encode("utf-8")
    return _bg_make_response(status, headers, body, legacy)


async def _bg_http_response(
    path: str, legacy: bool
) -> Response | tuple[int, Headers, bytes]:
    """处理 ``GET /api/bg?url=<urlencoded>&refresh=1``。

    成功：200 + 图片 Content-Type + ``Cache-Control`` + ``X-Bg-Cache: hit|miss``；
    缺 url/非法 scheme → 400；内网目标 → 403；下载失败/非图片/超限 → 502。

    Args:
        path: 含 query string 的原始请求路径。
        legacy: 是否 legacy websockets（决定返回三元组还是 Response）。
    """
    query = parse_qs(path.split("?", 1)[1]) if "?" in path else {}
    url = (query.get("url") or [""])[0].strip()
    refresh_raw = (query.get("refresh") or [""])[0].strip().lower()
    refresh = refresh_raw in ("1", "true", "yes", "on")
    if not url:
        return _bg_error_response(400, "missing url", legacy)
    try:
        body, mime, from_cache = await _bg_get_image(url, refresh=refresh)
    except _BgError as e:
        logger.info(f"bg proxy {e.status} ({e.message}): {url}")
        return _bg_error_response(e.status, e.message, legacy)
    except Exception as e:  # 兜底：代理异常不应把 HTTP 层打崩
        logger.warning(f"bg proxy unexpected error: {e}")
        return _bg_error_response(502, "background image fetch failed", legacy)
    headers = Headers()
    headers["Content-Type"] = mime
    headers["Content-Length"] = str(len(body))
    headers["Cache-Control"] = _BG_CACHE_CONTROL
    headers["X-Bg-Cache"] = "hit" if from_cache else "miss"
    logger.info(f"bg proxy 200 ({'hit' if from_cache else 'miss'}): {url}")
    return _bg_make_response(200, headers, body, legacy)


async def _http_request_handler(
    connection: Any, request: Any
) -> Response | tuple[int, Headers, bytes] | None:
    """处理普通 HTTP 请求：Web 终端页面、背景图代理，其余交给 WebSocket 升级。

    兼容两代 websockets API（``process_request`` 的参数形状不同）：

    - legacy（websockets <=12 的默认 ``serve``）: ``(path: str, request_headers: Headers)``
    - asyncio（websockets >=11，13 起为默认）: ``(connection, request: Request)``

    返回 ``None`` 则继续 WebSocket 升级。
    """
    # 鸭子类型区分：Request 对象有 .path，legacy 传入的 Headers 没有
    path = getattr(request, "path", None)
    legacy = path is None
    if legacy:
        # legacy: 第一个参数是 path 字符串，第二个是握手 Headers
        path = connection
        request_headers = request
    else:
        request_headers = request.headers
    # 只拦截非 WebSocket 的 HTTP GET 请求
    if request_headers.get("Upgrade", "").lower() == "websocket":
        return None
    # path 可能包含 query string（如 /wstunnel?token=xxx），需去掉
    clean_path = path.split("?")[0]
    # 背景图代理：不依赖内嵌页面是否加载成功，故放在 _INDEX_HTML 判断之前
    if clean_path == "/api/bg":
        return await _bg_http_response(path, legacy)
    if _INDEX_HTML is None:
        return None
    if clean_path in ("/", "/index.html", "/wstunnel", "/wsstunnel"):
        headers = Headers()
        headers["Content-Type"] = "text/html; charset=utf-8"
        # 页面是内嵌在 relay 里分发的：必须禁止缓存。
        # 手机浏览器（尤其 iOS Safari / 微信 X5）会把 HTML 缓存到下一次
        # 冷启动，导致 relay 已升级、真机仍跑旧页面 —— 前端修复"永远不生效"。
        headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        headers["Pragma"] = "no-cache"
        headers["Expires"] = "0"
        if legacy:
            # legacy 消费方执行 AbortHandshake(*resp)，要求三元组
            return (200, headers, _INDEX_HTML)
        return Response(200, "OK", headers, _INDEX_HTML)
    return None


# 匹配 ANSI 转义序列：ESC [ ... 最终字节，以及 ESC ] ... BEL/ST
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]|\x1b\[[?][0-9;]*[a-zA-Z]")

# 后端名合法字符集：字母/数字/下划线开头，可含点、连字符。
# 名字会原样出现在 [Info] 文本与 Web 终端 DOM 中，必须限制字符集防注入。
_BACKEND_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")


def _maybe_unb64(s: str) -> str:
    """尝试 base64 解码（用于审计日志可读性），失败返回原文。"""
    try:
        return base64.b64decode(s).decode("utf-8", "replace")
    except Exception:
        return s


def _strip_ansi(data: bytes) -> str:
    """将 PTY 二进制输出转为干净文本，剥离 ANSI 转义序列。

    Args:
        data: PTY 原始输出（含 ANSI 转义码）。

    Returns:
        清理后的纯文本字符串。
    """
    text = data.decode("utf-8", errors="replace")
    return _ANSI_RE.sub("", text)


# ──────────────────────────────────────────────
#  微信推送通知器
# ──────────────────────────────────────────────

class _WxPushNotifier:
    """微信推送通知器：后端上线/下线时发送消息。"""

    def __init__(self, url: str, key: str) -> None:
        self.url = url
        self.key = key

    async def send(self, text: str) -> None:
        """异步发送推送，失败仅 warning，不影响主流程。"""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self.url,
                    json={"msgtype": "text", "content": text, "to_user": "@all"},
                    headers={"Authorization": f"Bearer {self.key}"},
                    timeout=10,
                )
                data = resp.json()
                if data.get("status") != "success":
                    logger.warning(f"WxPush response: {data}")
                else:
                    logger.info(f"WxPush sent: {text[:50]}")
        except Exception as e:
            logger.warning(f"WxPush failed: {e}")


# ──────────────────────────────────────────────
#  协议解析（纯函数，便于单元测试）
# ──────────────────────────────────────────────

def _parse_backend_auth(
    msg: str | bytes,
    token: str | None,
    **kwargs,
) -> tuple[str, str] | None:
    """解析后端认证消息，返回 (name, mode) 元组，认证失败返回 ``None``。

    支持格式:
      IAM_BACKEND:<token>:<name>:<pty_flag>  → (name, "pty"|"pipe")
      IAM_BACKEND:<token>:<name>             → (name, "pipe")  # 兼容旧客户端
      IAM_BACKEND:<token>                    → (auto, "pipe")
      IAM_BACKEND:<name>:<pty_flag>          → (name, "pty"|"pipe")  # 无 token
      IAM_BACKEND:<pty_flag>                 → (auto, "pty"|"pipe")  # 无 token
      IAM_BACKEND                            → (auto, "pipe")        # 最旧兼容

    Args:
        msg: 第一条 WebSocket 消息（文本或二进制）。
        token: 期望的认证令牌，``None`` 表示不校验。

    Returns:
        ``(name, mode)`` 二元组，或 ``None`` 表示不是合法的后端注册消息。
    """
    if isinstance(msg, bytes):
        return None

    msg = msg.strip()

    # ── 后端身份判定（2026-09-06 加固）──
    # 旧实现的两个洞：
    #   1. --token-file 多 token 模式下 self.token 为 None，前缀退化为
    #      "IAM_BACKEND"，任何人无 token 即可注册任意名字的后端，
    #      从而劫持/窃听前端的按键与会话；
    #   2. startswith 前缀匹配存在碰撞：token="abc" 时 "IAM_BACKEND:abcdef"
    #      也能通过（知道前缀即可冒名）。
    # 现在的规则：
    #   - 单 token 模式：前缀必须精确到 "IAM_BACKEND:<token>"，且后随
    #     空串或 ":"（消除前缀碰撞）；
    #   - 多 token 模式（token_manager 提供）：消息第一段必须是任一
    #     已注册 token（hmac 常数时间比较），否则视为非法注册。
    token_manager = kwargs.get("token_manager")
    token_enabled = bool(token) or (token_manager is not None and token_manager.enabled)

    if token:
        prefix = f"IAM_BACKEND:{token}"
        if not msg.startswith(prefix):
            return None
        rest = msg[len(prefix):]
        # 精确边界：后面必须是空或 ":"，防止 "IAM_BACKEND:abc" 吃掉 "abcdef"
        if rest and not rest.startswith(":"):
            return None
    else:
        if not msg.startswith("IAM_BACKEND"):
            return None
        rest = msg[len("IAM_BACKEND"):]

    parts = [p.strip() for p in rest.split(":") if p.strip()] if rest else []

    # 多 token 模式：第一段必须是已注册 token，验证通过后剥离
    if not token and token_manager is not None and token_manager.enabled:
        if not parts:
            return None
        candidate = parts[0]
        if token_manager.validate(candidate) is None:
            return None
        parts = parts[1:]

    mode = "pipe"
    if parts and parts[-1] in ("pty", "pipe"):
        mode = parts.pop()

    name = parts[0] if parts else None
    return (name, mode)  # name 可能为 None，由调用方自动命名


def _is_frontend_auth(msg: str | bytes, token: str | None) -> bool:
    """检查消息是否为合法的前端认证。

    Args:
        msg: 第一条 WebSocket 消息。
        token: 期望的认证令牌。

    Returns:
        ``True`` 表示认证通过。
    """
    if isinstance(msg, bytes):
        return False
    msg = msg.strip()
    if token:
        return msg == f"AUTH:{token}"
    return True


# ──────────────────────────────────────────────
#  前端广播辅助函数
# ──────────────────────────────────────────────

async def _gather_send(frontends: set[Any], send_fn: Any) -> None:
    """并发执行 ``send_fn(f)``，发送失败的连接从集合中清除。

    - 迭代 ``list(frontends)`` 快照：避免遍历期间集合被并发注册/注销
      修改触发 ``RuntimeError: Set changed size during iteration``。
    - ``asyncio.gather`` 并发发送：避免慢前端 TCP 背压阻塞其他前端（队头阻塞）。
    """
    targets = list(frontends)
    if not targets:
        return
    dead: set[Any] = set()

    async def _send_one(f: Any) -> None:
        try:
            await send_fn(f)
        except Exception:
            dead.add(f)

    await asyncio.gather(*(_send_one(f) for f in targets))
    frontends -= dead


async def _forward_to_frontends(
    frontends: set[Any],
    message: str,
    tag: str | None = None,
) -> None:
    """转发文本消息给所有前端，可选加 ``[@tag]`` 标签。

    自动清理已断开的前端连接。

    Args:
        frontends: 前端 WebSocket 连接集合（会被原地修改）。
        message: 要转发的文本消息。
        tag: 可选标签，多后端时区分来源。
    """
    payload = f"[@{tag}] {message}" if tag else message
    await _gather_send(frontends, lambda f: f.send(payload))


async def _forward_to_frontends_untagged(
    frontends: set[Any],
    message: str,
) -> None:
    """转发文本消息给所有前端，**不加** ``[@tag]`` 前缀。

    用于文件传输数据块等场景，避免标签乱掉协议格式。
    自动清理已断开的前端连接。
    """
    await _gather_send(frontends, lambda f: f.send(message))


async def _forward_binary_to_frontends(
    frontends: set[Any],
    data: bytes,
    frontend_text_modes: dict[Any, bool],
    tag: str | None = None,
    only_for: Any = None,
) -> None:
    """转发二进制帧给所有前端。

    文本模式前端会收到剥离 ANSI 转义码的文本帧，其余前端收到原始二进制帧。
    ANSI 清洗只做一次，所有文本模式前端共享结果。
    自动清理已断开的前端连接。

    Args:
        frontends: 前端 WebSocket 连接集合（会被原地修改）。
        data: 二进制 PTY 输出数据。
        frontend_text_modes: 前端连接 → 是否文本模式。
        tag: 保留参数，当前未使用。
    """
    text = _strip_ansi(data)  # 清洗一次，避免对每个前端重复计算

    async def _send(f: Any) -> None:
        if only_for is not None and not only_for(f):
            return
        if frontend_text_modes.get(f):
            if text:
                await f.send(text)
        else:
            await f.send(data)

    await _gather_send(frontends, _send)


async def _send_backend_list(
    ws: Any,
    backends: dict[str, Any],
    backend_modes: dict[str, str] | None = None,
    current: str | None = None,
    backend_connected_at: dict[str, float] | None = None,
) -> None:
    """向前端发送当前后端列表。

    Args:
        ws: 目标前端 WebSocket。
        backends: 后端名称→连接映射。
        backend_modes: 后端名称→模式映射。
        current: 当前选中的后端名称。
        backend_connected_at: 后端名称→连接时间戳。
    """
    if not backends:
        await ws.send("[Info] No backends connected")
    else:
        now = time.time()
        names: list[str] = []
        for n in backends:
            mode = (backend_modes or {}).get(n, "pipe")
            marker = " *" if n == current else ""
            uptime = ""
            if backend_connected_at and n in backend_connected_at:
                elapsed = int(now - backend_connected_at[n])
                if elapsed < 60:
                    uptime = f" ↑{elapsed}s"
                elif elapsed < 3600:
                    uptime = f" ↑{elapsed // 60}m"
                else:
                    uptime = f" ↑{elapsed // 3600}h{(elapsed % 3600) // 60}m"
            names.append(f"{n}({mode}){uptime}{marker}")
        lines = [f"[Info] Connected backends: {', '.join(names)}"]
        if current:
            lines.append(f"[Info] Current: {current}")
        await ws.send("\n".join(lines))


async def _broadcast_backend_list(
    frontends: set[Any],
    backends: dict[str, Any],
    backend_modes: dict[str, str],
    frontend_targets: dict[Any, str | None],
    backend_connected_at: dict[str, float] | None = None,
) -> None:
    """广播后端列表给所有前端。

    自动清理已断开的前端连接。

    Args:
        frontends: 前端 WebSocket 连接集合（会被原地修改）。
        backends: 后端名称→连接映射。
        backend_modes: 后端名称→模式映射。
        frontend_targets: 前端→当前后端名称映射。
    """

    async def _send(f: Any) -> None:
        current = frontend_targets.get(f)
        await _send_backend_list(f, backends, backend_modes, current, backend_connected_at)

    await _gather_send(frontends, _send)


# ──────────────────────────────────────────────
#  RelayState — 中继服务核心状态机
# ──────────────────────────────────────────────

class RelayState:
    """管理中继服务的所有连接状态和消息路由。

    Attributes:
        backends: 后端名称 → WebSocket 连接。
        backend_modes: 后端名称 → ``"pty"`` | ``"pipe"``。
        frontends: 所有前端 WebSocket 连接。
        frontend_targets: 前端连接 → 当前选中的后端名称（``None`` = 自动）。
        frontend_text_modes: 前端连接 → 是否启用文本模式（剥离 ANSI）。
    """

    def __init__(
        self,
        token: str | None,
        notifier: _WxPushNotifier | None = None,
    ) -> None:
        self.token = token
        self.notifier = notifier
        self.backends: dict[str, Any] = {}
        self.backend_modes: dict[str, str] = {}
        self.backend_connected_at: dict[str, float] = {}
        self.frontends: set[Any] = set()
        self.frontend_targets: dict[Any, str | None] = {}
        self.frontend_text_modes: dict[Any, bool] = {}
        self._counter: int = 0
        # 安全组件
        self.token_manager = TokenManager(token)
        self.audit = AuditLogger()
        self.ip_allowlist = IPAllowList()
        self.brute_force = BruteForceGuard()
        self.deny_list = DenyList()
        self._client_info: dict[Any, dict] = {}
        # 后台微信推送任务（保持引用防 GC，完成后自动移除）
        self._notify_tasks: set[Any] = set()
        # 连接限制
        self._max_frontends = 100
        self._max_per_ip: dict[str, int] = {}
        self._max_connections_per_ip = 10
        self._max_file_size = 500 * 2 ** 20  # 500MB
        # 后端心跳观测：name → 最近一次 __PING__ 时间（超时 watchdog 用）
        self._backend_last_seen: dict[str, float] = {}
        self._backend_timeout = 90.0     # 3 个心跳周期(30s)未 ping 判定半开
        self._watch_interval = 15.0      # watchdog 扫描周期
        self._heartbeat_watch_task: Any = None

    def _next_backend_name(self) -> str:
        """生成自动后端名称 ``backend-N``。"""
        self._counter += 1
        return f"backend-{self._counter}"

    def _notify(self, text: str) -> None:
        """后台推送微信通知，不阻塞注册/注销主流程。"""
        if self.notifier is None:
            return
        try:
            task = asyncio.create_task(self.notifier.send(text))
        except RuntimeError:
            return  # 无运行中的事件循环（测试环境），跳过
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    def _release_ip_slot(self, ip: str) -> None:
        """释放 IP 连接计数；归零后移除条目，避免长期运行内存增长。"""
        remaining = self._max_per_ip.get(ip, 0) - 1
        if remaining > 0:
            self._max_per_ip[ip] = remaining
        else:
            self._max_per_ip.pop(ip, None)

    def _resolve_target(self, ws: Any) -> tuple[str, Any] | None:
        """解析前端当前的目标后端。

        Args:
            ws: 前端 WebSocket 连接。

        Returns:
            ``(name, ws_backend)`` 二元组，或 ``None`` 表示无可用后端。
        """
        target_name = self.frontend_targets.get(ws)
        if target_name and target_name in self.backends:
            return (target_name, self.backends[target_name])
        # auto: 使用第一个注册的后端
        if self.backends:
            return next(iter(self.backends.items()))
        return None

    def _extract_url_token(self, websocket: Any) -> str | None:
        """从 WebSocket 连接 URL 中提取 token 参数。"""
        try:
            path = "/"
            if hasattr(websocket, "request"):
                path = websocket.request.path
            elif hasattr(websocket, "path"):
                path = websocket.path
            parsed = urlparse(path)
            params = parse_qs(parsed.query)
            tokens = params.get("token", [])
            return tokens[0] if tokens else None
        except Exception:
            return None

    # ── 后端注册/注销 ──

    async def _register_backend(
        self, ws: Any, name: str | None, mode: str, peer_ip: str = "0.0.0.0"
    ) -> str:
        """注册后端连接，返回实际使用的名称。

        同时写入 ``_client_info``（与前端注册对称），否则后端断开时
        finally 的 IP 连接计数释放路径找不到条目——计数只增不减，
        直连 relay 的后端重连 10 次后会被自己的配额锁死（P0）。
        """
        if not name:
            name = self._next_backend_name()
        self.backends[name] = ws
        self.backend_modes[name] = mode
        self.backend_connected_at[name] = time.time()
        self._backend_last_seen[name] = time.time()
        self._client_info[ws] = {
            "id": name,
            "ip": peer_ip,
            "role": "backend",
            "connected_at": time.time(),
        }
        logger.info(
            f"Backend registered: '{name}' mode={mode} ip={peer_ip} "
            f"(total {len(self.backends)})"
        )
        # 微信推送：后端上线（后台发送，不阻塞注册与列表广播）
        self._notify(f"✅ wsstunnel: {name} 已上线 ({mode})")
        # 通知所有前端后端列表变化
        await _broadcast_backend_list(
            self.frontends, self.backends, self.backend_modes,
            self.frontend_targets, self.backend_connected_at,
        )
        return name

    async def _unregister_backend(self, name: str, ws: Any = None) -> None:
        """注销后端连接并清理关联状态。

        Args:
            name: 后端名称。
            ws: 发起注销的连接。传入时做身份校验——若该名称已被新连接
                顶替（重连场景），则跳过注销，避免误删新后端的注册。
        """
        if ws is not None and self.backends.get(name) is not ws:
            logger.debug(f"Backend '{name}' already replaced, skip unregister")
            return
        # 下线前先算运行时长
        elapsed = ""
        conn_time = self.backend_connected_at.get(name)
        if conn_time:
            secs = int(time.time() - conn_time)
            if secs < 60:
                elapsed = f"（运行 {secs}s）"
            elif secs < 3600:
                elapsed = f"（运行 {secs // 60}m）"
            else:
                h, m = secs // 3600, (secs % 3600) // 60
                elapsed = f"（运行 {h}h{m}m）"
        self.backends.pop(name, None)
        self.backend_modes.pop(name, None)
        self.backend_connected_at.pop(name, None)
        self._backend_last_seen.pop(name, None)
        # 释放该后端连接占用的 IP 计数（注册时写入 _client_info），
        # 正常路径在此释放；异常路径由 handler finally 兜底，二者不重复
        info = self._client_info.pop(ws, None) if ws is not None else None
        if info:
            ip = info.get("ip", "")
            if ip:
                self._release_ip_slot(ip)
        # 如果断开的后端正好是某个前端的当前目标，清除它
        for f in list(self.frontend_targets):
            if self.frontend_targets.get(f) == name:
                self.frontend_targets[f] = None
        logger.info(f"Backend disconnected: '{name}' (total {len(self.backends)})")
        self._notify(f"❌ wsstunnel: {name} 已下线{elapsed}")
        await _broadcast_backend_list(
            self.frontends, self.backends, self.backend_modes,
            self.frontend_targets, self.backend_connected_at,
        )

    # ── 前端注册/注销 ──

    async def _register_frontend(
        self, ws: Any, token_info: Any = None, peer_ip: str = "0.0.0.0"
    ) -> None:
        """注册前端连接并进入消息循环。"""
        client_id = getattr(token_info, "id", "unknown") if token_info else "unknown"
        role = getattr(token_info, "role", Role.ADMIN) if token_info else Role.ADMIN
        self._client_info[ws] = {
            "id": client_id,
            "ip": peer_ip,
            "role": role,
            "connected_at": time.time(),
        }
        self.frontends.add(ws)
        self.frontend_targets[ws] = None
        logger.info(
            f"Frontend authenticated: id={client_id} role={role} ip={peer_ip} "
            f"(total {len(self.frontends)})"
        )
        self.audit.connect(client_id, peer_ip, role, client_id)
        await _send_backend_list(
            ws, self.backends, self.backend_modes, None,
            self.backend_connected_at,
        )
        try:
            async for message in ws:
                await self._handle_frontend_msg(ws, message)
        except websockets.exceptions.ConnectionClosed:
            pass
        self._unregister_frontend(ws)

    def _unregister_frontend(self, ws: Any) -> None:
        """注销前端连接。"""
        info = self._client_info.pop(ws, {})
        if info:
            self.audit.disconnect(
                info.get("id", "?"), info.get("ip", "?"),
                time.time() - info.get("connected_at", time.time()),
            )
            # 释放 IP 连接计数
            ip = info.get("ip", "")
            if ip:
                self._release_ip_slot(ip)
        self.frontends.discard(ws)
        self.frontend_targets.pop(ws, None)
        self.frontend_text_modes.pop(ws, None)
        logger.info(f"Frontend disconnected (total {len(self.frontends)})")

    # ── 前端消息路由 ──

    async def _handle_frontend_msg(self, ws: Any, message: str | bytes) -> None:
        """处理前端发出的单条消息（支持文本和二进制）。

        根据消息内容路由到对应的子处理方法。
        """
        info = self._client_info.get(ws, {})
        role: Role = info.get("role", Role.READONLY)

        # 二进制帧：原始按键输入 → 需要 ADMIN
        if isinstance(message, bytes):
            if role < Role.ADMIN:
                self.audit.permission_denied(
                    info.get("id", "?"), info.get("ip", "?"), "binary_input", role
                )
                return
            await self._forward_binary_to_backend(ws, message)
            return

        msg = message.strip()

        # __PING__: 前端保活（CLI/Web 心跳防反代空闲超时切断），就地应答，
        # 不转发给后端（否则会作为命令注入 shell）。所有角色可用。
        if msg == "__PING__":
            # 保活应答用 ping 控制帧而非文本 "__PONG__"：文本帧会被
            # Web 终端/CLI 原样打印到用户屏幕（2026-09-06 修复的泄漏），
            # ping 控制帧同样在反代上产生往返流量，防空闲切断
            await ws.ping()
            return

        # LIST / USE: 所有角色可用
        upper = msg.upper()
        if upper == "LIST":
            await self._handle_list(ws)
            return
        if upper == "USE" or upper.startswith("USE "):
            await self._handle_use(ws, msg)
            return

        # __RESIZE / __SIGNAL / __TEXT / __RAW: 需要 ADMIN
        if msg.startswith("__RESIZE:") or msg.startswith("__SIGNAL:"):
            if role < Role.ADMIN:
                self.audit.permission_denied(
                    info.get("id", "?"), info.get("ip", "?"), "control", role
                )
                return
            await self._handle_control(ws, msg)
            return
        if msg in ("__TEXT", "__RAW"):
            self.frontend_text_modes[ws] = msg == "__TEXT"
            await ws.send(
                f"[Info] {'Text' if msg == '__TEXT' else 'Raw'} mode enabled"
            )
            return

        # 文件传输协议: 需要至少 FILE 角色，检查通过后**显式转发给后端**。
        # （此前依赖 fall-through 碰巧让 ADMIN 走到普通命令分支转发，
        #   而 FILE 角色反而被末尾的 ADMIN 检查拦截——专属角色形同虚设。）
        if msg.startswith("__FILE_BEGIN:"):
            parts = msg.split(":", 2)
            file_size: int | None = None
            if len(parts) == 3:
                try:
                    file_size = int(parts[2])
                except ValueError:
                    file_size = None
            if file_size is not None and file_size > self._max_file_size:
                await ws.send(
                    f"__FILE_ERROR::File too large "
                    f"({file_size} > {self._max_file_size} bytes)"
                )
                self.audit.permission_denied(
                    info.get("id", "?"), info.get("ip", "?"),
                    "file_too_large", role,
                )
                return
            if role < Role.FILE:
                self.audit.permission_denied(
                    info.get("id", "?"), info.get("ip", "?"), "file_upload", role
                )
                await ws.send("__FILE_ERROR::Permission denied: file operations require file role")
                return
            # 只在 BEGIN 记审计，CHUNK 不记（否则大文件每次传输产生上千条日志）
            self.audit.file_upload(
                info.get("id", "?"),
                _maybe_unb64(parts[1]) if len(parts) > 1 else "?",
                file_size or 0, role,
            )
            await self._send_to_current_backend(ws, msg)
            return

        if msg.startswith("__FILE_CHUNK:"):
            if role < Role.FILE:
                self.audit.permission_denied(
                    info.get("id", "?"), info.get("ip", "?"), "file_upload", role
                )
                await ws.send("__FILE_ERROR::Permission denied: file operations require file role")
                return
            await self._send_to_current_backend(ws, msg)
            return

        if msg.startswith("__FILE_DOWNLOAD:"):
            if role < Role.FILE:
                self.audit.permission_denied(
                    info.get("id", "?"), info.get("ip", "?"), "file_download", role
                )
                return
            path_b64 = msg.split(":", 1)[1] if ":" in msg else "?"
            self.audit.file_download(
                info.get("id", "?"), _maybe_unb64(path_b64), role
            )
            await self._send_to_current_backend(ws, msg)
            return

        # @name <cmd>: 需要 ADMIN
        if msg.startswith("@"):
            if role < Role.ADMIN:
                self.audit.permission_denied(
                    info.get("id", "?"), info.get("ip", "?"), "command", role
                )
                return
            # 命令黑名单对 @name 路由的命令同样生效
            space = msg.find(" ")
            target_cmd = msg[space + 1:] if space != -1 else ""
            if self.deny_list.is_denied(target_cmd):
                self.audit.permission_denied(
                    info.get("id", "?"), info.get("ip", "?"), "denied_command", role
                )
                await ws.send("[Error] Command blocked by deny policy")
                return
            await self._handle_at_cmd(ws, msg)
            return

        # 普通命令: 需要 ADMIN，记录审计
        if role < Role.ADMIN:
            self.audit.permission_denied(
                info.get("id", "?"), info.get("ip", "?"), "command", role
            )
            return
        # ── 命令黑名单（--deny-cmd）──
        if self.deny_list.is_denied(msg):
            self.audit.permission_denied(
                info.get("id", "?"), info.get("ip", "?"), "denied_command", role
            )
            await ws.send("[Error] Command blocked by deny policy")
            return
        self.audit.command(info.get("id", "?"), msg[:100], role)
        await self._send_to_current_backend(ws, msg)

    async def _handle_list(self, ws: Any) -> None:
        """处理 LIST 命令：列举所有后端。"""
        current = self.frontend_targets.get(ws)
        await _send_backend_list(ws, self.backends, self.backend_modes, current, self.backend_connected_at)

    async def _handle_use(self, ws: Any, msg: str) -> None:
        """处理 USE [name] 命令：切换或查看当前后端。"""
        parts = msg.split(None, 1)
        if len(parts) == 1:
            # USE（无参数）: 显示当前目标
            current = self.frontend_targets.get(ws)
            if current:
                await ws.send(f"[Info] Current backend: {current}")
            else:
                target = self._resolve_target(ws)
                if target:
                    await ws.send(f"[Info] Current backend: {target[0]} (auto)")
                else:
                    await ws.send("[Info] No backends connected")
        else:
            name = parts[1].strip()
            if name in self.backends:
                self.frontend_targets[ws] = name
                mode = self.backend_modes.get(name, "pipe")
                await ws.send(f"[Info] Switched to backend: {name}({mode})")
                logger.info(f"Frontend switched to backend '{name}'")
            else:
                await ws.send(
                    f"[Error] Backend '{name}' not found. "
                    f"Use LIST to see available backends."
                )

    async def _handle_at_cmd(self, ws: Any, msg: str) -> None:
        """处理 ``@name <cmd>`` 命令：临时发给指定后端。"""
        space = msg.find(" ")
        if space == -1:
            await ws.send("[Error] Usage: @backend_name <command>")
            return
        name = msg[1:space]
        cmd = msg[space + 1:]
        if name in self.backends:
            try:
                await self.backends[name].send(cmd)
            except Exception:
                await ws.send(f"[Error] Backend '{name}' disconnected")
                self.backends.pop(name, None)
        else:
            await ws.send(
                f"[Error] Backend '{name}' not found. "
                f"Use LIST to see available backends."
            )

    async def _handle_control(self, ws: Any, msg: str) -> None:
        """处理 ``__RESIZE:rows,cols`` 和 ``__SIGNAL:SIGXXX`` 控制命令。"""
        target = self._resolve_target(ws)
        if target:
            name, ws_backend = target
            try:
                await ws_backend.send(msg)
            except Exception:
                await ws.send(f"[Error] Backend '{name}' disconnected")
                self.backends.pop(name, None)

    async def _send_to_current_backend(self, ws: Any, msg: str) -> None:
        """发送普通文本命令给当前选中的后端。"""
        target = self._resolve_target(ws)
        if target:
            name, ws_backend = target
            try:
                await ws_backend.send(msg)
            except Exception:
                await ws.send(f"[Error] Backend '{name}' disconnected")
                self.backends.pop(name, None)
        else:
            await ws.send("[Error] No backends connected. Use LIST to check.")

    async def _forward_binary_to_backend(self, ws: Any, data: bytes) -> None:
        """转发二进制帧（原始按键输入）给当前后端。"""
        target = self._resolve_target(ws)
        if target:
            name, ws_backend = target
            try:
                await ws_backend.send(data)
            except Exception:
                await ws.send(f"[Error] Backend '{name}' disconnected")
                self.backends.pop(name, None)
        else:
            await ws.send("[Error] No backends connected.")

    # ── 后端心跳 watchdog ──

    async def heartbeat_watchdog(self) -> None:
        """定期扫描后端最近心跳时间，超时主动关闭半开连接。

        协议层 ping 已关闭（ping_interval=None），relay 对后端心跳只被动
        应答：若后端心跳线程静默死亡（不抛异常、不置位重连事件），这条
        半开连接会一直挂在 ``LIST`` 里显示在线，实际发命令无响应，直到
        TCP 层超时（小时级）。超过 ``_backend_timeout``（90s，3 个心跳
        周期）未收到 ``__PING__`` 即主动 close，触发正常下线流程。
        """
        while True:
            await asyncio.sleep(self._watch_interval)
            now = time.time()
            for name, last in list(self._backend_last_seen.items()):
                if now - last <= self._backend_timeout:
                    continue
                self._backend_last_seen.pop(name, None)
                ws = self.backends.get(name)
                if ws is None:
                    continue
                logger.warning(
                    f"Backend '{name}' heartbeat timeout "
                    f"(no ping for {int(now - last)}s), closing"
                )
                try:
                    await ws.close(1000, "heartbeat timeout")
                except Exception:
                    pass

    # ── 主 handler 入口 ──

    async def handler(self, websocket: Any, _path: object = None) -> None:
        """WebSocket 连接处理入口。

        由 ``websockets.serve()`` 调用，负责：
        1. URL token 自动认证（前端）
        2. 第一条消息角色检测（后端/前端）
        3. 后端消息转发循环
        4. 异常和连接清理
        """
        # ── IP 白名单检查 ──
        peer_ip = "0.0.0.0"
        try:
            peer_ip, _ = websocket.remote_address
        except Exception:
            pass
        if not self.ip_allowlist.allow(peer_ip):
            logger.info(f"Security: rejected connection from {peer_ip} (not in allowlist)")
            await websocket.close(1008, "IP not allowed")
            return
        if self.brute_force.is_locked(peer_ip):
            logger.info(f"Security: rejected connection from {peer_ip} (rate limited)")
            await websocket.close(1008, "Too many attempts, try later")
            return
        # ── 连接数限制（127.0.0.1/::1 豁免）──
        if peer_ip not in ("127.0.0.1", "::1"):
            if len(self.frontends) >= self._max_frontends:
                logger.info(f"Security: rejected connection (max frontends reached)")
                await websocket.close(1008, "Server full")
                return
            ip_count = self._max_per_ip.get(peer_ip, 0)
            if ip_count >= self._max_connections_per_ip:
                logger.info(f"Security: rejected connection from {peer_ip} (too many connections)")
                await websocket.close(1008, "Too many connections from your IP")
                return
            self._max_per_ip[peer_ip] = ip_count + 1

        # ── URL token 自动认证 ──
        url_token = self._extract_url_token(websocket)
        if url_token:
            token_info = self.token_manager.validate(url_token)
            if token_info:
                self.brute_force.record_success(peer_ip)
                await websocket.send("AUTH_OK")
                await self._register_frontend(websocket, token_info, peer_ip)
                return

        try:
            first = await asyncio.wait_for(websocket.recv(), timeout=30)

            # ── 后端注册 ──
            backend_info = _parse_backend_auth(first, self.token, token_manager=self.token_manager)
            if backend_info:
                name, mode = backend_info
                # 后端名字符集校验：名字会进入 [Info] 文本与 Web 终端 DOM，
                # 非法名字自动降级为自动命名，防注入（Web 端另有转义兜底）
                if name is not None and not _BACKEND_NAME_RE.fullmatch(name):
                    logger.warning(
                        f"Security: invalid backend name rejected, auto-naming: {name[:32]!r}"
                    )
                    name = None
                if name is None:
                    name = self._next_backend_name()
                if name in self.backends:
                    logger.info(f"Backend '{name}' reconnecting, evicting old session")
                    old_ws = self.backends[name]
                    try:
                        await old_ws.close(1000, "Replaced by new session")
                    except Exception:
                        pass
                    self.backends.pop(name, None)
                    self.backend_modes.pop(name, None)
                    self.backend_connected_at.pop(name, None)
                actual_name = await self._register_backend(websocket, name, mode, peer_ip)
                try:
                    async for message in websocket:
                        # ── 心跳（仅文本） ──
                        if isinstance(message, str) and message == "__PING__":
                            self._backend_last_seen[actual_name] = time.time()
                            try:
                                # 应答用 ping 控制帧：文本 __PONG__ 曾被写进
                                # 后端 PTY 出现在用户终端（协议帧泄漏）
                                await websocket.ping()
                            except Exception:
                                pass
                            continue
                        # ── 二进制帧：PTY 原始输出，直接转发 ──
                        if isinstance(message, bytes):
                            # 只发给「当前 target 就是本后端」的前端。以前是广播，
                            # 多后端同时在线时每个终端都会混进别人的提示符、
                            # tmux 状态栏、全屏重绘。
                            src = actual_name

                            def _watching(f, _src=src):
                                t = self._resolve_target(f)
                                return bool(t) and t[0] == _src

                            await _forward_binary_to_frontends(
                                self.frontends, message,
                                self.frontend_text_modes,
                                src if len(self.backends) > 1 else None,
                                _watching,
                            )
                            continue
                        # ── 文本帧：文件传输协议消息不加标签 ──
                        if isinstance(message, str) and message.startswith("__FILE_"):
                            await _forward_to_frontends_untagged(
                                self.frontends, message,
                            )
                            continue
                        # ── 文本帧：转发给所有前端，多后端时加标签 ──
                        await _forward_to_frontends(
                            self.frontends, message,
                            actual_name if len(self.backends) > 1 else None,
                        )
                except websockets.exceptions.ConnectionClosed:
                    pass
                # 传入 websocket 做身份校验：重连顶替场景下旧 handler
                # 醒来时名称已归属新连接，跳过注销避免误删新后端
                await self._unregister_backend(actual_name, websocket)

            # ── 前端注册（AUTH 消息认证）──
            elif _is_frontend_auth(first, self.token):
                token_str = first.replace("AUTH:", "").strip() if first.startswith("AUTH:") else ""
                token_info = self.token_manager.validate(token_str) if token_str else None
                if token_info is None and self.token_manager.enabled:
                    # 已启用认证却无有效 token → 拒绝；
                    # 未启用认证（无 token 模式）→ 任何消息都放行（向后兼容）
                    self.brute_force.record_failure(peer_ip)
                    self.audit.auth_failed(peer_ip, token_str[:8])
                    await websocket.send("AUTH_FAIL")
                    await websocket.close(1008, "Authentication failed")
                    return
                self.brute_force.record_success(peer_ip)
                await websocket.send("AUTH_OK")
                await self._register_frontend(websocket, token_info, peer_ip)

            else:
                # 错误 token 的前端 AUTH（AUTH:guess）与 token 不匹配的
                # 后端注册（IAM_BACKEND:guess:...）都会落到这里。之前
                # 此路径不计数，--token 模式的防爆破锁定形同虚设。
                self.brute_force.record_failure(peer_ip)
                self.audit.auth_failed(peer_ip, first[:8] if isinstance(first, str) else "<binary>")
                await websocket.send("AUTH_FAIL")
                await websocket.close(1008, "Authentication failed")

        except asyncio.TimeoutError:
            await websocket.close(1002, "Protocol error: expected auth message")
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.exception(f"Handler error: {e}")
        finally:
            # 释放 IP 连接计数（认证失败/超时连接也要释放配额）
            info = self._client_info.get(websocket)
            if info:
                ip = info.get("ip", "")
                if ip:
                    self._release_ip_slot(ip)
            self._client_info.pop(websocket, None)
            # 异常路径的后端清理：按身份匹配（正常路径已注销，此处为空操作），
            # 走完整注销流程（通知 + 广播），而非静默 pop
            for n, bws in list(self.backends.items()):
                if bws is websocket:
                    await self._unregister_backend(n, websocket)
                    break
            self.frontends.discard(websocket)
            self.frontend_targets.pop(websocket, None)
            self.frontend_text_modes.pop(websocket, None)


# ──────────────────────────────────────────────
#  公共 API
# ──────────────────────────────────────────────

def _create_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """从证书文件创建 SSL 上下文。

    Args:
        cert_path: TLS 证书文件路径。
        key_path: TLS 私钥文件路径。

    Returns:
        配置好的 ``SSLContext`` 实例。
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    logger.info(f"TLS enabled (cert={cert_path})")
    return ctx


async def _run_async(
    host: str,
    port: int,
    handler: Any,
    ssl_context: ssl.SSLContext | None = None,
    compression: bool = False,
    state: "RelayState | None" = None,
) -> None:
    """异步运行中继服务。

    Args:
        host: 监听地址。
        port: 监听端口。
        handler: WebSocket 连接处理函数。
        ssl_context: 可选 SSL 上下文，启用 ``wss://``。
        compression: 是否启用 WebSocket permessage-deflate 压缩。
        state: 可选 RelayState 实例；传入时启动后端心跳超时 watchdog。
    """
    watch_task = None
    if state is not None:
        watch_task = asyncio.create_task(state.heartbeat_watchdog())
    try:
        async with websockets.serve(
            handler, host, port,
            ssl=ssl_context,
            ping_interval=None,
            ping_timeout=None,
            process_request=_http_request_handler,
            compression="deflate" if compression else None,
            max_size=_WS_MAX_FRAME,  # 见 _WS_MAX_FRAME 处的约束注释
        ):
            scheme = "wss" if ssl_context else "ws"
            http_scheme = "https" if ssl_context else "http"
            logger.info(f"Relay running on {scheme}://{host}:{port}")
            if _INDEX_HTML:
                logger.info(f"Web terminal: {http_scheme}://{host}:{port}")
            logger.info("Heartbeat: disabled protocol ping (using __PING__/__PONG__ instead)")
            await asyncio.Future()
    finally:
        if watch_task is not None:
            watch_task.cancel()


def run_relay(
    host: str = "0.0.0.0",
    port: int = 8080,
    token: str | None = None,
    cert_path: str | None = None,
    key_path: str | None = None,
    wxpush: str | None = None,
    token_file: str | None = None,
    allow_ip: list[str] | None = None,
    deny_cmd: list[str] | None = None,
    compression: bool = False,
) -> None:
    """启动 WebSocket 中继服务。

    Args:
        host: 监听地址，默认 ``0.0.0.0``。
        port: 监听端口，默认 ``8080``。
        token: 可选认证令牌。``None`` = 不开启认证（向后兼容）。
        cert_path: TLS 证书路径。
        key_path: TLS 私钥路径。
        wxpush: 微信推送通知，格式 ``url:key``。
        token_file: token JSON 文件路径（支持多 token + 角色 + 过期）。
        allow_ip: IP 白名单列表（支持 CIDR）。
        deny_cmd: 命令黑名单列表。
        compression: 启用 WebSocket permessage-deflate 压缩。
    """
    if token:
        logger.info(f"Authentication enabled (token={token[:8]}...)")
    else:
        # 无 token = 任何人都能注册后端/前端（向后兼容取舍，P1-2）。
        # 绑定到非回环地址时风险实际存在，用横幅级日志醒目提示。
        exposed = host not in ("127.0.0.1", "localhost", "::1")
        banner = (
            "=" * 62,
            "⚠️  SECURITY WARNING: running WITHOUT token authentication",
            "   anyone who can reach this port becomes backend/frontend",
            *(
                ("   ⚠️  listening on non-loopback %s — EXPOSED" % host,)
                if exposed
                else ("   (listening on loopback %s, local only)" % host,)
            ),
            "   recommend: wsstunnel relay --token <secret> "
            "or bind 127.0.0.1 behind a reverse proxy",
            "=" * 62,
        )
        for line in banner:
            logger.warning(line)

    notifier: _WxPushNotifier | None = None
    if wxpush:
        try:
            url, key = wxpush.rsplit(":", 1)
            notifier = _WxPushNotifier(url.strip(), key.strip())
            logger.info(f"WxPush enabled (url={url.strip()[:40]}...)")
        except ValueError:
            logger.error(f"Invalid --wxpush format, expected url:key, got: {wxpush}")

    ssl_context: ssl.SSLContext | None = None
    if cert_path:
        ssl_context = _create_ssl_context(cert_path, key_path or cert_path)

    state = RelayState(token, notifier)

    # ── 安全组件配置 ──
    if token_file:
        try:
            state.token_manager.load_file(token_file)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load token file '{token_file}': {e}")
            return
    if allow_ip:
        state.ip_allowlist = IPAllowList(allow_ip)
        logger.info(f"IP allowlist enabled: {allow_ip}")
    if deny_cmd:
        state.deny_list = DenyList(deny_cmd)
        logger.info(f"Command deny list enabled: {deny_cmd}")

    if state.token_manager.enabled:
        logger.info(
            f"Security: {state.token_manager.count} token(s) loaded, "
            f"IP allowlist={'on' if state.ip_allowlist.enabled else 'off'}, "
            f"brute-force={'on' if state.brute_force else 'off'}"
        )

    if compression:
        logger.info("WebSocket compression enabled (permessage-deflate)")
    asyncio.run(_run_async(host, port, state.handler, ssl_context, compression, state))
