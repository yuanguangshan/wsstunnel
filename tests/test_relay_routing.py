"""tests/test_relay_routing.py — relay 前端消息路由与文件传输回归测试

覆盖 v1.0.1 修复的问题：
1. FILE 角色文件传输被末尾 ADMIN 检查拦截（fall-through bug）
2. --deny-cmd 从未执行（DenyList.is_denied 零调用）
3. 并发广播下集合迭代修改崩溃（Set changed size during iteration）
4. 后端重连竞态：旧 handler 注销误删新后端注册
5. token-file 带时区的 expires 触发 TypeError
6. CLI put/get 与 relay 确认时序（[Info] 噪声消息导致误报 Upload rejected）
"""

from __future__ import annotations

import asyncio
import base64
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
import websockets

import wsstunnel.client as client_mod
from wsstunnel.client import _resolve_path
from wsstunnel.relay import RelayState, _BACKEND_NAME_RE, _forward_to_frontends
from wsstunnel.security import BruteForceGuard, DenyList, Role, TokenInfo, TokenManager
from wsstunnel.cli import _recv_until

from .test_relay import MockWebSocket


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


# ──────────────────────────────────────────────
#  1. FILE 角色路由修复
# ──────────────────────────────────────────────

def _make_state_with(role: Role) -> tuple[RelayState, MockWebSocket, MockWebSocket]:
    """构造带指定角色的前端 + 一个已注册后端的 RelayState。"""
    state = RelayState(None)
    backend_ws = MockWebSocket()
    frontend = MockWebSocket()
    state._client_info[frontend] = {
        "id": "c1", "ip": "1.2.3.4", "role": role, "connected_at": time.time(),
    }
    state.frontends.add(frontend)
    state.frontend_targets[frontend] = None
    # 同步注册（跳过通知/广播的 await 不可行，直接手工登记）
    state.backends["bk"] = backend_ws
    state.backend_modes["bk"] = "pipe"
    state.backend_connected_at["bk"] = time.time()
    return state, frontend, backend_ws


class TestFileRoleRouting:
    """FILE 角色用户必须能真正执行文件传输。"""

    @pytest.mark.asyncio
    async def test_file_role_upload_reaches_backend(self):
        state, fw, bw = _make_state_with(Role.FILE)
        await state._handle_frontend_msg(fw, f"__FILE_BEGIN:{b64('/tmp/x.txt')}:100")
        assert any("__FILE_BEGIN" in str(m) for m in bw.sent)

    @pytest.mark.asyncio
    async def test_file_role_chunk_reaches_backend(self):
        state, fw, bw = _make_state_with(Role.FILE)
        await state._handle_frontend_msg(fw, f"__FILE_CHUNK:{b64('/tmp/x.txt')}:0:AAAA")
        assert any("__FILE_CHUNK" in str(m) for m in bw.sent)

    @pytest.mark.asyncio
    async def test_file_role_download_reaches_backend(self):
        state, fw, bw = _make_state_with(Role.FILE)
        await state._handle_frontend_msg(fw, f"__FILE_DOWNLOAD:{b64('/tmp/x.txt')}")
        assert any("__FILE_DOWNLOAD" in str(m) for m in bw.sent)

    @pytest.mark.asyncio
    async def test_readonly_still_blocked(self):
        state, fw, bw = _make_state_with(Role.READONLY)
        await state._handle_frontend_msg(fw, f"__FILE_BEGIN:{b64('/tmp/x.txt')}:100")
        assert not any("FILE" in str(m) for m in bw.sent)
        assert any("Permission denied" in str(m) for m in fw.sent)

    @pytest.mark.asyncio
    async def test_admin_upload_reaches_backend(self):
        """修复前 ADMIN 依赖意外 fall-through；修复后显式转发，行为保持。"""
        state, fw, bw = _make_state_with(Role.ADMIN)
        await state._handle_frontend_msg(fw, f"__FILE_BEGIN:{b64('/tmp/x.txt')}:100")
        assert any("__FILE_BEGIN" in str(m) for m in bw.sent)

    @pytest.mark.asyncio
    async def test_oversize_upload_rejected_for_file_role(self):
        state, fw, bw = _make_state_with(Role.FILE)
        big = state._max_file_size + 1
        await state._handle_frontend_msg(fw, f"__FILE_BEGIN:{b64('/tmp/x.txt')}:{big}")
        assert not any("FILE" in str(m) for m in bw.sent)
        assert any("File too large" in str(m) for m in fw.sent)


# ──────────────────────────────────────────────
#  2. --deny-cmd 执行
# ──────────────────────────────────────────────

class TestDenyCmdEnforcement:
    """命令黑名单必须在 relay 侧真实拦截。"""

    @pytest.mark.asyncio
    async def test_deny_normal_command(self):
        state, fw, bw = _make_state_with(Role.ADMIN)
        state.deny_list = DenyList(["rm"])
        await state._handle_frontend_msg(fw, "rm -rf /")
        assert not any("rm" in str(m) for m in bw.sent)
        assert any("blocked" in str(m).lower() for m in fw.sent)

    @pytest.mark.asyncio
    async def test_deny_at_command(self):
        state, fw, bw = _make_state_with(Role.ADMIN)
        state.deny_list = DenyList(["rm"])
        await state._handle_frontend_msg(fw, "@bk rm -rf /")
        assert not any("rm" in str(m) for m in bw.sent)
        assert any("blocked" in str(m).lower() for m in fw.sent)

    @pytest.mark.asyncio
    async def test_non_denied_command_passes(self):
        state, fw, bw = _make_state_with(Role.ADMIN)
        state.deny_list = DenyList(["rm"])
        await state._handle_frontend_msg(fw, "ls -la")
        assert any("ls -la" in str(m) for m in bw.sent)

    @pytest.mark.asyncio
    async def test_no_deny_list_passes_all(self):
        state, fw, bw = _make_state_with(Role.ADMIN)
        await state._handle_frontend_msg(fw, "rm -rf /")
        assert any("rm -rf /" in str(m) for m in bw.sent)


# ──────────────────────────────────────────────
#  3. 并发广播集合快照
# ──────────────────────────────────────────────

class TestBroadcastSetMutation:
    """广播期间前端集合被并发修改不应触发 RuntimeError。"""

    @pytest.mark.asyncio
    async def test_addition_during_broadcast_is_safe(self):
        frontends: set = set()
        late_joiner = MockWebSocket()

        class MutatingWebSocket(MockWebSocket):
            async def send(self, message):
                await asyncio.sleep(0)  # 制造 await 切换点
                frontends.add(late_joiner)  # 模拟广播期间新前端注册
                self.sent.append(message)

        frontends.add(MutatingWebSocket())
        await _forward_to_frontends(frontends, "hello")
        assert late_joiner in frontends

    @pytest.mark.asyncio
    async def test_removal_during_broadcast_is_safe(self):
        frontends: set = set()
        leaver = MockWebSocket()

        class MutatingWebSocket(MockWebSocket):
            async def send(self, message):
                await asyncio.sleep(0)
                frontends.discard(leaver)  # 模拟广播期间前端断开注销
                self.sent.append(message)

        frontends.add(MutatingWebSocket())
        frontends.add(leaver)
        await _forward_to_frontends(frontends, "hello")  # 修复前: RuntimeError
        assert leaver not in frontends


# ──────────────────────────────────────────────
#  4. 后端重连竞态
# ──────────────────────────────────────────────

class TestBackendReconnectRace:
    @pytest.mark.asyncio
    async def test_old_handler_cannot_evict_new_registration(self):
        state = RelayState(None)
        old_ws, new_ws = MockWebSocket(), MockWebSocket()
        await state._register_backend(old_ws, "bk", "pipe")
        # 新连接重连顶替（handler 中的顶替路径）
        await state._register_backend(new_ws, "bk", "pipe")
        # 旧 handler 随后醒来执行注销：身份不匹配，必须跳过
        await state._unregister_backend("bk", old_ws)
        assert state.backends.get("bk") is new_ws

    @pytest.mark.asyncio
    async def test_matching_identity_still_unregisters(self):
        state = RelayState(None)
        ws = MockWebSocket()
        await state._register_backend(ws, "bk", "pipe")
        await state._unregister_backend("bk", ws)
        assert "bk" not in state.backends

    @pytest.mark.asyncio
    async def test_unregister_without_ws_keeps_compat(self):
        state = RelayState(None)
        ws = MockWebSocket()
        await state._register_backend(ws, "bk", "pipe")
        await state._unregister_backend("bk")  # 旧调用方式（不传 ws）
        assert "bk" not in state.backends


# ──────────────────────────────────────────────
#  5. 后端名字符集校验 / token 时区 / 防爆破 TTL
# ──────────────────────────────────────────────

class TestBackendNameValidation:
    @pytest.mark.parametrize("name", ["web-1", "db_2", "a", "X" * 64, "node.1"])
    def test_valid_names(self, name):
        assert _BACKEND_NAME_RE.fullmatch(name)

    @pytest.mark.parametrize("name", ["", "-lead", ".dot", "a b", "<img>", "x" * 65, "a\nb"])
    def test_invalid_names(self, name):
        assert not _BACKEND_NAME_RE.fullmatch(name)


class TestTokenExpiryTimezones:
    def test_tz_aware_valid(self):
        mgr = TokenManager()
        mgr._tokens["t"] = TokenInfo(
            id="t", token="t", role=Role.ADMIN,
            expires=datetime.now(timezone.utc) + timedelta(days=1),
        )
        assert mgr.validate("t") is not None

    def test_tz_aware_expired(self):
        mgr = TokenManager()
        mgr._tokens["t"] = TokenInfo(
            id="t", token="t", role=Role.ADMIN,
            expires=datetime.now(timezone.utc) - timedelta(days=1),
        )
        assert mgr.validate("t") is None

    def test_naive_still_works(self):
        mgr = TokenManager()
        mgr._tokens["t"] = TokenInfo(
            id="t", token="t", role=Role.ADMIN,
            expires=datetime.now() + timedelta(days=1),
        )
        assert mgr.validate("t") is not None


class TestBruteForceTTL:
    def test_stale_failures_cleaned(self):
        bf = BruteForceGuard(max_attempts=2, lockout_sec=5)
        bf._failures["9.9.9.9"] = (1, time.time() - 999)  # 早已过期的失败记录
        bf._last_cleanup = time.time() - 999              # 强制触发清理窗口
        bf.record_failure("1.1.1.1")
        assert "9.9.9.9" not in bf._failures
        assert "1.1.1.1" in bf._failures


# ──────────────────────────────────────────────
#  6. 客户端路径解析
# ──────────────────────────────────────────────

class TestResolvePath:
    def setup_method(self):
        self._old_pid, self._old_cwd = client_mod._shell_pid, client_mod._cwd
        client_mod._shell_pid = None
        client_mod._cwd = "/base"

    def teardown_method(self):
        client_mod._shell_pid, client_mod._cwd = self._old_pid, self._old_cwd

    def test_absolute_untouched(self):
        assert _resolve_path("/etc/passwd") == "/etc/passwd"

    def test_relative_joins_tracked_cwd(self):
        assert _resolve_path("sub/file.txt") == "/base/sub/file.txt"

    def test_dot_relative(self):
        assert _resolve_path("./f.txt") == "/base/f.txt"

    def test_tilde_expands_to_home(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/u")
        assert _resolve_path("~") == "/home/u"
        assert _resolve_path("~/x/y") == "/home/u/x/y"

    def test_proc_failure_falls_back(self):
        client_mod._shell_pid = 2 ** 22  # 不存在的 pid，readlink 必败
        assert _resolve_path("f.txt") == "/base/f.txt"


class TestRecvUntil:
    """CLI 协议确认等待：跳过 [Info] 噪声、[Error] 快速失败。"""

    class FakeSyncWS:
        def __init__(self, msgs):
            self._msgs = list(msgs)
            self.timeouts: list[float] = []

        def settimeout(self, t):
            self.timeouts.append(t)

        def recv(self):
            return self._msgs.pop(0)

    def test_skips_info_noise(self):
        ws = self.FakeSyncWS([
            "[Info] Connected backends: bk1(pipe)",
            "__FILE_OK:QUEJD:=:100".replace("QUEJD:=:", ""),  # __FILE_OK:100
        ])
        assert _recv_until(ws, lambda m: m.startswith("__FILE_")) == "__FILE_OK:100"

    def test_error_fails_fast(self):
        ws = self.FakeSyncWS(["[Error] No backends connected. Use LIST to check."])
        with pytest.raises(RuntimeError, match="No backends"):
            _recv_until(ws, lambda m: m.startswith("__FILE_"))

    def test_error_not_fatal_when_disabled(self):
        ws = self.FakeSyncWS(["[Error] Backend 'x' not found", "done"])
        assert _recv_until(
            ws, lambda m: m.startswith("[Error]"), timeout=5, fatal_error=False
        ) == "[Error] Backend 'x' not found"


# ──────────────────────────────────────────────
#  7. 端到端（真实 websockets serve）
# ──────────────────────────────────────────────

async def _serve(state: RelayState) -> tuple[object, str]:
    server = await websockets.serve(state.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}"


class TestEndToEndRouting:
    @pytest.mark.asyncio
    async def test_file_role_upload_reaches_backend_live(self):
        """FILE 角色经真实 relay 上传：BEGIN 必须到达后端（v1.0.0 被拦截）。"""
        state = RelayState(None)
        state.token_manager._tokens["filer"] = TokenInfo(
            id="filer", token="filer", role=Role.FILE
        )
        server, url = await _serve(state)
        try:
            async with websockets.connect(url) as bk:
                await bk.send("IAM_BACKEND::bk1:pipe")
                await asyncio.sleep(0.2)
                async with websockets.connect(url + "?token=filer") as fw:
                    assert await asyncio.wait_for(fw.recv(), 3) == "AUTH_OK"
                    await fw.send(f"__FILE_BEGIN:{b64('/tmp/e2e.txt')}:100")
                    msg = await asyncio.wait_for(bk.recv(), 3)
                    assert "__FILE_BEGIN" in msg
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_broadcast_survives_concurrent_registration(self):
        """后端注册广播期间前端连入，不应打死后端 handler。"""
        state = RelayState(None)
        server, url = await _serve(state)
        try:
            async with websockets.connect(url) as bk:
                await bk.send("IAM_BACKEND::bk1:pipe")
                # 前端 A 先注册（订阅广播），前端 B 在广播 await 点上连入
                fw_a = await websockets.connect(url)
                await fw_a.send("AUTH:")
                assert await asyncio.wait_for(fw_a.recv(), 3) == "AUTH_OK"
                # 前端 A 此时收到首份后端列表
                await asyncio.wait_for(fw_a.recv(), 3)
                await asyncio.sleep(0.2)
                fw_b = await websockets.connect(url)
                await fw_b.send("AUTH:")
                assert await asyncio.wait_for(fw_b.recv(), 3) == "AUTH_OK"
                await asyncio.sleep(0.5)
                # 触发一次后端列表广播（注册第二个后端）
                async with websockets.connect(url) as bk2:
                    await bk2.send("IAM_BACKEND::bk2:pipe")
                    # 前端 A 必须仍活着并收到含两个后端的列表广播
                    msg = await asyncio.wait_for(fw_a.recv(), 3)
                    assert "bk1" in msg and "bk2" in msg
                fw_b.close()
                fw_a.close()
        finally:
            server.close()
            await server.wait_closed()


class _FakeBackendProtocol:
    """假后端：实现 put/get 所需的最小文件协议应答。"""

    def __init__(self, url: str, token: str = "tok"):
        self.url = url
        self.token = token
        self.thread: threading.Thread | None = None

    def start(self):
        ready = threading.Event()

        def _run():
            async def main():
                async with websockets.connect(self.url) as bk:
                    await bk.send(f"IAM_BACKEND:{self.token}:fake:pipe")
                    async for msg in bk:
                        if msg.startswith("__FILE_BEGIN:"):
                            _, b64p, size = msg.split(":", 2)
                            await bk.send(f"__FILE_OK:{b64p}:{size}")
                        elif msg.startswith("__FILE_END:"):
                            parts = msg.split(":", 2)
                            b64p = parts[1]
                            total = parts[2] if len(parts) > 2 else "?"
                            await bk.send(f"__FILE_DONE:{b64p}:{total}")
                        elif msg.startswith("__FILE_DOWNLOAD:"):
                            _, b64p = msg.split(":", 1)
                            data = b"e2e-download-content"
                            await bk.send(f"__FILE_BEGIN:{b64p}:{len(data)}")
                            await bk.send(
                                f"__FILE_CHUNK:{b64p}:0:{base64.b64encode(data).decode()}"
                            )
                            await bk.send(f"__FILE_END:{b64p}:{len(data)}")

            asyncio.run(main())
            ready.set()

        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()


class TestCliEndToEnd:
    """CLI put/get 走真实 relay + 假后端，验证确认时序修复。"""

    def _start_relay(self, deny_cmd=None) -> str:
        state = RelayState("tok")
        if deny_cmd:
            state.deny_list = DenyList(deny_cmd)
        ready = threading.Event()
        holder: dict = {}

        def _run():
            async def main():
                server = await websockets.serve(state.handler, "127.0.0.1", 0)
                holder["port"] = server.sockets[0].getsockname()[1]
                ready.set()
                await asyncio.Future()

            asyncio.run(main())

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        assert ready.wait(5), "relay did not start"
        return f"ws://127.0.0.1:{holder['port']}"

    def test_put_completes_despite_info_noise(self, tmp_path):
        """修复前：relay 的 [Info] 后端列表排在 __FILE_OK 前，put 误报 rejected。"""
        from click.testing import CliRunner
        from wsstunnel.cli import cli

        url = self._start_relay()
        backend = _FakeBackendProtocol(url)
        backend.start()
        time.sleep(0.3)

        local = tmp_path / "up.txt"
        local.write_text("hello wsstunnel")
        result = CliRunner().invoke(
            cli, ["put", "--server", url, "--token", "tok", str(local)]
        )
        assert result.exit_code == 0, result.output
        assert "Upload complete" in result.output

    def test_put_with_backend_no_long_stall(self, tmp_path):
        """修复前：--backend 的排空循环靠 120s 超时退出；修复后收到确认即走。"""
        from click.testing import CliRunner
        from wsstunnel.cli import cli

        url = self._start_relay()
        backend = _FakeBackendProtocol(url)
        backend.start()
        time.sleep(0.3)

        local = tmp_path / "up2.txt"
        local.write_text("x" * 1024)
        result = CliRunner().invoke(
            cli,
            ["put", "--server", url, "--token", "tok",
             "--backend", "fake", str(local)],
        )
        assert result.exit_code == 0, result.output
        assert "Switched to backend" in result.output

    def test_get_streams_to_file(self, tmp_path):
        from click.testing import CliRunner
        from wsstunnel.cli import cli

        url = self._start_relay()
        backend = _FakeBackendProtocol(url)
        backend.start()
        time.sleep(0.3)

        out = tmp_path / "down.txt"
        result = CliRunner().invoke(
            cli, ["get", "--server", url, "--token", "tok", "/remote/f.txt",
                  str(out)]
        )
        assert result.exit_code == 0, result.output
        assert out.read_bytes() == b"e2e-download-content"
        assert not out.with_suffix(out.suffix + ".part").exists()

    def test_put_deny_cmd_rejected_at_relay(self, tmp_path):
        """--deny-cmd 不影响文件协议消息；黑名单只拦命令（回归 #1）。"""
        from click.testing import CliRunner
        from wsstunnel.cli import cli

        url2 = self._start_relay(deny_cmd=["shutdown"])
        backend = _FakeBackendProtocol(url2)
        backend.start()
        time.sleep(0.3)

        local = tmp_path / "up3.txt"
        local.write_text("ok")
        result = CliRunner().invoke(
            cli, ["put", "--server", url2, "--token", "tok", str(local)]
        )
        assert result.exit_code == 0, result.output
