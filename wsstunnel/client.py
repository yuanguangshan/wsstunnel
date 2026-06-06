#!/usr/bin/env python3
"""
wsstunnel/client.py — WebSocket 后端客户端（容器端）

同步版本，使用 websocket-client 库以支持 HTTP 代理穿透。
支持两种模式:
  - PTY 模式（默认）: 使用伪终端，支持 vim/top/htop 等 TUI 程序
  - 管道模式（--no-pty）: 使用普通管道，仅支持行缓冲输出（向后兼容）
"""

from __future__ import annotations

import base64
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
_FILE_CHUNK_SIZE = 65536    # 文件传输每块大小（64KB）

# 正在进行的文件传输状态：path -> {file, total, received}
_file_transfers: dict[str, dict] = {}

# shell 当前工作目录追踪（拦截 cd 命令自动更新）
_cwd: str = os.getcwd()

# PTY 按键缓冲：用于检测 dl 等特殊命令（后端拦截，不受前端影响）
_key_buffer: str = ""


def _update_cwd(cmd: str) -> None:
    """根据 shell 命令更新追踪的当前工作目录。"""
    global _cwd
    # 只处理 cd 命令
    stripped = cmd.strip()
    if not (stripped.startswith("cd ") or stripped == "cd"):
        return
    parts = stripped.split()
    if len(parts) == 1:
        # cd（无参数）→ $HOME
        target = os.environ.get("HOME", "/")
    else:
        target = parts[1]
        # 处理 ~ 开头的路径
        if target.startswith("~/"):
            home = os.environ.get("HOME", "/")
            target = os.path.join(home, target[2:])
        elif target == "~":
            target = os.environ.get("HOME", "/")
        # 非绝对路径：相对于当前 _cwd
        if not os.path.isabs(target):
            target = os.path.normpath(os.path.join(_cwd, target))
    _cwd = target
    logger.debug(f"CWD tracked: {_cwd}")


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
#  文件传输
# ──────────────────────────────────────────────

def _b64(s: str) -> str:
    """Base64 编码字符串（用于路径）。"""
    return base64.b64encode(s.encode()).decode()


def _unb64(s: str) -> str:
    """Base64 解码为字符串。"""
    return base64.b64decode(s).decode()


def _resolve_path(path: str) -> str:
    """解析上传/下载路径：相对路径自动拼接 shell 当前目录。"""
    if path.startswith("./") or path.startswith("~"):
        return os.path.normpath(os.path.join(_cwd, path))
    if not os.path.isabs(path):
        return os.path.normpath(os.path.join(_cwd, path))
    return path


def _handle_file_cmd(msg: str, ws: websocket.WebSocket) -> bool:
    """处理文件传输命令。返回 True 表示 msg 已被文件模块消费。"""
    global _file_transfers, _cwd

    # ── 上传：前端通知开始上传 ──
    if msg.startswith("__FILE_BEGIN:"):
        parts = msg.split(":", 2)
        if len(parts) < 3:
            return True
        try:
            path = _resolve_path(_unb64(parts[1]))
            total = int(parts[2])
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            f = open(path, "wb")
            _file_transfers[path] = {"file": f, "total": total, "received": 0}
            # 确认收到（使用 __FILE_OK: 避免与下载的 __FILE_BEGIN: 混淆）
            ws.send(f"__FILE_OK:{parts[1]}:{total}")
            logger.info(f"File upload started: {path} ({total} bytes)")
        except (OSError, ValueError) as e:
            ws.send(f"__FILE_ERROR:{parts[1]}:{e}")
        return True

    # ── 上传：数据块 ──
    if msg.startswith("__FILE_CHUNK:"):
        parts = msg.split(":", 3)
        if len(parts) < 4:
            return True
        try:
            path = _unb64(parts[1])
            data = base64.b64decode(parts[3])
        except Exception:
            return True
        state = _file_transfers.get(path)
        if state:
            state["file"].write(data)
            state["file"].flush()
            state["received"] += len(data)
        return True

    # ── 上传：结束 ──
    if msg.startswith("__FILE_END:"):
        parts = msg.split(":", 2)
        if len(parts) < 2:
            return True
        try:
            path = _unb64(parts[1])
        except Exception:
            return True
        state = _file_transfers.pop(path, None)
        if state:
            state["file"].close()
            actual = state["received"]
            logger.info(f"File upload completed: {path} ({actual} bytes)")
            # 使用 __FILE_DONE: 避免与下载的 __FILE_END: 混淆
            ws.send(f"__FILE_DONE:{parts[1]}:{actual}")
        return True

    # ── 上传：取消 ──
    if msg.startswith("__FILE_CANCEL:"):
        try:
            path = _unb64(msg.split(":", 1)[1])
        except Exception:
            return True
        state = _file_transfers.pop(path, None)
        if state:
            state["file"].close()
            try:
                os.remove(path)
            except OSError:
                pass
            logger.info(f"File upload cancelled: {path}")
            ws.send(f"__FILE_DONE:{_b64(path)}:0")
        return True

    # ── 下载：前端请求 ──
    if msg.startswith("__FILE_DOWNLOAD:"):
        try:
            path = _resolve_path(_unb64(msg.split(":", 1)[1]))
        except Exception:
            return True
        _send_file(path, ws)
        return True

    # ── 当前目录追踪（Web 终端通知的 cd 命令）──
    if msg.startswith("__CWD:"):
        target = msg[6:].strip()
        if target:
            global _cwd
            if target.startswith("~/"):
                home = os.environ.get("HOME", "/")
                target = os.path.join(home, target[2:])
            elif target == "~":
                target = os.environ.get("HOME", "/")
            if not os.path.isabs(target):
                target = os.path.normpath(os.path.join(_cwd, target))
            _cwd = target
            logger.debug(f"CWD updated via __CWD: {_cwd}")
        return True

    # ── Shell 友好命令：dl <path>（从交互式 Shell 下载文件）──
    if msg.startswith("dl ") and not msg.startswith("__"):
        path = msg[3:].strip()
        if path:
            path = _resolve_path(path)
            logger.info(f"Shell download request: {path}")
            _send_file(path, ws)
            return True

    return False


def _send_file(path: str, ws: websocket.WebSocket) -> None:
    """读取本地文件并通过 WebSocket 分块发送。"""
    b64_path = _b64(path)
    try:
        total = os.path.getsize(path)
        ws.send(f"__FILE_BEGIN:{b64_path}:{total}")
        idx = 0
        with open(path, "rb") as f:
            while True:
                data = f.read(_FILE_CHUNK_SIZE)
                if not data:
                    break
                b64_data = base64.b64encode(data).decode()
                ws.send(f"__FILE_CHUNK:{b64_path}:{idx}:{b64_data}")
                idx += 1
        ws.send(f"__FILE_END:{b64_path}:{total}")
        logger.info(f"File download sent: {path} ({total} bytes, {idx} chunks)")
    except FileNotFoundError:
        ws.send(f"__FILE_ERROR:{b64_path}:File not found")
    except PermissionError:
        ws.send(f"__FILE_ERROR:{b64_path}:Permission denied")
    except Exception as e:
        ws.send(f"__FILE_ERROR:{b64_path}:{e}")


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
    global _key_buffer
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
            pass_fds=(master_fd,),
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
                                logger.warning("PTY read EOF (shell exited)")
                                break
                            ws.send_binary(data)
                        except OSError as e:
                            logger.error(f"PTY reader OSError: {e}")
                            break
                        except Exception as e:
                            logger.error(f"PTY reader exception: {type(e).__name__}: {e}")
                            break
            finally:
                logger.info("PTY output thread exited")
                sr.set()

        t = threading.Thread(target=read_pty_output, args=(master_fd, shell_proc, shell_restart), daemon=True)
        t.start()

        # 新 shell 会话，重置按键缓冲
        global _key_buffer
        _key_buffer = ""

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
                    # 缓冲按键，检测 dl 命令（前端拦截不可靠，特别是移动端）
                    try:
                        ch = msg.decode("utf-8")
                        # Ctrl+C 清空缓冲（bash 行被取消）
                        if ch == "\x03":
                            _key_buffer = ""
                        # 只缓冲可打印字符和回车/换行
                        elif ch.isprintable() or ch in ("\r", "\n", "\x7f"):
                            _key_buffer += ch
                        if ch in ("\r", "\n"):
                            line = _key_buffer.replace("\r", "").replace("\n", "").strip()
                            _key_buffer = ""
                            if line.startswith("dl "):
                                path = _resolve_path(line[3:].strip())
                                if path:
                                    logger.info(f"PTY buffer dl: {path}")
                                    # 后台线程发送，不阻塞主循环（用户继续输入）
                                    threading.Thread(
                                        target=_send_file,
                                        args=(path, ws),
                                        daemon=True,
                                    ).start()
                                    # \x03 = Ctrl+C 清掉 bash 输入缓冲，\r = 新提示符
                                    os.write(master_fd, b"\x03\r")
                                    _key_buffer = ""
                                    continue
                    except (UnicodeDecodeError, AttributeError):
                        pass
                    os.write(master_fd, msg)
                elif isinstance(msg, str):
                    if msg == "__PONG__":
                        continue
                    if _handle_file_cmd(msg, ws):
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
                    # 追踪 cd 命令更新当前目录
                    _update_cwd(msg)
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
            # ── 文件传输命令 ──
            if _handle_file_cmd(cmd, ws):
                continue
            # ── 正常命令，转发给 shell ──
            logger.debug(f"Command: {cmd.strip()[:60]}")
            _update_cwd(cmd)
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
