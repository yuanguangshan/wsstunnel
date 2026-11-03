"""tests/test_relay_bg_proxy.py — /api/bg 背景图代理与磁盘缓存

覆盖：
1. SSRF 防护：scheme / 内网与保留 IP / 主机名解析结果 / localhost；
2. 下载约束：非图片、超大、非 200、重定向（每次重新校验、最多 3 次）；
3. 磁盘缓存：命中不触网、refresh=1 强制重拉并覆盖、坏元数据按未命中、
   写缓存失败退化为不缓存；
4. handler 层：两代 websockets API（legacy 三元组 / asyncio Response）、
   400/403/502 与 200 + X-Bg-Cache 头的完整约定。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from urllib.parse import quote

import httpx
import pytest

import wsstunnel.relay as relay
from wsstunnel.relay import (
    _BgError,
    _bg_cache_paths,
    _bg_cache_read,
    _bg_cache_write,
    _bg_download,
    _bg_get_image,
    _bg_ip_blocked,
    _bg_validate_url,
    _http_request_handler,
)

# 一份合法 PNG 魔术字节（内容不重要，只看 MIME/大小校验）。
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

PUBLIC_URL = "https://example.com/bg.png"
PUBLIC_IP = "93.184.216.34"


# ──────────────────────────────────────────────
#  公共夹具 / 辅助
# ──────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """所有用例的磁盘缓存都隔离到 tmp 目录，绝不污染 ~/.wsstunnel。"""
    cache_dir = tmp_path / "bg-cache"
    monkeypatch.setenv("WSSTUNNEL_BG_CACHE", str(cache_dir))
    return cache_dir


@pytest.fixture
def public_dns(monkeypatch):
    """把主机名解析成公网 IP（IP 字面量原样返回），避免测试真的走 DNS。"""

    async def _fake_resolve(host: str) -> list[str]:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return [PUBLIC_IP]
        return [host]

    monkeypatch.setattr(relay, "_bg_resolve_hosts", _fake_resolve)


class _FakeUpstreamResponse:
    """模拟 httpx.stream() 返回的流式响应上下文。"""

    def __init__(self, status_code: int = 200, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
        self._chunks = list(chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeAsyncClient:
    """按顺序吐出预设响应的假 httpx.AsyncClient（记录请求 URL 与参数）。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.urls: list[str] = []
        self.kwargs: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def stream(self, method, url, **kwargs):
        self.urls.append(str(url))
        if not self._responses:
            raise AssertionError("unexpected extra upstream request")
        return self._responses.pop(0)


def _patch_upstream(monkeypatch, *responses) -> _FakeAsyncClient:
    client = _FakeAsyncClient(responses)

    def _factory(*args, **kwargs):
        client.kwargs = kwargs
        return client

    monkeypatch.setattr(relay.httpx, "AsyncClient", _factory)
    return client


def _image_response(body: bytes = PNG, mime: str = "image/png"):
    return _FakeUpstreamResponse(
        status_code=200,
        headers={"Content-Type": mime},
        chunks=[body[:10], body[10:]] if body else [],
    )


def _redirect_response(location: str):
    return _FakeUpstreamResponse(
        status_code=302, headers={"Location": location}, chunks=[]
    )


async def _call_handler(path: str, *, legacy: bool):
    """按 websockets 代际调用 _http_request_handler。"""
    from websockets.datastructures import Headers

    if legacy:
        # legacy serve: process_request(path: str, request_headers: Headers)
        return await _http_request_handler(path, Headers())

    class _FakeRequest:
        def __init__(self) -> None:
            self.path = path
            self.headers = Headers()

    # asyncio serve: process_request(connection, request: Request)
    return await _http_request_handler(object(), _FakeRequest())


def _status_of(resp) -> int:
    return resp[0] if isinstance(resp, tuple) else resp.status_code


def _headers_of(resp):
    return resp[1] if isinstance(resp, tuple) else resp.headers


def _body_of(resp) -> bytes:
    return resp[2] if isinstance(resp, tuple) else resp.body


# ──────────────────────────────────────────────
#  1. SSRF：IP 地址段判定
# ──────────────────────────────────────────────

class TestBgIpBlocked:
    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",  # 环回
            "127.8.8.8",
            "10.1.2.3",  # 私有
            "172.16.0.1",
            "192.168.1.10",
            "169.254.169.254",  # 云元数据（链路本地）
            "0.0.0.0",  # 未指定
            "100.64.0.1",  # 运营商级 NAT
            "::1",  # IPv6 环回
            "fc00::1",  # IPv6 唯一本地
            "fe80::1",  # IPv6 链路本地
            "::ffff:127.0.0.1",  # IPv4-mapped
        ],
    )
    def test_blocked(self, ip):
        assert _bg_ip_blocked(ip) is True

    @pytest.mark.parametrize(
        "ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2001:4860:4860::8888"]
    )
    def test_public_allowed(self, ip):
        assert _bg_ip_blocked(ip) is False

    def test_non_ip_treated_as_blocked(self):
        """非 IP 字面量不该被当成公网放行（调用方会先走 DNS 解析）。"""
        assert _bg_ip_blocked("example.com") is True

    def test_proxy_fake_ip_allowed(self):
        """Clash/Surge 的 fake-IP 段（198.18.0.0/15）必须放行。

        开着代理的机器上所有域名都会解析成该段地址（本机实测
        picsum.photos → 198.18.4.174），若按私网拒绝，背景图代理会
        完全不可用；该段没有真实内网服务，放行不构成 SSRF 跳板。
        """
        assert _bg_ip_blocked("198.18.4.174") is False
        assert _bg_ip_blocked("198.19.255.254") is False
        # 真正的私网/运营商 NAT 仍然拦截
        assert _bg_ip_blocked("100.64.0.1") is True


# ──────────────────────────────────────────────
#  2. SSRF：URL 校验
# ──────────────────────────────────────────────

class TestBgValidateUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/a.png",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:image/png;base64,AAAA",
            "http:///a.png",
            "https://",
            "not a url",
            "",
        ],
    )
    @pytest.mark.asyncio
    async def test_bad_scheme_or_shape_is_400(self, url, monkeypatch):
        async def _boom(host):  # 不该走到解析
            raise AssertionError(f"unexpected DNS lookup: {host}")

        monkeypatch.setattr(relay, "_bg_resolve_hosts", _boom)
        with pytest.raises(_BgError) as ei:
            await _bg_validate_url(url)
        assert ei.value.status == 400

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/a.png",
            "http://127.0.0.1:8080/a.png",
            "http://10.0.0.5/a.png",
            "http://172.16.3.4/a.png",
            "http://192.168.1.1/a.png",
            "http://169.254.169.254/latest/meta-data/",
            "http://0.0.0.0/a.png",
            "http://[::1]/a.png",
            "http://100.64.0.9/a.png",
        ],
    )
    @pytest.mark.asyncio
    async def test_private_literal_is_403(self, url):
        with pytest.raises(_BgError) as ei:
            await _bg_validate_url(url)
        assert ei.value.status == 403

    @pytest.mark.asyncio
    async def test_localhost_is_403_without_dns(self, monkeypatch):
        async def _boom(host):
            raise AssertionError("localhost 不该走到 DNS 解析")

        monkeypatch.setattr(relay, "_bg_resolve_hosts", _boom)
        with pytest.raises(_BgError) as ei:
            await _bg_validate_url("http://localhost:8080/a.png")
        assert ei.value.status == 403

    @pytest.mark.asyncio
    async def test_hostname_resolving_to_private_is_403(self, monkeypatch):
        async def _fake_resolve(host):
            assert host == "intranet.example.test"
            return ["127.0.0.1"]

        monkeypatch.setattr(relay, "_bg_resolve_hosts", _fake_resolve)
        with pytest.raises(_BgError) as ei:
            await _bg_validate_url("http://intranet.example.test/a.png")
        assert ei.value.status == 403

    @pytest.mark.asyncio
    async def test_mixed_resolution_is_403(self, public_dns, monkeypatch):
        async def _fake_resolve(host):
            return [PUBLIC_IP, "10.0.0.1"]

        monkeypatch.setattr(relay, "_bg_resolve_hosts", _fake_resolve)
        with pytest.raises(_BgError) as ei:
            await _bg_validate_url(PUBLIC_URL)
        assert ei.value.status == 403

    @pytest.mark.asyncio
    async def test_public_hostname_allowed(self, public_dns):
        assert await _bg_validate_url(PUBLIC_URL) == PUBLIC_URL


# ──────────────────────────────────────────────
#  3. 下载约束
# ──────────────────────────────────────────────

class TestBgDownload:
    @pytest.mark.asyncio
    async def test_success_returns_body_and_mime(self, public_dns, monkeypatch):
        client = _patch_upstream(
            monkeypatch, _image_response(PNG, "image/webp")
        )
        body, mime = await _bg_download(PUBLIC_URL)
        assert body == PNG
        assert mime == "image/webp"
        assert client.urls == [PUBLIC_URL]
        # 手动跟随重定向 + 8s 超时是安全契约的一部分
        assert client.kwargs.get("follow_redirects") is False
        assert "timeout" in client.kwargs

    @pytest.mark.asyncio
    async def test_non_image_is_502(self, public_dns, monkeypatch):
        _patch_upstream(
            monkeypatch,
            _FakeUpstreamResponse(
                status_code=200,
                headers={"Content-Type": "text/html"},
                chunks=[b"<html></html>"],
            ),
        )
        with pytest.raises(_BgError) as ei:
            await _bg_download(PUBLIC_URL)
        assert ei.value.status == 502

    @pytest.mark.asyncio
    async def test_missing_content_type_is_502(self, public_dns, monkeypatch):
        _patch_upstream(
            monkeypatch,
            _FakeUpstreamResponse(status_code=200, chunks=[PNG]),
        )
        with pytest.raises(_BgError) as ei:
            await _bg_download(PUBLIC_URL)
        assert ei.value.status == 502

    @pytest.mark.asyncio
    async def test_oversize_is_502(self, public_dns, monkeypatch):
        half = relay._BG_MAX_BYTES // 2 + 1
        _patch_upstream(
            monkeypatch,
            _FakeUpstreamResponse(
                status_code=200,
                headers={"Content-Type": "image/png"},
                chunks=[b"x" * half, b"x" * half],
            ),
        )
        with pytest.raises(_BgError) as ei:
            await _bg_download(PUBLIC_URL)
        assert ei.value.status == 502

    @pytest.mark.asyncio
    async def test_non_200_is_502(self, public_dns, monkeypatch):
        _patch_upstream(
            monkeypatch,
            _FakeUpstreamResponse(status_code=404, chunks=[]),
        )
        with pytest.raises(_BgError) as ei:
            await _bg_download(PUBLIC_URL)
        assert ei.value.status == 502

    @pytest.mark.asyncio
    async def test_empty_body_is_502(self, public_dns, monkeypatch):
        _patch_upstream(
            monkeypatch,
            _FakeUpstreamResponse(
                status_code=200, headers={"Content-Type": "image/png"}, chunks=[]
            ),
        )
        with pytest.raises(_BgError) as ei:
            await _bg_download(PUBLIC_URL)
        assert ei.value.status == 502

    @pytest.mark.asyncio
    async def test_redirect_followed(self, public_dns, monkeypatch):
        client = _patch_upstream(
            monkeypatch,
            _redirect_response("https://cdn.example.com/real.png"),
            _image_response(),
        )
        body, mime = await _bg_download(PUBLIC_URL)
        assert body == PNG and mime == "image/png"
        assert client.urls == [PUBLIC_URL, "https://cdn.example.com/real.png"]

    @pytest.mark.asyncio
    async def test_redirect_to_private_is_403(self, public_dns, monkeypatch):
        """302 到内网必须重新校验并拒绝（否则 SSRF 防护形同虚设）。"""
        _patch_upstream(
            monkeypatch,
            _redirect_response("http://169.254.169.254/latest/meta-data/"),
        )
        with pytest.raises(_BgError) as ei:
            await _bg_download(PUBLIC_URL)
        assert ei.value.status == 403

    @pytest.mark.asyncio
    async def test_redirect_without_location_is_502(self, public_dns, monkeypatch):
        _patch_upstream(
            monkeypatch,
            _FakeUpstreamResponse(status_code=302, chunks=[]),
        )
        with pytest.raises(_BgError) as ei:
            await _bg_download(PUBLIC_URL)
        assert ei.value.status == 502

    @pytest.mark.asyncio
    async def test_too_many_redirects_is_502(self, public_dns, monkeypatch):
        """初始请求 + 最多 3 次重定向 = 最多 4 次上游请求。"""
        client = _patch_upstream(
            monkeypatch,
            _redirect_response("/1.png"),
            _redirect_response("/2.png"),
            _redirect_response("/3.png"),
            _redirect_response("/4.png"),
        )
        with pytest.raises(_BgError) as ei:
            await _bg_download(PUBLIC_URL)
        assert ei.value.status == 502
        assert len(client.urls) == relay._BG_MAX_REDIRECTS + 1

    @pytest.mark.asyncio
    async def test_network_error_is_502(self, public_dns, monkeypatch):
        class _BoomClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            def stream(self, *args, **kwargs):
                raise httpx.ConnectError("boom")

        monkeypatch.setattr(relay.httpx, "AsyncClient", lambda *a, **k: _BoomClient())
        with pytest.raises(_BgError) as ei:
            await _bg_download(PUBLIC_URL)
        assert ei.value.status == 502


# ──────────────────────────────────────────────
#  4. 磁盘缓存
# ──────────────────────────────────────────────

class TestBgCache:
    def test_cache_dir_env_override(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom-cache"
        monkeypatch.setenv("WSSTUNNEL_BG_CACHE", str(custom))
        assert relay._bg_cache_dir() == str(custom)

    def test_cache_dir_defaults_to_home(self, monkeypatch):
        monkeypatch.delenv("WSSTUNNEL_BG_CACHE", raising=False)
        assert relay._bg_cache_dir() == os.path.join(
            os.path.expanduser("~"), ".wsstunnel", "bg-cache"
        )

    @pytest.mark.asyncio
    async def test_hit_does_not_touch_network(self, monkeypatch):
        _bg_cache_write(PUBLIC_URL, PNG, "image/png")

        def _boom(*args, **kwargs):
            raise AssertionError("缓存命中不应创建 httpx 客户端")

        monkeypatch.setattr(relay.httpx, "AsyncClient", _boom)
        body, mime, from_cache = await _bg_get_image(PUBLIC_URL)
        assert (body, mime, from_cache) == (PNG, "image/png", True)

    @pytest.mark.asyncio
    async def test_cache_files_use_sha256_and_metadata(self, isolated_cache):
        _bg_cache_write(PUBLIC_URL, PNG, "image/png")
        digest = hashlib.sha256(PUBLIC_URL.encode("utf-8")).hexdigest()
        body_path, meta_path = _bg_cache_paths(PUBLIC_URL)
        assert os.path.basename(body_path) == digest
        assert meta_path == body_path + ".json"
        assert os.path.dirname(body_path) == str(isolated_cache)
        with open(body_path, "rb") as f:
            assert f.read() == PNG
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["url"] == PUBLIC_URL
        assert meta["mime"] == "image/png"
        assert isinstance(meta["ts"], float)

    @pytest.mark.asyncio
    async def test_refresh_skips_cache_and_overwrites(self, monkeypatch):
        _bg_cache_write(PUBLIC_URL, b"old-body", "image/png")
        new_body = PNG + b"new"
        calls: list[str] = []

        async def _fake_download(url: str):
            calls.append(url)
            return new_body, "image/webp"

        monkeypatch.setattr(relay, "_bg_download", _fake_download)
        body, mime, from_cache = await _bg_get_image(PUBLIC_URL, refresh=True)
        assert (body, mime, from_cache) == (new_body, "image/webp", False)
        assert calls == [PUBLIC_URL]
        # 覆盖后的缓存读到的是新内容
        assert _bg_cache_read(PUBLIC_URL) == (new_body, "image/webp")

    @pytest.mark.asyncio
    async def test_miss_then_download_writes_cache(self, public_dns, monkeypatch):
        _patch_upstream(monkeypatch, _image_response())
        body, mime, from_cache = await _bg_get_image(PUBLIC_URL)
        assert (body, mime, from_cache) == (PNG, "image/png", False)
        assert _bg_cache_read(PUBLIC_URL) == (PNG, "image/png")

    @pytest.mark.asyncio
    async def test_missing_metadata_is_treated_as_miss(self, monkeypatch):
        body_path, _ = _bg_cache_paths(PUBLIC_URL)
        os.makedirs(os.path.dirname(body_path), exist_ok=True)
        with open(body_path, "wb") as f:
            f.write(PNG)

        calls: list[str] = []

        async def _fake_download(url: str):
            calls.append(url)
            return PNG, "image/png"

        monkeypatch.setattr(relay, "_bg_download", _fake_download)
        _, _, from_cache = await _bg_get_image(PUBLIC_URL)
        assert from_cache is False
        assert calls == [PUBLIC_URL]

    @pytest.mark.asyncio
    async def test_corrupt_metadata_is_treated_as_miss(self, monkeypatch):
        body_path, meta_path = _bg_cache_paths(PUBLIC_URL)
        os.makedirs(os.path.dirname(body_path), exist_ok=True)
        with open(body_path, "wb") as f:
            f.write(PNG)
        with open(meta_path, "w", encoding="utf-8") as f:
            f.write("{not-json")

        async def _fake_download(url: str):
            return PNG, "image/png"

        monkeypatch.setattr(relay, "_bg_download", _fake_download)
        _, _, from_cache = await _bg_get_image(PUBLIC_URL)
        assert from_cache is False

    @pytest.mark.asyncio
    async def test_non_image_mime_in_metadata_is_miss(self, monkeypatch):
        _bg_cache_write(PUBLIC_URL, b"<html>", "text/html")

        async def _fake_download(url: str):
            return PNG, "image/png"

        monkeypatch.setattr(relay, "_bg_download", _fake_download)
        body, mime, from_cache = await _bg_get_image(PUBLIC_URL)
        assert (body, mime, from_cache) == (PNG, "image/png", False)

    @pytest.mark.asyncio
    async def test_write_failure_degrades_to_no_cache(self, tmp_path, monkeypatch):
        """缓存目录不可创建时只告警，图片照常返回（不能 500）。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir", encoding="utf-8")
        monkeypatch.setattr(
            relay, "_bg_cache_dir", lambda: str(blocker / "cache")
        )

        async def _fake_download(url: str):
            return PNG, "image/png"

        monkeypatch.setattr(relay, "_bg_download", _fake_download)
        body, mime, from_cache = await _bg_get_image(PUBLIC_URL)
        assert (body, mime, from_cache) == (PNG, "image/png", False)


# ──────────────────────────────────────────────
#  5. handler 层（two websockets API shapes）
# ──────────────────────────────────────────────

class TestBgHttpHandler:
    @pytest.mark.parametrize("legacy", [True, False])
    @pytest.mark.asyncio
    async def test_missing_url_is_400(self, legacy):
        resp = await _call_handler("/api/bg", legacy=legacy)
        assert _status_of(resp) == 400
        assert "url" in _body_of(resp).decode("utf-8")

    @pytest.mark.parametrize("legacy", [True, False])
    @pytest.mark.asyncio
    async def test_bad_scheme_is_400(self, legacy):
        target = quote("ftp://example.com/a.png", safe="")
        resp = await _call_handler(f"/api/bg?url={target}", legacy=legacy)
        assert _status_of(resp) == 400

    @pytest.mark.parametrize("legacy", [True, False])
    @pytest.mark.asyncio
    async def test_private_target_is_403(self, legacy):
        target = quote("http://169.254.169.254/latest/meta-data/", safe="")
        resp = await _call_handler(f"/api/bg?url={target}", legacy=legacy)
        assert _status_of(resp) == 403

    @pytest.mark.parametrize("legacy", [True, False])
    @pytest.mark.asyncio
    async def test_success_returns_image_and_miss_header(
        self, legacy, public_dns, monkeypatch
    ):
        _patch_upstream(monkeypatch, _image_response())
        target = quote(PUBLIC_URL, safe="")
        resp = await _call_handler(f"/api/bg?url={target}", legacy=legacy)
        headers = _headers_of(resp)
        assert _status_of(resp) == 200
        assert _body_of(resp) == PNG
        assert headers.get("Content-Type") == "image/png"
        assert headers.get("X-Bg-Cache") == "miss"
        assert "public" in headers.get("Cache-Control", "")
        assert "max-age=86400" in headers.get("Cache-Control", "")

    @pytest.mark.parametrize("legacy", [True, False])
    @pytest.mark.asyncio
    async def test_success_with_query_string_in_target(
        self, legacy, public_dns, monkeypatch
    ):
        """url 参数本身的 query（?a=1&b=2）必须靠 urlencode 完整传进来。"""
        target_url = "https://example.com/bg.png?a=1&b=2"
        _patch_upstream(monkeypatch, _image_response())
        resp = await _call_handler(
            f"/api/bg?url={quote(target_url, safe='')}", legacy=legacy
        )
        assert _status_of(resp) == 200

    @pytest.mark.parametrize("legacy", [True, False])
    @pytest.mark.asyncio
    async def test_cache_hit_returns_hit_header(self, legacy, monkeypatch):
        _bg_cache_write(PUBLIC_URL, PNG, "image/png")

        def _boom(*args, **kwargs):
            raise AssertionError("缓存命中不应触网")

        monkeypatch.setattr(relay.httpx, "AsyncClient", _boom)
        resp = await _call_handler(
            f"/api/bg?url={quote(PUBLIC_URL, safe='')}", legacy=legacy
        )
        assert _status_of(resp) == 200
        assert _headers_of(resp).get("X-Bg-Cache") == "hit"

    @pytest.mark.parametrize("legacy", [True, False])
    @pytest.mark.asyncio
    async def test_refresh_forces_refetch(self, legacy, public_dns, monkeypatch):
        _bg_cache_write(PUBLIC_URL, b"old-body", "image/png")
        new_body = PNG + b"fresh"
        client = _patch_upstream(
            monkeypatch,
            _FakeUpstreamResponse(
                status_code=200,
                headers={"Content-Type": "image/png"},
                chunks=[new_body],
            ),
        )
        resp = await _call_handler(
            f"/api/bg?url={quote(PUBLIC_URL, safe='')}&refresh=1", legacy=legacy
        )
        assert _status_of(resp) == 200
        assert _body_of(resp) == new_body
        assert _headers_of(resp).get("X-Bg-Cache") == "miss"
        assert client.urls == [PUBLIC_URL]

    @pytest.mark.parametrize("legacy", [True, False])
    @pytest.mark.asyncio
    async def test_non_image_is_502(self, legacy, public_dns, monkeypatch):
        _patch_upstream(
            monkeypatch,
            _FakeUpstreamResponse(
                status_code=200,
                headers={"Content-Type": "text/html"},
                chunks=[b"<html></html>"],
            ),
        )
        resp = await _call_handler(
            f"/api/bg?url={quote(PUBLIC_URL, safe='')}", legacy=legacy
        )
        assert _status_of(resp) == 502

    @pytest.mark.parametrize("legacy", [True, False])
    @pytest.mark.asyncio
    async def test_oversize_is_502(self, legacy, public_dns, monkeypatch):
        half = relay._BG_MAX_BYTES // 2 + 1
        _patch_upstream(
            monkeypatch,
            _FakeUpstreamResponse(
                status_code=200,
                headers={"Content-Type": "image/png"},
                chunks=[b"x" * half, b"x" * half],
            ),
        )
        resp = await _call_handler(
            f"/api/bg?url={quote(PUBLIC_URL, safe='')}", legacy=legacy
        )
        assert _status_of(resp) == 502

    @pytest.mark.asyncio
    async def test_index_route_still_works(self):
        """新增 /api/bg 分支不能影响原有页面路由。"""
        resp = await _call_handler("/", legacy=False)
        assert _status_of(resp) == 200
        assert b"<" in _body_of(resp)

    @pytest.mark.asyncio
    async def test_unknown_path_still_passes_through(self):
        assert await _call_handler("/favicon.ico", legacy=False) is None
