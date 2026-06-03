#!/usr/bin/env python3
"""
wsstunnel/client.py — WebSocket 后端客户端（容器端）

同步版本，使用 websocket-client 库以支持 HTTP 代理穿透。
支持两种模式:
  - PTY 模式（默认）: 使用伪终端，支持 vim/top/htop 等 TUI 程序
  - 管道模式（--no-pty）: 使用普通管道，仅支持行缓冲输出（向后兼容）
"""

from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import ssl
import struct
import subprocess
import termios
import threading
import time
import logging
from typing import Any
from urllib.parse import urlparse

import websocket

logger = logging.getLogger(__name__)

_RECONNECT_MAX_DELAY = 300  # 最大重连间隔 5 分钟
_HEARTBEAT_INTERVAL = 30    # 心跳间隔秒数
_PIPE_READ_BUF = 4096       # 管道模式读取缓冲区大小


# ──────────────────────────────────────────────
#  终端与信号工具
# ──────────────────────────────────────────────

def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """设置伪终端窗口大小。

    Args:
        fd: PTY master 文件描述符。
        rows: 行数。
        cols: 列数。
    """
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


# 标准信号名称到 signal 模块常量的映射
_SIGNAL_MAP: dict[str, signal.Signals] = {
    "SIGINT": signal.SIGINT,
    "SIGTERM": signal.SIGTERM,
    "SIGKILL": signal.SIGKILL,
    "SIGQUIT": signal.SIGQUIT,
    "SIGHUP": signal.SIGHUP,
    "SIGUSR1": signal.SIGUSR1,
    "SIGUSR2": signal.SIGUSR2,
}


def _send_signal(shell_proc: subprocess.Popen, sig_name: str) -> None:
    """向进程组发送信号。

    Args:
        shell_proc: shell 子进程。
        sig_name: 信号名称，如 ``"SIGINT"``。
    """
    sig = _SIGNAL_MAP.get(sig_name.upper())
    if sig is None:
        logger.warning(f"Unknown signal: {sig_name}")
        return
    try:
        # 向整个进程组发送信号（PID 取负值）
        os.killpg(shell_proc.pid, sig)
        logger.info(f"Sent {sig_name} to process group {shell_proc.pid}")
    except ProcessLookupError:
        logger.debug(f"Process group {shell_proc.pid} already exited")
    except PermissionError:
        logger.warning(f"Permission denied sending {sig_name} to pgid {shell_proc.pid}")


def _terminate_process(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """优雅终止子进程：先 SIGTERM，超时后 SIGKILL。

    Args:
        proc: 要终止的子进程。
        timeout: SIGTERM 后等待退出的秒数。
    """
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ──────────────────────────────────────────────
#  心跳
# ──────────────────────────────────────────────

def _heartbeat(ws: websocket.WebSocket, reconnect_event: threading.Event) -> None:
    """心跳线程：定期发送 ``__PING__``，失败时触发重连。

    Args:
        ws: WebSocket 连接实例。
        reconnect_event: 重连事件，``set()`` 时通知主线程重连。
    """
    while not reconnect_event.is_set():
        try:
            ws.send("__PING__")
            time.sleep(_HEARTBEAT_INTERVAL)
        except Exception:
            logger.warning("Heartbeat failed, triggering reconnection")
            reconnect_event.set()
            break


# ──────────────────────────────────────────────
#  公共 API
# ──────────────────────────────────────────────

def run_client(
    server_url: str,
    proxy: str | None = None,
    reconnect_interval: int = 5,
    token: str | None = None,
    insecure: bool = False,
    shell: str = "/bin/bash",
    name: str | None = None,
    no_pty: bool = False,
) -> None:
    """启动 WebSocket 后端客户端。

    连接中继服务器，注册为后端，并启动交互式 shell 子进程。
    断开时自动重连（指数退避），内置心跳保活。

    Args:
        server_url: 中继服务器地址，如 ``ws://1.2.3.4:8080``。
        proxy: HTTP 代理地址，如 ``http://127.0.0.1:18080``。``None`` 表示直连。
        reconnect_interval: 初始重连间隔秒数，默认 5。
        token: 认证令牌。与中继端 ``--token`` 保持一致。
        insecure: 跳过 TLS 证书验证（自签名证书）。
        shell: shell 路径，默认 ``/bin/bash``。
        name: 容器名称，用于多容器场景。
        no_pty: 禁用 PTY，回退到管道模式（向后兼容）。
    """
    proxy_host: str | None = None
    proxy_port: int | None = None
    if proxy:
        p = urlparse(proxy)
        proxy_host = p.hostname
        proxy_port = p.port

    # 通知 relay 本后端的终端模式
    mode_flag = "pipe" if no_pty else "pty"

    attempt = 0
    while True:
        reconnect_event = threading.Event()
        try:
            ws = websocket.WebSocket(
                sslopt={"cert_reqs": ssl.CERT_NONE} if insecure else None,
            )
            ws.settimeout(60)
            ws.connect(
                server_url,
                http_proxy_host=proxy_host,
                http_proxy_port=proxy_port,
            )
            logger.info(f"Connected to {server_url}")

            # 发送注册消息（携带终端模式标记）
            if token and name:
                reg_msg = f"IAM_BACKEND:{token}:{name}:{mode_flag}"
            elif token:
                reg_msg = f"IAM_BACKEND:{token}::{mode_flag}"
            elif name:
                reg_msg = f"IAM_BACKEND:{name}:{mode_flag}"
            else:
                reg_msg = f"IAM_BACKEND:{mode_flag}"
            ws.send(reg_msg)
            logger.info(
                f"Registered as backend (name={name or 'auto'}, "
                f"mode={'pipe' if no_pty else 'pty'})"
            )

            # 连接成功，重置重连计数
            attempt = 0

            # 启动心跳线程
            hb = threading.Thread(
                target=_heartbeat, args=(ws, reconnect_event), daemon=True
            )
            hb.start()

            if no_pty:
                _run_pipe_mode(ws, shell, reconnect_event)
            else:
                _run_pty_mode(ws, shell, reconnect_event)

        except Exception as e:
            attempt += 1
            delay = min(
                reconnect_interval * (2 ** (attempt - 1)),
                _RECONNECT_MAX_DELAY,
            )
            logger.error(
                f"Client error: {e}, reconnecting in {delay}s "
                f"(attempt {attempt})"
            )
            time.sleep(delay)
        else:
            # _run_*mode 正常返回 ≠ 连接健康
            # PTY 线程退出 / 心跳失败 / WebSocket 关闭 都会触发
            # reconnect_event.set() 后让 run_*mode 优雅退出（无异常）
            # 这里检测到 event 已 set 就走重连，而不是 break 退出进程
            if reconnect_event.is_set():
                logger.info("Connection degraded, reconnecting...")
                continue
            break


# ──────────────────────────────────────────────
#  PTY 模式：支持 vim / top / htop 等 TUI 程序
# ──────────────────────────────────────────────

def _run_pty_mode(
    ws: websocket.WebSocket,
    shell: str,
    reconnect_event: threading.Event,
) -> None:
    """PTY 模式：使用伪终端，支持全屏 TUI 程序和窗口大小调整。

    Shell 崩溃后自动在本连接内重生（最多 5 次），避免频繁重连。

    Args:
        ws: WebSocket 连接。
        shell: shell 可执行文件路径。
        reconnect_event: 重连事件。
    """
    max_restarts = 5
    restart_count = 0

    while not reconnect_event.is_set() and restart_count < max_restarts:
        master_fd, slave_fd = pty.openpty()
        try:
            cols, rows = os.get_terminal_size()
        except OSError:
            rows, cols = 50, 200
        _set_winsize(master_fd, rows, cols)
        shell_proc = subprocess.Popen(
            [shell, "-i"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=False,
            preexec_fn=os.setsid,
        )
        os.close(slave_fd)

        shell_restart = threading.Event()

        def read_pty_output(mfd: int, sp: subprocess.Popen, sr: threading.Event) -> None:
            try:
                while not sr.is_set() and not reconnect_event.is_set():
                    rlist, _, _ = select.select([mfd], [], [], 0.5)
                    if rlist:
                        try:
                            data = os.read(mfd, 65536)
                            if not data:
                                break
                            ws.send_binary(data)
                        except OSError:
                            break
                        except Exception:
                            break
            finally:
                logger.info("PTY output thread exited")
                sr.set()

        t = threading.Thread(target=read_pty_output, args=(master_fd, shell_proc, shell_restart), daemon=True)
        t.start()

        try:
            while not shell_restart.is_set() and not reconnect_event.is_set():
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not msg:
                    shell_restart.set()
                    break
                if isinstance(msg, bytes):
                    os.write(master_fd, msg)
                elif isinstance(msg, str):
                    if msg == "__PONG__":
                        continue
                    if msg.startswith("__RESIZE:"):
                        try:
                            _, dims = msg.split(":", 1)
                            r, c = map(int, dims.split(","))
                            _set_winsize(master_fd, r, c)
                        except (ValueError, OSError) as e:
                            logger.debug(f"Resize failed: {e}")
                        continue
                    if msg.startswith("__SIGNAL:"):
                        sig_name = msg.split(":", 1)[1].strip()
                        _send_signal(shell_proc, sig_name)
                        continue
                    os.write(master_fd, (msg + "\n").encode())
        except websocket.WebSocketConnectionClosedException:
            logger.warning("WebSocket connection closed")
            reconnect_event.set()
            break
        except websocket.WebSocketTimeoutException:
            pass
        except Exception as e:
            logger.error(f"Receive error: {e}")
        finally:
            shell_restart.set()
            _terminate_process(shell_proc)
            try:
                os.close(master_fd)
            except OSError:
                pass

        if not reconnect_event.is_set():
            restart_count += 1
            logger.warning(f"Shell respawning ({restart_count}/{max_restarts})...")
            time.sleep(2)

    if restart_count >= max_restarts:
        logger.error(f"Shell respawn limit reached ({max_restarts}), triggering reconnect")
    reconnect_event.set()
    ws.close()


# ──────────────────────────────────────────────
#  管道模式：向后兼容，仅支持行缓冲输出
# ──────────────────────────────────────────────

def _run_pipe_mode(
    ws: websocket.WebSocket,
    shell: str,
    reconnect_event: threading.Event,
) -> None:
    """管道模式（向后兼容）：使用普通管道，按行缓冲输出。

    使用 4096 字节缓冲读取 shell 输出，遇到换行符或缓冲区满时发送。

    Args:
        ws: WebSocket 连接。
        shell: shell 可执行文件路径。
        reconnect_event: 重连事件。
    """
    shell_proc = subprocess.Popen(
        [shell, "-i"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )

    def read_and_forward() -> None:
        """读取 shell 输出，按行缓冲后通过 WebSocket 发送。"""
        buf = bytearray()
        try:
            while True:
                data = shell_proc.stdout.read(_PIPE_READ_BUF)
                if not data:
                    break
                buf.extend(data)
                # 找到最后一个换行符，发送完整行
                last_nl = buf.rfind(b"\n")
                if last_nl >= 0:
                    ws.send(buf[:last_nl + 1].decode("utf-8", errors="replace"))
                    buf = buf[last_nl + 1:]
                elif len(buf) >= 4096:
                    # 缓冲区满且无换行，直接发送
                    ws.send(buf.decode("utf-8", errors="replace"))
                    buf.clear()
            # 发送剩余内容
            if buf:
                ws.send(buf.decode("utf-8", errors="replace"))
        except Exception:
            pass
        logger.info("Shell output thread exited")

    t = threading.Thread(target=read_and_forward, daemon=True)
    t.start()

    # 主线程：接收 WebSocket 命令，写入 shell
    try:
        while not reconnect_event.is_set():
            cmd = ws.recv()
            if not cmd:
                break
            # ── 心跳回包，直接忽略 ──
            if cmd == "__PONG__":
                continue
            # ── 正常命令，转发给 shell ──
            logger.debug(f"Command: {cmd.strip()[:60]}")
            shell_proc.stdin.write((cmd + "\n").encode())
            shell_proc.stdin.flush()
    except websocket.WebSocketConnectionClosedException:
        logger.warning("WebSocket connection closed")
    except websocket.WebSocketTimeoutException:
        pass
    except Exception as e:
        logger.error(f"Receive error: {e}")
    finally:
        reconnect_event.set()  # 停止心跳
        _terminate_process(shell_proc)
        ws.close()
