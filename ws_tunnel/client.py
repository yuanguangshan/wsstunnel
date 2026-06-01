#!/usr/bin/env python3
"""
ws_tunnel/client.py — WebSocket 后端客户端（容器端）

同步版本，使用 websocket-client 库以支持 HTTP 代理穿透。
启动交互式 bash 子进程，通过 WebSocket 接收命令并返回输出。
"""

import ssl
import subprocess
import threading
import time
import logging
from urllib.parse import urlparse

import websocket

logger = logging.getLogger(__name__)

_RECONNECT_MAX_DELAY = 300  # 最大重连间隔 5 分钟


def run_client(
    server_url: str,
    proxy: str | None = None,
    reconnect_interval: int = 5,
    token: str | None = None,
    insecure: bool = False,
    shell: str = "/bin/bash",
):
    """启动 WebSocket 后端客户端

    连接中继服务器，注册为后端，并启动交互式 shell 子进程。
    断开时自动重连（指数退避）。

    Args:
        server_url: 中继服务器地址，如 ws://1.2.3.4:8080
        proxy: HTTP 代理地址，如 http://127.0.0.1:18080。None 表示直连。
        reconnect_interval: 初始重连间隔秒数，默认 5
        token: 认证令牌。与中继端 --token 保持一致。
        insecure: 跳过 TLS 证书验证（自签名证书）。
        shell: shell 路径，默认 /bin/bash
    """
    proxy_host = proxy_port = None
    if proxy:
        p = urlparse(proxy)
        proxy_host = p.hostname
        proxy_port = p.port

    attempt = 0
    while True:
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

            # 发送注册消息（带 token 或向后兼容无 token）
            if token:
                ws.send(f"IAM_BACKEND:{token}")
            else:
                ws.send("IAM_BACKEND")
            logger.info("Registered as backend")

            # 连接成功，重置重连计数
            attempt = 0

            # 启动 shell 进程
            shell_proc = subprocess.Popen(
                [shell, "-i"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )

            def read_and_forward():
                """读取 shell 输出并通过 WebSocket 发送"""
                try:
                    while True:
                        out = shell_proc.stdout.read(1)
                        if not out:
                            break
                        try:
                            ws.send(out.decode("utf-8", errors="replace"))
                        except Exception:
                            break
                except Exception:
                    pass
                logger.info("Shell output thread exited")

            t = threading.Thread(target=read_and_forward, daemon=True)
            t.start()

            # 主线程：接收 WebSocket 命令，写入 shell
            try:
                while True:
                    cmd = ws.recv()
                    if not cmd:
                        break
                    logger.debug(f"Command: {cmd.strip()[:60]}")
                    shell_proc.stdin.write((cmd + "\n").encode())
                    shell_proc.stdin.flush()
            except websocket.WebSocketConnectionClosedException:
                logger.warning("WebSocket connection closed")
            except Exception as e:
                logger.error(f"Receive error: {e}")
            finally:
                shell_proc.kill()
                ws.close()

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
