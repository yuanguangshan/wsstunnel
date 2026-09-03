"""tests/test_p0_fixes.py — 源码解析文档(第十节)缺陷修复回归测试

对应修复项：
- P0  后端连接 IP 计数只增不减（_register_backend 不写 _client_info）
- P1-1 防爆破锁定固定 3s → 指数退避
- P1-3 relay 无主动心跳 → 后端心跳超时 watchdog
- P2-2 前端 __PING__ 无应答（会被当命令转发进 shell）
"""

from __future__ import annotations

import asyncio
import time

import pytest

from wsstunnel.relay import RelayState, _WS_MAX_FRAME
from wsstunnel.security import BruteForceGuard

from .test_relay import IterWebSocket, MockWebSocket


class TrackedWS(IterWebSocket):
    """带 remote_address 的后端连接模拟（触发非本地 IP 计数路径）。

    handler 首帧走 ``recv()``（_incoming 队列），后续消息走 ``async for``
    （_messages 迭代）——两条通道都要喂对，否则 recv 等 30s 超时。
    """

    remote_address = ("203.0.113.7", 51000)

    def __init__(self, messages: list[str | bytes]):
        super().__init__(messages)
        if messages:
            self._incoming.put_nowait(messages[0])   # 首帧 → recv()
            self._messages = messages[1:]            # 其余 → async for


# ──────────────────────────────────────────────
#  P0：后端 IP 连接计数必须可释放
# ──────────────────────────────────────────────

class TestBackendIPSlotRelease:
    """直连 relay 的后端重连 N 次后不得被自身配额锁死。"""

    @pytest.mark.asyncio
    async def test_backend_reconnects_do_not_leak_ip_slots(self):
        state = RelayState(None)
        limit = state._max_connections_per_ip  # 10
        # 重连 limit + 5 次：若计数泄漏，第 11 次起被 1008 拒绝
        for round_no in range(limit + 5):
            ws = TrackedWS([f"IAM_BACKEND:bk{round_no}:pty"])
            await state.handler(ws)
            assert not ws.closed, (
                f"round {round_no}: backend rejected — IP slot leaked"
            )
        assert state._max_per_ip.get(TrackedWS.remote_address[0], 0) == 0, \
            "backend disconnects must release per-IP connection slots"

    @pytest.mark.asyncio
    async def test_backend_and_frontend_slots_independent(self):
        state = RelayState(None)
        bw = TrackedWS(["IAM_BACKEND:bk:pty"])
        await state.handler(bw)
        # 后端断开后计数归零，前端仍可正常使用同 IP 配额
        assert state._max_per_ip.get(TrackedWS.remote_address[0], 0) == 0


# ──────────────────────────────────────────────
#  P1-1：防爆破指数退避
# ──────────────────────────────────────────────

class TestBruteForceExponentialLockout:
    def test_lockout_doubles_on_repeat(self):
        g = BruteForceGuard(max_attempts=3, lockout_sec=3.0)
        seq = []
        import wsstunnel.security as sec
        orig = sec.time.time
        clock = [1000.0]
        sec.time.time = lambda: clock[0]
        try:
            for _ in range(3):
                g.record_failure("1.2.3.4")
            seq.append(g._locked_until["1.2.3.4"] - clock[0])  # 3s
            clock[0] += 3.1  # 锁过期后再失败 → 6s
            g.record_failure("1.2.3.4")
            seq.append(g._locked_until["1.2.3.4"] - clock[0])
            clock[0] += 6.1
            g.record_failure("1.2.3.4")
            seq.append(g._locked_until["1.2.3.4"] - clock[0])
        finally:
            sec.time.time = orig
        assert seq == [3.0, 6.0, 12.0]

    def test_lockout_capped(self):
        g = BruteForceGuard(max_attempts=1, lockout_sec=3.0)
        import wsstunnel.security as sec
        orig = sec.time.time
        sec.time.time = lambda: 1000.0
        try:
            for _ in range(30):  # 远超封顶所需的连续失败
                g.record_failure("9.9.9.9")
        finally:
            sec.time.time = orig
        assert g._locked_until["9.9.9.9"] - 1000.0 == g._LOCKOUT_MAX


# ──────────────────────────────────────────────
#  P1-3：后端心跳超时 watchdog
# ──────────────────────────────────────────────

class TestHeartbeatWatchdog:
    @pytest.mark.asyncio
    async def test_stale_backend_gets_closed(self):
        state = RelayState(None)
        state._watch_interval = 0.01   # 测试用快速扫描
        state._backend_timeout = 0.05
        bw = MockWebSocket()
        state.backends["bk"] = bw
        state._backend_last_seen["bk"] = time.time() - 999  # 远超超时
        task = asyncio.create_task(state.heartbeat_watchdog())
        await asyncio.sleep(0.1)
        task.cancel()
        assert bw.closed, "stale backend must be closed by watchdog"
        assert "bk" not in state._backend_last_seen

    @pytest.mark.asyncio
    async def test_fresh_backend_untouched(self):
        state = RelayState(None)
        state._watch_interval = 0.01
        state._backend_timeout = 90.0
        bw = MockWebSocket()
        state.backends["bk"] = bw
        state._backend_last_seen["bk"] = time.time()
        task = asyncio.create_task(state.heartbeat_watchdog())
        await asyncio.sleep(0.05)
        task.cancel()
        assert not bw.closed
        assert state._backend_last_seen.get("bk") is not None

    @pytest.mark.asyncio
    async def test_ping_refreshes_last_seen(self):
        state = RelayState(None)

        class PingProbe(TrackedWS):
            """在 __PONG__ 应答瞬间快照 last_seen（注销前）。"""

            def __init__(self, messages):
                super().__init__(messages)
                self.last_seen_at_pong = None

            async def send(self, message):
                if message == "__PONG__":
                    self.last_seen_at_pong = state._backend_last_seen.get("bkp")
                await super().send(message)

        bw = PingProbe(["IAM_BACKEND:bkp:pty", "__PING__"])  # 无 token 格式: name:mode
        await state.handler(bw)
        # 转发循环收到 __PING__ 必须刷新 last_seen（否则会被 watchdog 误杀）
        assert bw.last_seen_at_pong and bw.last_seen_at_pong > 0
        # 连接注销后条目应清理，watchdog 不再监控已下线后端
        assert "bkp" not in state._backend_last_seen


# ──────────────────────────────────────────────
#  P2-2：前端 __PING__ 应答
# ──────────────────────────────────────────────

class TestFrontendPing:
    @pytest.mark.asyncio
    async def test_frontend_ping_answered_not_forwarded(self):
        state, fw, bw = _state_with_backend()
        await state._handle_frontend_msg(fw, "__PING__")
        assert "__PONG__" in fw.sent
        # 关键反例：PING 不得作为命令转发给后端 shell
        assert "__PING__" not in bw.sent

    @pytest.mark.asyncio
    async def test_frontend_ping_allowed_for_readonly(self):
        from wsstunnel.security import Role
        state, fw, bw = _state_with_backend(Role.READONLY)
        await state._handle_frontend_msg(fw, "__PING__")
        assert "__PONG__" in fw.sent


def _state_with_backend(role=None):
    from wsstunnel.security import Role
    role = Role.ADMIN if role is None else role
    state = RelayState(None)
    backend = MockWebSocket()
    frontend = MockWebSocket()
    state._client_info[frontend] = {
        "id": "c1", "ip": "1.2.3.4", "role": role, "connected_at": time.time(),
    }
    state.frontends.add(frontend)
    state.frontend_targets[frontend] = None
    state.backends["bk"] = backend
    state.backend_modes["bk"] = "pipe"
    state.backend_connected_at["bk"] = time.time()
    return state, frontend, backend


# ──────────────────────────────────────────────
#  P2-3：帧上限常量存在且与分块约束自洽
# ──────────────────────────────────────────────

def test_ws_max_frame_covers_file_chunk():
    from wsstunnel.client import _FILE_CHUNK_SIZE
    import math
    b64_size = math.ceil(_FILE_CHUNK_SIZE / 3) * 4
    assert b64_size * 2 < _WS_MAX_FRAME  # 留 2 倍余量
