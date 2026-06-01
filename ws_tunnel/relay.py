#!/usr/bin/env python3
"""
ws_tunnel/relay.py — WebSocket 中继服务（VPS 端）

角色注册机制:
  - 客户端连接后第一条消息决定身份
  - "IAM_BACKEND" 或 "IAM_BACKEND:<token>" → 注册为后端（容器）
  - "AUTH:<token>" → 注册为前端（第三方/浏览器）
  - 不设 token 时保持向后兼容（旧协议）

数据流:
  前端发送命令 → relay → 后端 (容器)
  后端输出结果 → relay → 所有前端
"""

import asyncio
import logging
import ssl

import websockets

logger = logging.getLogger(__name__)


def _make_handler(token: str | None):
    """创建 handler 闭包，捕获 token"""
    backend = None
    frontends: set = set()

    async def handler(websocket):
        nonlocal backend, frontends
        try:
            first = await asyncio.wait_for(websocket.recv(), timeout=30)

            # ── 后端注册 ──
            if _is_backend_auth(first, token):
                if backend is not None:
                    await websocket.close(1008, "Backend already registered")
                    return
                backend = websocket
                logger.info("Backend registered")
                try:
                    async for message in backend:
                        dead = set()
                        for f in frontends:
                            try:
                                await f.send(message)
                            except Exception:
                                dead.add(f)
                        frontends -= dead
                except websockets.exceptions.ConnectionClosed:
                    pass
                backend = None
                logger.info("Backend disconnected")

            # ── 前端注册 ──
            elif _is_frontend_auth(first, token):
                await websocket.send("AUTH_OK")
                frontends.add(websocket)
                logger.info(f"Frontend authenticated (total {len(frontends)})")
                async for message in websocket:
                    if backend:
                        try:
                            await backend.send(message)
                        except Exception:
                            logger.warning("Backend disconnected, cannot forward")
                            break
                    else:
                        await websocket.send("[Error] No backend connected")
                frontends.discard(websocket)
                logger.info(f"Frontend disconnected (total {len(frontends)})")

            else:
                await websocket.send("AUTH_FAIL")
                await websocket.close(1008, "Authentication failed")

        except asyncio.TimeoutError:
            await websocket.close(1002, "Protocol error: expected auth message")
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.exception(f"Handler error: {e}")
        finally:
            frontends.discard(websocket)
            if websocket == backend:
                backend = None

    return handler


def _is_backend_auth(msg: str, token: str | None) -> bool:
    """检查消息是否为合法的后端注册"""
    if token:
        return msg == f"IAM_BACKEND:{token}"
    return msg == "IAM_BACKEND"


def _is_frontend_auth(msg: str, token: str | None) -> bool:
    """检查消息是否为合法的前端认证"""
    if token:
        return msg == f"AUTH:{token}"
    # 无 token 时：第一条消息就是命令，直接当作前端
    return True


def _create_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """从证书文件创建 SSL 上下文"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    logger.info(f"TLS enabled (cert={cert_path})")
    return ctx


async def _run_async(host: str, port: int, handler, ssl_context=None):
    """异步运行中继服务"""
    async with websockets.serve(handler, host, port, ssl=ssl_context):
        logger.info(f"Relay running on {'wss://' if ssl_context else 'ws://'}{host}:{port}")
        await asyncio.Future()  # run forever


def run_relay(host: str = "0.0.0.0", port: int = 8080, token: str | None = None,
              cert_path: str | None = None, key_path: str | None = None):
    """启动 WebSocket 中继服务

    Args:
        host: 监听地址，默认 0.0.0.0
        port: 监听端口，默认 8080
        token: 可选认证令牌。None = 不开启认证（向后兼容）
    """
    if token:
        logger.info(f"Authentication enabled (token={token[:8]}...)")
    else:
        logger.warning("No token set — anyone can connect!")

    ssl_context = None
    if cert_path:
        ssl_context = _create_ssl_context(cert_path, key_path or cert_path)

    handler = _make_handler(token)
    asyncio.run(_run_async(host, port, handler, ssl_context))
