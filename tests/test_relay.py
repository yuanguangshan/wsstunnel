"""tests/test_relay.py — RelayState 核心逻辑测试"""

import asyncio
import pytest

from wsstunnel.relay import (
    RelayState,
    _parse_backend_auth,
    _forward_to_frontends,
    _forward_binary_to_frontends,
    _send_backend_list,
    _broadcast_backend_list,
)


# ──────────────────────────────────────────────
#  Mock WebSocket
# ──────────────────────────────────────────────

class MockWebSocket:
    """模拟 WebSocket 连接，记录发送的消息。"""

    def __init__(self):
        self.sent: list[str | bytes] = []
        self.closed: bool = False
        self._incoming: asyncio.Queue[str | bytes] = asyncio.Queue()

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        return await self._incoming.get()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True

    def __hash__(self):
        return id(self)

    def __eq__(self, other):
        return self is other


class FailingWebSocket(MockWebSocket):
    """模拟已断开的 WebSocket。"""

    async def send(self, message: str | bytes) -> None:
        raise ConnectionError("Connection closed")


class IterWebSocket(MockWebSocket):
    """支持 async for 迭代的 Mock WebSocket。"""

    def __init__(self, messages: list[str | bytes]):
        super().__init__()
        self._messages = list(messages)

    def __aiter__(self):
        self._iter = iter(self._messages)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


# ──────────────────────────────────────────────
#  RelayState 基础测试
# ──────────────────────────────────────────────

class TestRelayState:
    """RelayState 类的核心逻辑测试。"""

    def test_initial_state(self):
        state = RelayState(token="secret")
        assert state.backends == {}
        assert state.backend_modes == {}
        assert state.frontends == set()
        assert state.frontend_targets == {}
        assert state._counter == 0

    def test_next_backend_name(self):
        state = RelayState(token="secret")
        assert state._next_backend_name() == "backend-1"
        assert state._next_backend_name() == "backend-2"
        assert state._next_backend_name() == "backend-3"

    @pytest.mark.asyncio
    async def test_register_backend(self):
        state = RelayState(token="secret")
        ws = MockWebSocket()
        name = await state._register_backend(ws, "mybox", "pty")
        assert name == "mybox"
        assert "mybox" in state.backends
        assert state.backend_modes["mybox"] == "pty"

    @pytest.mark.asyncio
    async def test_register_backend_auto_name(self):
        state = RelayState(token="secret")
        ws = MockWebSocket()
        name = await state._register_backend(ws, None, "pipe")
        assert name == "backend-1"
        assert state._counter == 1

    @pytest.mark.asyncio
    async def test_unregister_backend(self):
        state = RelayState(token="secret")
        ws = MockWebSocket()
        await state._register_backend(ws, "mybox", "pty")
        await state._unregister_backend("mybox")
        assert "mybox" not in state.backends
        assert "mybox" not in state.backend_modes

    @pytest.mark.asyncio
    async def test_unregister_clears_frontend_targets(self):
        state = RelayState(token="secret")
        backend_ws = MockWebSocket()
        frontend_ws = MockWebSocket()
        await state._register_backend(backend_ws, "mybox", "pty")
        state.frontends.add(frontend_ws)
        state.frontend_targets[frontend_ws] = "mybox"
        await state._unregister_backend("mybox")
        assert state.frontend_targets[frontend_ws] is None

    @pytest.mark.asyncio
    async def test_register_frontend(self):
        state = RelayState(token="secret")
        ws = IterWebSocket([])  # 立即结束迭代
        await state._register_frontend(ws)
        assert ws not in state.frontends  # 循环结束后应已注销
        assert ws not in state.frontend_targets

    @pytest.mark.asyncio
    async def test_unregister_frontend(self):
        state = RelayState(token="secret")
        ws = MockWebSocket()
        state.frontends.add(ws)
        state.frontend_targets[ws] = "mybox"
        state._unregister_frontend(ws)
        assert ws not in state.frontends
        assert ws not in state.frontend_targets

    # ── resolve_target ──

    @pytest.mark.asyncio
    async def test_resolve_target_auto(self):
        """前端没有显式选择时，自动路由到第一个后端。"""
        state = RelayState(token="secret")
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()
        frontend = MockWebSocket()
        await state._register_backend(ws1, "box1", "pty")
        await state._register_backend(ws2, "box2", "pipe")
        state.frontends.add(frontend)
        state.frontend_targets[frontend] = None
        result = state._resolve_target(frontend)
        assert result is not None
        assert result[0] == "box1"

    @pytest.mark.asyncio
    async def test_resolve_target_explicit(self):
        """前端显式选择后端。"""
        state = RelayState(token="secret")
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()
        frontend = MockWebSocket()
        await state._register_backend(ws1, "box1", "pty")
        await state._register_backend(ws2, "box2", "pipe")
        state.frontends.add(frontend)
        state.frontend_targets[frontend] = "box2"
        result = state._resolve_target(frontend)
        assert result is not None
        assert result[0] == "box2"

    @pytest.mark.asyncio
    async def test_resolve_target_no_backends(self):
        state = RelayState(token="secret")
        frontend = MockWebSocket()
        state.frontends.add(frontend)
        result = state._resolve_target(frontend)
        assert result is None


# ──────────────────────────────────────────────
#  前端消息路由测试
# ──────────────────────────────────────────────

class TestRelayMessageRouting:
    """前端消息路由逻辑测试。"""

    @pytest.mark.asyncio
    async def test_handle_list(self):
        state = RelayState(token="secret")
        backend_ws = MockWebSocket()
        frontend = MockWebSocket()
        await state._register_backend(backend_ws, "mybox", "pty")
        state.frontends.add(frontend)
        state.frontend_targets[frontend] = "mybox"
        await state._handle_list(frontend)
        assert len(frontend.sent) == 1
        assert "mybox" in frontend.sent[0]
        assert "pty" in frontend.sent[0]

    @pytest.mark.asyncio
    async def test_handle_use_switch(self):
        state = RelayState(token="secret")
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()
        frontend = MockWebSocket()
        await state._register_backend(ws1, "box1", "pty")
        await state._register_backend(ws2, "box2", "pipe")
        state.frontends.add(frontend)
        state.frontend_targets[frontend] = None
        await state._handle_use(frontend, "USE box2")
        assert state.frontend_targets[frontend] == "box2"
        assert any("Switched to backend: box2" in str(m) for m in frontend.sent)

    @pytest.mark.asyncio
    async def test_handle_use_show_current(self):
        state = RelayState(token="secret")
        backend_ws = MockWebSocket()
        frontend = MockWebSocket()
        await state._register_backend(backend_ws, "mybox", "pty")
        state.frontends.add(frontend)
        state.frontend_targets[frontend] = "mybox"
        await state._handle_use(frontend, "USE")
        assert any("Current backend: mybox" in str(m) for m in frontend.sent)

    @pytest.mark.asyncio
    async def test_handle_use_nonexistent(self):
        state = RelayState(token="secret")
        frontend = MockWebSocket()
        state.frontends.add(frontend)
        await state._handle_use(frontend, "USE ghost")
        assert any("not found" in str(m) for m in frontend.sent)

    @pytest.mark.asyncio
    async def test_handle_at_command(self):
        state = RelayState(token="secret")
        backend_ws = MockWebSocket()
        frontend = MockWebSocket()
        await state._register_backend(backend_ws, "mybox", "pty")
        state.frontends.add(frontend)
        await state._handle_at_cmd(frontend, "@mybox whoami")
        assert "whoami" in backend_ws.sent

    @pytest.mark.asyncio
    async def test_handle_at_command_no_space(self):
        state = RelayState(token="secret")
        frontend = MockWebSocket()
        state.frontends.add(frontend)
        await state._handle_at_cmd(frontend, "@mybox")
        assert any("Usage:" in str(m) for m in frontend.sent)

    @pytest.mark.asyncio
    async def test_handle_at_command_nonexistent(self):
        state = RelayState(token="secret")
        frontend = MockWebSocket()
        state.frontends.add(frontend)
        await state._handle_at_cmd(frontend, "@ghost cmd")
        assert any("not found" in str(m) for m in frontend.sent)

    @pytest.mark.asyncio
    async def test_handle_control_resize(self):
        state = RelayState(token="secret")
        backend_ws = MockWebSocket()
        frontend = MockWebSocket()
        await state._register_backend(backend_ws, "mybox", "pty")
        state.frontends.add(frontend)
        state.frontend_targets[frontend] = "mybox"
        await state._handle_control(frontend, "__RESIZE:50,200")
        assert "__RESIZE:50,200" in backend_ws.sent

    @pytest.mark.asyncio
    async def test_handle_control_signal(self):
        state = RelayState(token="secret")
        backend_ws = MockWebSocket()
        frontend = MockWebSocket()
        await state._register_backend(backend_ws, "mybox", "pty")
        state.frontends.add(frontend)
        state.frontend_targets[frontend] = "mybox"
        await state._handle_control(frontend, "__SIGNAL:SIGINT")
        assert "__SIGNAL:SIGINT" in backend_ws.sent

    @pytest.mark.asyncio
    async def test_send_to_current_backend(self):
        state = RelayState(token="secret")
        backend_ws = MockWebSocket()
        frontend = MockWebSocket()
        await state._register_backend(backend_ws, "mybox", "pty")
        state.frontends.add(frontend)
        state.frontend_targets[frontend] = "mybox"
        await state._send_to_current_backend(frontend, "whoami")
        assert "whoami" in backend_ws.sent

    @pytest.mark.asyncio
    async def test_send_to_current_backend_no_backends(self):
        state = RelayState(token="secret")
        frontend = MockWebSocket()
        state.frontends.add(frontend)
        await state._send_to_current_backend(frontend, "whoami")
        assert any("No backends" in str(m) for m in frontend.sent)

    @pytest.mark.asyncio
    async def test_forward_binary_to_backend(self):
        state = RelayState(token="secret")
        backend_ws = MockWebSocket()
        frontend = MockWebSocket()
        await state._register_backend(backend_ws, "mybox", "pty")
        state.frontends.add(frontend)
        state.frontend_targets[frontend] = "mybox"
        await state._forward_binary_to_backend(frontend, b"\x03")
        assert b"\x03" in backend_ws.sent


# ──────────────────────────────────────────────
#  前端广播测试
# ──────────────────────────────────────────────

class TestBroadcastFunctions:
    """前端广播辅助函数测试。"""

    @pytest.mark.asyncio
    async def test_forward_text_with_tag(self):
        ws = MockWebSocket()
        frontends = {ws}
        await _forward_to_frontends(frontends, "output", tag="mybox")
        assert "[@mybox] output" in ws.sent

    @pytest.mark.asyncio
    async def test_forward_text_without_tag(self):
        ws = MockWebSocket()
        frontends = {ws}
        await _forward_to_frontends(frontends, "output")
        assert "output" in ws.sent

    @pytest.mark.asyncio
    async def test_forward_removes_dead_connections(self):
        ws_ok = MockWebSocket()
        ws_dead = FailingWebSocket()
        frontends = {ws_ok, ws_dead}
        await _forward_to_frontends(frontends, "output")
        assert ws_dead not in frontends
        assert ws_ok in frontends

    @pytest.mark.asyncio
    async def test_forward_binary(self):
        ws = MockWebSocket()
        frontends = {ws}
        await _forward_binary_to_frontends(frontends, b"\x01\x02")
        assert b"\x01\x02" in ws.sent

    @pytest.mark.asyncio
    async def test_send_backend_list_with_backends(self):
        ws = MockWebSocket()
        backends = {"box1": MockWebSocket(), "box2": MockWebSocket()}
        modes = {"box1": "pty", "box2": "pipe"}
        await _send_backend_list(ws, backends, modes, current="box1")
        msg = ws.sent[0]
        assert "box1(pty) *" in msg
        assert "box2(pipe)" in msg
        assert "Current: box1" in msg

    @pytest.mark.asyncio
    async def test_send_backend_list_empty(self):
        ws = MockWebSocket()
        await _send_backend_list(ws, {})
        assert "No backends" in ws.sent[0]

    @pytest.mark.asyncio
    async def test_broadcast_backend_list(self):
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()
        frontends = {ws1, ws2}
        backends = {"box1": MockWebSocket()}
        modes = {"box1": "pty"}
        targets = {ws1: "box1", ws2: None}
        await _broadcast_backend_list(frontends, backends, modes, targets)
        assert len(ws1.sent) > 0
        assert len(ws2.sent) > 0
