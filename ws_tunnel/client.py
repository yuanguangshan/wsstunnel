#!/usr/bin/env python3
"""
ws_tunnel/client.py — WebSocket 后端客户端（容器端）

同步版本，使用 websocket-client 库以支持 HTTP 代理穿透。
支持两种模式:
  - PTY 模式（默认）: 使用伪终端，支持 vim/top/htop 等 TUI 程序
  - 管道模式（--no-pty）: 使用普通管道，仅支持行缓冲输出（向后兼容）
"""

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
from urllib.parse import urlparse

import websocket

logger = logging.getLogger(__name__)

_RECONNECT_MAX_DELAY = 300  # 最大重连间隔 5 分钟
_HEARTBEAT_INTERVAL = 30    # 心跳间隔秒数


def _set_winsize(fd, rows, cols):
    """设置伪终端窗口大小"""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


# 标准信号名称到 signal 模块常量的映射
_SIGNAL_MAP = {
    "SIGINT": signal.SIGINT,
    "SIGTERM": signal.SIGTERM,
    "SIGKILL": signal.SIGKILL,
    "SIGQUIT": signal.SIGQUIT,
    "SIGHUP": signal.SIGHUP,
    "SIGUSR1": signal.SIGUSR1,
    "SIGUSR2": signal.SIGUSR2,
}


def _send_signal(shell_proc, sig_name):
    """向前端进程组发送信号（通过 PTY 的进程组）"""
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


def _heartbeat(ws, reconnect_event):
    """心跳线程：定期发送 __PING__，失败时触发重连"""
    while not reconnect_event.is_set():
        try:
            ws.send("__PING__")
            time.sleep(_HEARTBEAT_INTERVAL)
        except Exception:
            logger.warning("Heartbeat failed, triggering reconnection")
            reconnect_event.set()
            break


def run_client(
    server_url: str,
    proxy: str | None = None,
    reconnect_interval: int = 5,
    token: str | None = None,
    insecure: bool = False,
    shell: str = "/bin/bash",
    name: str | None = None,
    no_pty: bool = False,
):
    """启动 WebSocket 后端客户端

    连接中继服务器，注册为后端，并启动交互式 shell 子进程。
    断开时自动重连（指数退避），内置心跳保活。

    Args:
        server_url: 中继服务器地址，如 ws://1.2.3.4:8080
        proxy: HTTP 代理地址，如 http://127.0.0.1:18080。None 表示直连。
        reconnect_interval: 初始重连间隔秒数，默认 5
        token: 认证令牌。与中继端 --token 保持一致。
        insecure: 跳过 TLS 证书验证（自签名证书）。
        shell: shell 路径，默认 /bin/bash
        name: 容器名称，用于多容器场景
        no_pty: 禁用 PTY，回退到管道模式（向后兼容）
    """
    proxy_host = proxy_port = None
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
            break


# ──────────────────────────────────────────────
#  PTY 模式：支持 vim / top / htop 等 TUI 程序
# ──────────────────────────────────────────────

def _run_pty_mode(ws, shell, reconnect_event):
    """PTY 模式：使用伪终端，支持全屏 TUI 程序和窗口大小调整"""
    master_fd, slave_fd = pty.openpty()

    # 获取当前终端大小（如果可以从父进程继承）
    try:
        cols, rows = os.get_terminal_size()
    except OSError:
        rows, cols = 24, 80
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

    # 禁用 PTY 回显，避免前端（文本模式）看到双重输入
    # TUI 程序（vim/top 等）会自行设置终端模式，不受影响
    try:
        mode = termios.tcgetattr(master_fd)
        mode[3] &= ~termios.ECHO  # 清除 ECHO 标志
        termios.tcsetattr(master_fd, termios.TCSANOW, mode)
    except OSError:
        pass

    # PTY 输出读取线程
    def read_pty_output():
        """从 PTY master 读取输出，以二进制帧发送到 WebSocket"""
        try:
            while not reconnect_event.is_set():
                rlist, _, _ = select.select([master_fd], [], [], 0.5)
                if rlist:
                    try:
                        data = os.read(master_fd, 65536)
                        if not data:
                            break
                        ws.send_binary(data)
                    except OSError:
                        break
        except Exception:
            pass
        finally:
            logger.info("PTY output thread exited")
            reconnect_event.set()

    t = threading.Thread(target=read_pty_output, daemon=True)
    t.start()

    # 主线程：接收 WebSocket 消息，写入 PTY
    try:
        while not reconnect_event.is_set():
            try:
                msg = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not msg:
                break

            if isinstance(msg, bytes):
                # 二进制数据：原始按键输入，直接写入 PTY
                os.write(master_fd, msg)
            elif isinstance(msg, str):
                # 文本消息
                if msg == "__PONG__":
                    continue
                if msg.startswith("__RESIZE:"):
                    # 窗口大小调整: __RESIZE:rows,cols
                    try:
                        _, dims = msg.split(":", 1)
                        r, c = map(int, dims.split(","))
                        _set_winsize(master_fd, r, c)
                    except (ValueError, OSError) as e:
                        logger.debug(f"Resize failed: {e}")
                    continue
                # 控制信号: __SIGNAL:SIGINT / __SIGNAL:SIGTERM / __SIGNAL:SIGKILL
                if msg.startswith("__SIGNAL:"):
                    sig_name = msg.split(":", 1)[1].strip()
                    _send_signal(shell_proc, sig_name)
                    continue
                # 普通命令：加上换行符后写入 PTY
                os.write(master_fd, (msg + "\n").encode())
    except websocket.WebSocketConnectionClosedException:
        logger.warning("WebSocket connection closed")
    except websocket.WebSocketTimeoutException:
        pass
    except Exception as e:
        logger.error(f"Receive error: {e}")
    finally:
        reconnect_event.set()
        shell_proc.kill()
        ws.close()
        try:
            os.close(master_fd)
        except OSError:
            pass


# ──────────────────────────────────────────────
#  管道模式：向后兼容，仅支持行缓冲输出
# ──────────────────────────────────────────────

def _run_pipe_mode(ws, shell, reconnect_event):
    """管道模式（向后兼容）：使用普通管道，按行缓冲输出"""
    shell_proc = subprocess.Popen(
        [shell, "-i"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )

    def read_and_forward():
        """读取 shell 输出，按行缓冲后通过 WebSocket 发送"""
        buf = bytearray()
        try:
            while True:
                byte = shell_proc.stdout.read(1)
                if not byte:
                    break
                buf.extend(byte)
                # 遇到换行符或缓冲足够大时发送
                if byte == b"\n" or len(buf) >= 4096:
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
        shell_proc.kill()
        ws.close()
