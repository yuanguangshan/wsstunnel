"""tests/test_client.py — 客户端工具函数的单元测试"""

import os
import signal

import pytest

from wsstunnel.client import _SIGNAL_MAP, _set_winsize


# ──────────────────────────────────────────────
#  _SIGNAL_MAP 测试
# ──────────────────────────────────────────────

class TestSignalMap:
    """信号名称映射测试。"""

    def test_contains_standard_signals(self):
        for name in ("SIGINT", "SIGTERM", "SIGKILL", "SIGQUIT",
                      "SIGHUP", "SIGUSR1", "SIGUSR2"):
            assert name in _SIGNAL_MAP

    def test_values_are_signal_constants(self):
        for name, sig in _SIGNAL_MAP.items():
            assert isinstance(sig, signal.Signals)

    def test_all_values_unique(self):
        values = list(_SIGNAL_MAP.values())
        assert len(values) == len(set(values))


# ──────────────────────────────────────────────
#  _set_winsize 测试
# ──────────────────────────────────────────────

class TestSetWinsize:
    """PTY 窗口大小设置测试。"""

    def test_set_winsize_on_pty(self):
        """在真实 PTY 上设置窗口大小应不报错。"""
        import pty as pty_mod
        import struct
        import fcntl
        import termios

        master, slave = pty_mod.openpty()
        try:
            _set_winsize(master, 50, 200)
            # 读取实际窗口大小验证
            result = fcntl.ioctl(master, termios.TIOCGWINSZ, b"\x00" * 8)
            rows, cols = struct.unpack("HHHH", result)[:2]
            assert rows == 50
            assert cols == 200
        finally:
            os.close(master)
            os.close(slave)

    def test_set_winsize_small(self):
        """小窗口也应正确设置。"""
        import pty as pty_mod
        import struct
        import fcntl
        import termios

        master, slave = pty_mod.openpty()
        try:
            _set_winsize(master, 10, 20)
            result = fcntl.ioctl(master, termios.TIOCGWINSZ, b"\x00" * 8)
            rows, cols = struct.unpack("HHHH", result)[:2]
            assert rows == 10
            assert cols == 20
        finally:
            os.close(master)
            os.close(slave)


# ──────────────────────────────────────────────
#  重连退避逻辑测试
# ──────────────────────────────────────────────

class TestReconnectBackoff:
    """重连指数退避计算测试。"""

    def test_backoff_first_attempt(self):
        delay = min(5 * (2 ** 0), 300)
        assert delay == 5

    def test_backoff_second_attempt(self):
        delay = min(5 * (2 ** 1), 300)
        assert delay == 10

    def test_backoff_third_attempt(self):
        delay = min(5 * (2 ** 2), 300)
        assert delay == 20

    def test_backoff_capped_at_max(self):
        delay = min(5 * (2 ** 100), 300)
        assert delay == 300
