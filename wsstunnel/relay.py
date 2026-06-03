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
import logging
import os
import re
import ssl
from typing import Any
from urllib.parse import urlparse, parse_qs

import httpx
import websockets
from websockets.http import Headers
from websockets.server import Response

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


async def _http_request_handler(connection: Any, request: Any) -> Response | None:
    """处理普通 HTTP 请求，返回 Web 终端页面。

    在 ``process_request`` 回调中使用。返回 ``None`` 则继续 WebSocket 升级。
    """
    if _INDEX_HTML is None:
        return None
    # 只拦截非 WebSocket 的 HTTP GET 请求
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    if request.path in ("/", "/index.html", "/wstunnel"):
        headers = Headers()
        headers["Content-Type"] = "text/html; charset=utf-8"
        return Response(200, "OK", headers, _INDEX_HTML)
    return None


# 匹配 ANSI 转义序列：ESC [ ... 最终字节，以及 ESC ] ... BEL/ST
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]|\x1b\[[?][0-9;]*[a-zA-Z]")


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

def _parse_backend_auth(msg: str | bytes, token: str | None) -> tuple[str, str] | None:
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

    prefix = f"IAM_BACKEND:{token}" if token else "IAM_BACKEND"
    if not msg.startswith(prefix):
        return None

    rest = msg[len(prefix):]
    parts = [p.strip() for p in rest.split(":") if p.strip()] if rest else []

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
    dead: set[Any] = set()
    for f in frontends:
        try:
            await f.send(payload)
        except Exception:
            dead.add(f)
    frontends -= dead


async def _forward_binary_to_frontends(
    frontends: set[Any],
    data: bytes,
    frontend_text_modes: dict[Any, bool],
    tag: str | None = None,
) -> None:
    """转发二进制帧给所有前端。

    文本模式前端会收到剥离 ANSI 转义码的文本帧，其余前端收到原始二进制帧。
    自动清理已断开的前端连接。

    Args:
        frontends: 前端 WebSocket 连接集合（会被原地修改）。
        data: 二进制 PTY 输出数据。
        frontend_text_modes: 前端连接 → 是否文本模式。
        tag: 保留参数，当前未使用。
    """
    dead: set[Any] = set()
    for f in frontends:
        try:
            if frontend_text_modes.get(f):
                # 文本模式：剥离 ANSI，以文本帧发送
                text = _strip_ansi(data)
                if text:
                    await f.send(text)
            else:
                await f.send(data)
        except Exception:
            dead.add(f)
    frontends -= dead


async def _send_backend_list(
    ws: Any,
    backends: dict[str, Any],
    backend_modes: dict[str, str] | None = None,
    current: str | None = None,
) -> None:
    """向前端发送当前后端列表。

    Args:
        ws: 目标前端 WebSocket。
        backends: 后端名称→连接映射。
        backend_modes: 后端名称→模式映射。
        current: 当前选中的后端名称。
    """
    if not backends:
        await ws.send("[Info] No backends connected")
    else:
        names: list[str] = []
        for n in backends:
            mode = (backend_modes or {}).get(n, "pipe")
            marker = " *" if n == current else ""
            names.append(f"{n}({mode}){marker}")
        lines = [f"[Info] Connected backends: {', '.join(names)}"]
        if current:
            lines.append(f"[Info] Current: {current}")
        await ws.send("\n".join(lines))


async def _broadcast_backend_list(
    frontends: set[Any],
    backends: dict[str, Any],
    backend_modes: dict[str, str],
    frontend_targets: dict[Any, str | None],
) -> None:
    """广播后端列表给所有前端。

    自动清理已断开的前端连接。

    Args:
        frontends: 前端 WebSocket 连接集合（会被原地修改）。
        backends: 后端名称→连接映射。
        backend_modes: 后端名称→模式映射。
        frontend_targets: 前端→当前后端名称映射。
    """
    dead: set[Any] = set()
    for f in frontends:
        try:
            current = frontend_targets.get(f)
            await _send_backend_list(f, backends, backend_modes, current)
        except Exception:
            dead.add(f)
    frontends -= dead


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
        self.frontends: set[Any] = set()
        self.frontend_targets: dict[Any, str | None] = {}
        self.frontend_text_modes: dict[Any, bool] = {}
        self._counter: int = 0

    def _next_backend_name(self) -> str:
        """生成自动后端名称 ``backend-N``。"""
        self._counter += 1
        return f"backend-{self._counter}"

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

    async def _register_backend(self, ws: Any, name: str | None, mode: str) -> str:
        """注册后端连接，返回实际使用的名称。"""
        if not name:
            name = self._next_backend_name()
        self.backends[name] = ws
        self.backend_modes[name] = mode
        logger.info(
            f"Backend registered: '{name}' mode={mode} "
            f"(total {len(self.backends)})"
        )
        # 微信推送：后端上线
        if self.notifier:
            await self.notifier.send(
                f"✅ ws-tunnel: 后端 '{name}' 已上线 ({mode})"
            )
        # 通知所有前端后端列表变化
        await _broadcast_backend_list(
            self.frontends, self.backends, self.backend_modes,
            self.frontend_targets,
        )
        return name

    async def _unregister_backend(self, name: str) -> None:
        """注销后端连接并清理关联状态。"""
        self.backends.pop(name, None)
        self.backend_modes.pop(name, None)
        # 如果断开的后端正好是某个前端的当前目标，清除它
        for f in list(self.frontend_targets):
            if self.frontend_targets.get(f) == name:
                self.frontend_targets[f] = None
        logger.info(f"Backend disconnected: '{name}' (total {len(self.backends)})")
        if self.notifier:
            await self.notifier.send(
                f"❌ ws-tunnel: 后端 '{name}' 已下线"
            )
        await _broadcast_backend_list(
            self.frontends, self.backends, self.backend_modes,
            self.frontend_targets,
        )

    # ── 前端注册/注销 ──

    async def _register_frontend(self, ws: Any) -> None:
        """注册前端连接并进入消息循环。"""
        self.frontends.add(ws)
        self.frontend_targets[ws] = None
        logger.info(f"Frontend authenticated (total {len(self.frontends)})")
        await _send_backend_list(
            ws, self.backends, self.backend_modes, None,
        )
        try:
            async for message in ws:
                await self._handle_frontend_msg(ws, message)
        except websockets.exceptions.ConnectionClosed:
            pass
        self._unregister_frontend(ws)

    def _unregister_frontend(self, ws: Any) -> None:
        """注销前端连接。"""
        self.frontends.discard(ws)
        self.frontend_targets.pop(ws, None)
        self.frontend_text_modes.pop(ws, None)
        logger.info(f"Frontend disconnected (total {len(self.frontends)})")

    # ── 前端消息路由 ──

    async def _handle_frontend_msg(self, ws: Any, message: str | bytes) -> None:
        """处理前端发出的单条消息（支持文本和二进制）。

        根据消息内容路由到对应的子处理方法。
        """
        # 二进制帧：原始按键输入，转发给当前后端
        if isinstance(message, bytes):
            await self._forward_binary_to_backend(ws, message)
            return

        msg = message.strip()

        # LIST: 列举后端
        if msg.upper() == "LIST":
            await self._handle_list(ws)
            return

        # USE [name]: 切换/查看当前后端
        if msg.upper() == "USE" or msg.upper().startswith("USE "):
            await self._handle_use(ws, msg)
            return

        # __RESIZE / __SIGNAL / __TEXT: 控制命令
        if msg.startswith("__RESIZE:") or msg.startswith("__SIGNAL:"):
            await self._handle_control(ws, msg)
            return

        # __TEXT: 切换文本模式（剥离 ANSI 转义码）
        if msg == "__TEXT":
            self.frontend_text_modes[ws] = True
            await ws.send("[Info] Text mode enabled (ANSI stripped)")
            return
        if msg == "__RAW":
            self.frontend_text_modes[ws] = False
            await ws.send("[Info] Raw mode enabled (binary PTY)")
            return

        # @name <cmd>: 临时发给指定后端
        if msg.startswith("@"):
            await self._handle_at_cmd(ws, msg)
            return

        # 普通命令: 发送给当前后端
        await self._send_to_current_backend(ws, msg)

    async def _handle_list(self, ws: Any) -> None:
        """处理 LIST 命令：列举所有后端。"""
        current = self.frontend_targets.get(ws)
        await _send_backend_list(ws, self.backends, self.backend_modes, current)

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

    # ── 主 handler 入口 ──

    async def handler(self, websocket: Any, _path: object = None) -> None:
        """WebSocket 连接处理入口。

        由 ``websockets.serve()`` 调用，负责：
        1. URL token 自动认证（前端）
        2. 第一条消息角色检测（后端/前端）
        3. 后端消息转发循环
        4. 异常和连接清理
        """
        # ── URL token 自动认证 ──
        url_token = self._extract_url_token(websocket)
        if url_token and self.token and url_token == self.token:
            await websocket.send("AUTH_OK")
            await self._register_frontend(websocket)
            return

        try:
            first = await asyncio.wait_for(websocket.recv(), timeout=30)

            # ── 后端注册 ──
            backend_info = _parse_backend_auth(first, self.token)
            if backend_info:
                name, mode = backend_info
                if name is None:
                    name = self._next_backend_name()
                if name in self.backends:
                    await websocket.close(1008, f"Backend '{name}' already registered")
                    return
                actual_name = await self._register_backend(websocket, name, mode)
                try:
                    async for message in websocket:
                        # ── 心跳（仅文本） ──
                        if isinstance(message, str) and message == "__PING__":
                            try:
                                await websocket.send("__PONG__")
                            except Exception:
                                pass
                            continue
                        # ── 二进制帧：PTY 原始输出，直接转发 ──
                        if isinstance(message, bytes):
                            await _forward_binary_to_frontends(
                                self.frontends, message,
                                self.frontend_text_modes,
                                actual_name if len(self.backends) > 1 else None,
                            )
                            continue
                        # ── 文本帧：转发给所有前端，多后端时加标签 ──
                        await _forward_to_frontends(
                            self.frontends, message,
                            actual_name if len(self.backends) > 1 else None,
                        )
                except websockets.exceptions.ConnectionClosed:
                    pass
                await self._unregister_backend(actual_name)

            # ── 前端注册（AUTH 消息认证）──
            elif _is_frontend_auth(first, self.token):
                await websocket.send("AUTH_OK")
                await self._register_frontend(websocket)

            else:
                await websocket.send("AUTH_FAIL")
                await websocket.close(1008, "Authentication failed")

        except asyncio.TimeoutError:
            await websocket.close(1002, "Protocol error: expected auth message")
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.exception(f"Handler error: {e}")
        finally:
            # 清理断开的连接
            for n, ws in list(self.backends.items()):
                if ws == websocket:
                    self.backends.pop(n, None)
                    self.backend_modes.pop(n, None)
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
) -> None:
    """异步运行中继服务。

    Args:
        host: 监听地址。
        port: 监听端口。
        handler: WebSocket 连接处理函数。
        ssl_context: 可选 SSL 上下文，启用 ``wss://``。
    """
    async with websockets.serve(
        handler, host, port,
        ssl=ssl_context,
        ping_interval=20,
        ping_timeout=10,
        process_request=_http_request_handler,
    ):
        scheme = "wss" if ssl_context else "ws"
        http_scheme = "https" if ssl_context else "http"
        logger.info(f"Relay running on {scheme}://{host}:{port}")
        if _INDEX_HTML:
            logger.info(f"Web terminal: {http_scheme}://{host}:{port}")
        logger.info("Heartbeat: ping every 20s, timeout 10s")
        await asyncio.Future()


def run_relay(
    host: str = "0.0.0.0",
    port: int = 8080,
    token: str | None = None,
    cert_path: str | None = None,
    key_path: str | None = None,
    wxpush: str | None = None,
) -> None:
    """启动 WebSocket 中继服务。

    Args:
        host: 监听地址，默认 ``0.0.0.0``。
        port: 监听端口，默认 ``8080``。
        token: 可选认证令牌。``None`` = 不开启认证（向后兼容）。
        cert_path: TLS 证书路径。
        key_path: TLS 私钥路径。
        wxpush: 微信推送通知，格式 ``url:key``。
    """
    if token:
        logger.info(f"Authentication enabled (token={token[:8]}...)")
    else:
        logger.warning("No token set — anyone can connect!")

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
    asyncio.run(_run_async(host, port, state.handler, ssl_context))
