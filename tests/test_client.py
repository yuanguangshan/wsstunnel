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


# ──────────────────────────────────────────────
#  PTY 子进程 fd 继承（回归：v1.0.20）
# ──────────────────────────────────────────────

class TestPtyChildFds:
    """子进程不得继承 PTY master。

    v0.18.x 曾用 ``pass_fds=(master_fd,)`` 把 master 传给 shell，导致
    "PTY 只可能由父进程关闭"这一前提失效：父进程 ``os.close(master_fd)``
    之后 PTY 仍被续命，读端永远等不到 HUP/EIO。表现是硬断线后
    ``tmux -u new-session`` 之类的全屏前端永久滞留（ppid=1、fd0 指向
    已失效的 /dev/pts/N），随重连次数线性累积。
    """

    def test_popen_does_not_pass_master_fd(self, monkeypatch):
        import threading

        import websocket

        from wsstunnel import client as client_mod

        captured = {}

        class _FakePopen:
            """只记录本次 Popen 的参数，不真正 spawn。"""

            def __init__(self, argv, **kwargs):
                captured["argv"] = argv
                captured["kwargs"] = kwargs
                self.pid = 424242

            def wait(self, timeout=None):
                return 0

        class _FakeWS:
            def recv(self):
                # 只跑一轮主循环：立刻模拟连接断开
                raise websocket.WebSocketConnectionClosedException()

            def send_binary(self, data):
                pass

            def close(self):
                pass

        monkeypatch.setattr(client_mod.subprocess, "Popen", _FakePopen)
        # 别真的对 424242 发信号
        monkeypatch.setattr(client_mod, "_terminate_process", lambda *a, **k: None)

        client_mod._run_pty_mode(_FakeWS(), "/bin/sh", threading.Event())

        assert captured, "Popen 未被调用，测试无效"
        assert "pass_fds" not in captured["kwargs"], (
            "PTY master 不能传给子进程：master 的副本会让 PTY 在父进程关闭后"
            "继续存活，断线时前台程序收不到 HUP，变成永久孤儿进程"
        )


# ──────────────────────────────────────────────
#  子 shell 环境 / pipe 模式输入（v1.0.21）
# ──────────────────────────────────────────────

class TestShellEnv:
    """spawn 子 shell 时的 TERM 兜底。"""

    def test_dumb_term_falls_back(self, monkeypatch):
        from wsstunnel import client as client_mod

        monkeypatch.setenv("TERM", "dumb")
        assert client_mod._shell_env()["TERM"] == "xterm-256color"

    def test_missing_term_falls_back(self, monkeypatch):
        from wsstunnel import client as client_mod

        monkeypatch.delenv("TERM", raising=False)
        assert client_mod._shell_env()["TERM"] == "xterm-256color"

    def test_good_term_untouched(self, monkeypatch):
        from wsstunnel import client as client_mod

        monkeypatch.setenv("TERM", "screen-256color")
        assert client_mod._shell_env()["TERM"] == "screen-256color"

    def test_other_env_preserved(self, monkeypatch):
        from wsstunnel import client as client_mod

        monkeypatch.setenv("TERM", "dumb")
        monkeypatch.setenv("WSB_PROBE", "42")
        assert client_mod._shell_env()["WSB_PROBE"] == "42"


class TestPipeModeBinaryInput:
    """pipe 模式收到二进制帧不得崩掉整条连接。"""

    def test_binary_frame_is_decoded(self, monkeypatch):
        import threading

        import websocket

        from wsstunnel import client as client_mod

        written = []

        class _Stdin:
            def write(self, b):
                written.append(b)
                return len(b)

            def flush(self):
                pass

        class _Stdout:
            def read(self, n):
                return b""        # 读到 EOF，转发线程立即退出

        class _Proc:
            pid = 424242

            def __init__(self):
                self.stdin = _Stdin()
                self.stdout = _Stdout()

        monkeypatch.setattr(client_mod.subprocess, "Popen", lambda *a, **k: _Proc())
        monkeypatch.setattr(client_mod, "_terminate_process", lambda *a, **k: None)

        class _WS:
            def __init__(self):
                self.calls = 0

            def recv(self):
                self.calls += 1
                if self.calls == 1:
                    return b"echo hi\r"
                raise websocket.WebSocketConnectionClosedException()

            def send(self, *a, **k):
                pass

            def close(self):
                pass

        client_mod._run_pipe_mode(_WS(), "/bin/sh", threading.Event())

        assert written, "命令没有被写进 shell"
        assert written[0] == b"echo hi\r\n"


# ──────────────────────────────────────────────
#  resize 通知（v1.0.22）
# ──────────────────────────────────────────────

class TestNotifyWinsize:
    """TIOCSWINSZ 之后必须补 SIGWINCH，否则 tmux 收不到。"""

    def test_sends_sigwinch_to_process_group(self, monkeypatch):
        from wsstunnel import client as client_mod

        calls = []

        class _Proc:
            pid = 4242

        monkeypatch.setattr(client_mod.os, "killpg",
                            lambda pid, sig: calls.append((pid, sig)))
        client_mod._notify_winsize(_Proc())

        assert calls == [(4242, signal.SIGWINCH)]

    def test_swallows_missing_process(self, monkeypatch):
        from wsstunnel import client as client_mod

        class _Proc:
            pid = 4242

        def _boom(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(client_mod.os, "killpg", _boom)
        client_mod._notify_winsize(_Proc())   # 不应抛异常

    def test_resize_message_triggers_signal(self, monkeypatch):
        """真正走一遍 __RESIZE 分支，确认 set_winsize 与 SIGWINCH 都发生。"""
        import threading

        import websocket

        from wsstunnel import client as client_mod

        resized = []
        signalled = []

        class _Proc:
            pid = 4242

            def wait(self, timeout=None):
                return 0

        class _WS:
            def __init__(self):
                self.calls = 0

            def recv(self):
                self.calls += 1
                if self.calls == 1:
                    return "__RESIZE:30,100"
                raise websocket.WebSocketConnectionClosedException()

            def send_binary(self, data):
                pass

            def close(self):
                pass

        monkeypatch.setattr(client_mod.subprocess, "Popen", lambda *a, **k: _Proc())
        monkeypatch.setattr(client_mod, "_terminate_process", lambda *a, **k: None)
        monkeypatch.setattr(client_mod, "_set_winsize",
                            lambda fd, r, c: resized.append((r, c)))
        monkeypatch.setattr(client_mod.os, "killpg",
                            lambda pid, sig: signalled.append((pid, sig)))

        client_mod._run_pty_mode(_WS(), "/bin/sh", threading.Event())

        # 第一次是 spawn 时的初始尺寸，第二次才是 __RESIZE
        assert (30, 100) in resized, resized
        assert (4242, signal.SIGWINCH) in signalled, signalled
