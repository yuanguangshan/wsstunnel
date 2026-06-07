#!/usr/bin/env python3
"""
wsstunnel/cli.py — 统一命令行入口

通过 click 提供 relay 和 client 两个子命令。
"""

import logging
import os
import sys

import click
from .relay import run_relay
from .client import run_client

# 从环境变量读取默认 token
_DEFAULT_TOKEN: str | None = os.environ.get("WS_TUNNEL_TOKEN", None)


def _setup_logging(verbose: bool, quiet: bool) -> None:
    """配置日志级别。

    Args:
        verbose: 启用 DEBUG 级别。
        quiet: 仅显示 WARNING 及以上。
    """
    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    else:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s:%(name)s:%(message)s",
    )


@click.group()
@click.version_option(package_name="wsstunnel")
def cli() -> None:
    """WebSocket Tunnel - 远程 Shell 中继工具"""


# ──────────────────────────────────────────────
#  文件传输（前端侧：上传/下载）
# ──────────────────────────────────────────────


def _b64(s: str) -> str:
    """Base64 编码字符串。"""
    import base64
    return base64.b64encode(s.encode()).decode()


def _connect_frontend(server: str, token: str | None, insecure: bool) -> "websocket.WebSocket":
    """连接中继并认证为前端，返回 WebSocket 连接。"""
    import ssl as ssl_mod
    import websocket
    ws = websocket.WebSocket(
        sslopt={"cert_reqs": ssl_mod.CERT_NONE} if insecure else None,
    )
    ws.settimeout(120)
    ws.connect(server)

    # 情况 1：URL 已带 ?token=xxx → relay 会自动认证并发送 AUTH_OK
    if "?token=" in server.lower():
        try:
            resp = ws.recv()
            if resp == "AUTH_OK":
                return ws
        except Exception:
            pass
        ws.close()
        raise RuntimeError("URL token authentication failed (relay may be too old)")

    # 情况 2：普通认证 → CLI 先主动发送 AUTH: 消息
    if token:
        ws.send(f"AUTH:{token}")
        resp = ws.recv()
        if resp != "AUTH_OK":
            ws.close()
            raise RuntimeError(f"Authentication failed: {resp}")
        return ws

    # 情况 3：无 token，纯明文连接
    return ws


@cli.command()
@click.option("--server", required=True, help="中继服务器地址，如 ws://1.2.3.4:8080")
@click.option("--token", "-t", default=None, help="认证令牌")
@click.option("--insecure", is_flag=True, default=False, help="跳过 TLS 证书验证")
@click.option("--backend", default=None, help="目标后端名称，默认当前选中的后端")
@click.argument("local_path", type=click.Path(exists=True, readable=True))
@click.argument("remote_path", required=False, default=None)
def put(
    server: str,
    token: str | None,
    insecure: bool,
    backend: str | None,
    local_path: str,
    remote_path: str | None,
) -> None:
    """上传文件到远端后端。

    LOCAL_PATH 是本地文件路径。
    REMOTE_PATH 是远端保存路径（不设则使用本地文件名）。
    """
    import os
    import base64
    import websocket
    import time

    if not remote_path:
        remote_path = os.path.basename(local_path)

    file_size = os.path.getsize(local_path)
    b64_remote = _b64(remote_path)

    click.echo(f"Connecting to {server}...")
    ws = _connect_frontend(server, token, insecure)

    # USE 指定后端
    if backend:
        ws.send(f"USE {backend}")
        click.echo(f"Switched to backend: {backend}")
        time.sleep(0.3)
        # 清空确认消息
        try:
            while True:
                ws.recv()
        except Exception:
            pass
        ws.settimeout(120)

    click.echo(f"Uploading {local_path} ({file_size} bytes) → {remote_path}...")

    ws.send(f"__FILE_BEGIN:{b64_remote}:{file_size}")
    # 等确认（后端回复 __FILE_OK: 表示接受上传）
    resp = ws.recv()
    if not resp.startswith("__FILE_OK:"):
        ws.close()
        raise RuntimeError(f"Upload rejected: {resp}")

    chunk_size = 65536
    idx = 0
    sent = 0
    with open(local_path, "rb") as f:
        while True:
            data = f.read(chunk_size)
            if not data:
                break
            b64_data = base64.b64encode(data).decode()
            ws.send(f"__FILE_CHUNK:{b64_remote}:{idx}:{b64_data}")
            idx += 1
            sent += len(data)
            click.echo(f"  Progress: {sent}/{file_size} bytes ({100*sent//file_size}%)", nl=False)
            click.echo("\r", nl=False)

    ws.send(f"__FILE_END:{b64_remote}")
    resp = ws.recv()
    ws.close()

    if resp.startswith("__FILE_DONE:"):
        click.echo(f"\n✅ Upload complete: {remote_path} ({sent} bytes)")
    elif resp.startswith("__FILE_ERROR:"):
        _, _, msg = resp.partition(":")
        _, _, msg = msg.partition(":")
        raise RuntimeError(f"Upload failed: {msg}")


@cli.command()
@click.option("--server", required=True, help="中继服务器地址，如 ws://1.2.3.4:8080")
@click.option("--token", "-t", default=None, help="认证令牌")
@click.option("--insecure", is_flag=True, default=False, help="跳过 TLS 证书验证")
@click.option("--backend", default=None, help="目标后端名称，默认当前选中的后端")
@click.argument("remote_path")
@click.argument("local_path", type=click.Path(), required=False, default=None)
def get(
    server: str,
    token: str | None,
    insecure: bool,
    backend: str | None,
    remote_path: str,
    local_path: str | None,
) -> None:
    """从远端后端下载文件。

    REMOTE_PATH 是远端文件路径。
    LOCAL_PATH 是本地保存路径（不设则使用远端文件名）。
    """
    import os
    import base64
    import websocket

    if not local_path:
        local_path = os.path.basename(remote_path)

    b64_remote = _b64(remote_path)

    click.echo(f"Connecting to {server}...")
    ws = _connect_frontend(server, token, insecure)

    if backend:
        ws.send(f"USE {backend}")
        import time
        time.sleep(0.3)
        try:
            while True:
                ws.recv()
        except Exception:
            pass
        ws.settimeout(120)

    click.echo(f"Downloading {remote_path} → {local_path}...")
    ws.send(f"__FILE_DOWNLOAD:{b64_remote}")

    chunks: dict[int, bytes] = {}
    total_size = 0
    received = 0

    while True:
        resp = ws.recv()
        if resp.startswith("__FILE_BEGIN:"):
            _, _, size_str = resp.partition(":")
            _, size_str = size_str.rsplit(":", 1)
            total_size = int(size_str)
            click.echo(f"  Size: {total_size} bytes")
        elif resp.startswith("__FILE_CHUNK:"):
            parts = resp.split(":", 3)
            idx = int(parts[2])
            data = base64.b64decode(parts[3])
            chunks[idx] = data
            received += len(data)
            click.echo(f"  Progress: {received}/{total_size} bytes")
        elif resp.startswith("__FILE_END:"):
            break
        elif resp.startswith("__FILE_ERROR:"):
            _, _, msg = resp.partition(":")
            _, _, msg = msg.partition(":")
            ws.close()
            raise RuntimeError(f"Download failed: {msg}")
        else:
            click.echo(f"  (ignored: {resp[:60]})")

    ws.close()

    # 按序写入
    os.makedirs(os.path.dirname(os.path.abspath(local_path)) or ".", exist_ok=True)
    with open(local_path, "wb") as f:
        for idx in sorted(chunks):
            f.write(chunks[idx])

    click.echo(f"✅ Download complete: {local_path} ({received} bytes)")


@cli.command()
@click.option("--host", default="0.0.0.0", help="监听地址")
@click.option("--port", default=8080, type=int, help="监听端口")
@click.option(
    "--token", "-t",
    default=_DEFAULT_TOKEN,
    help='认证令牌。也可通过 WS_TUNNEL_TOKEN 环境变量设置。不设则不开启认证。',
)
@click.option("--cert", default=None, help="TLS 证书路径（启用 wss://）")
@click.option("--key", default=None, help="TLS 私钥路径。未指定时使用 --cert 路径的同一文件")
@click.option(
    "--wxpush", default=None,
    help="微信推送通知（后端上线/下线），格式 url:key",
)
@click.option(
    "--token-file", default=None,
    help="Token JSON 文件（支持多 token、角色、过期）",
)
@click.option(
    "--allow-ip", default=None, multiple=True,
    help="IP 白名单（支持 CIDR，可多次指定）",
)
@click.option(
    "--deny-cmd", default=None, multiple=True,
    help="命令黑名单（如 --deny-cmd rm）",
)
@click.option("--verbose", is_flag=True, default=False, help="详细日志 (DEBUG)")
@click.option("--quiet", is_flag=True, default=False, help="静默模式，仅显示警告和错误")
def relay(
    host: str,
    port: int,
    token: str | None,
    cert: str | None,
    key: str | None,
    wxpush: str | None,
    token_file: str | None,
    allow_ip: tuple[str, ...],
    deny_cmd: tuple[str, ...],
    verbose: bool,
    quiet: bool,
) -> None:
    """启动中继服务（VPS 端）"""
    _setup_logging(verbose, quiet)
    run_relay(
        host, port, token, cert, key, wxpush,
        token_file=token_file,
        allow_ip=list(allow_ip) if allow_ip else None,
        deny_cmd=list(deny_cmd) if deny_cmd else None,
    )


@cli.command()
@click.option("--server", required=True, help="中继服务器地址，如 ws://1.2.3.4:8080")
@click.option("--proxy", default=None, help="HTTP 代理，如 http://127.0.0.1:18080")
@click.option("--reconnect", default=5, type=int, help="初始重连间隔秒数（指数退避，最大 300s）")
@click.option(
    "--token", "-t",
    default=_DEFAULT_TOKEN,
    help='认证令牌。也可通过 WS_TUNNEL_TOKEN 环境变量设置。',
)
@click.option(
    "--insecure", is_flag=True, default=False,
    help="跳过 TLS 证书验证（用于自签名证书）",
)
@click.option(
    "--shell", default="/bin/bash",
    help="远程 shell 路径，默认 /bin/bash",
)
@click.option(
    "--name", default=None,
    help="容器名称，用于多容器场景。前端通过 @name 路由。不设则自动命名。",
)
@click.option(
    "--no-pty", is_flag=True, default=False,
    help="禁用 PTY，回退到管道模式（不支持 vim/top 等 TUI 程序，向后兼容）",
)
@click.option("--verbose", is_flag=True, default=False, help="详细日志 (DEBUG)")
@click.option("--quiet", is_flag=True, default=False, help="静默模式，仅显示警告和错误")
@click.option(
    "--daemon", is_flag=True, default=False,
    help="后台守护进程模式（fork + PID 文件 + 日志）",
)
@click.option(
    "--pidfile", default="/var/run/wsstunnel.pid",
    help="PID 文件路径（仅 --daemon 时有效）",
)
@click.option(
    "--logfile", default="/var/log/wsstunnel/client.log",
    help="日志文件路径（仅 --daemon 时有效）",
)
def client(
    server: str,
    proxy: str | None,
    reconnect: int,
    token: str | None,
    insecure: bool,
    shell: str,
    name: str | None,
    no_pty: bool,
    verbose: bool,
    quiet: bool,
    daemon: bool,
    pidfile: str,
    logfile: str,
) -> None:
    """启动客户端（容器端）"""
    if daemon:
        _daemonize(pidfile, logfile, verbose)
    _setup_logging(verbose, quiet)
    run_client(server, proxy, reconnect, token, insecure, shell, name, no_pty)


def _daemonize(pidfile: str, logfile: str, verbose: bool) -> None:
    """将当前进程转为后台守护进程。"""
    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    os.setsid()
    pid2 = os.fork()
    if pid2 > 0:
        sys.exit(0)

    # PID 文件（不可写则回退到当前目录）
    try:
        os.makedirs(os.path.dirname(pidfile) or ".", exist_ok=True)
        with open(pidfile, "w") as f:
            f.write(str(os.getpid()))
    except (OSError, PermissionError):
        pidfile = f"/tmp/wsstunnel-{os.getpid()}.pid"
        with open(pidfile, "w") as f:
            f.write(str(os.getpid()))

    # 日志文件（不可写则回退到 ./wsstunnel.log）
    try:
        os.makedirs(os.path.dirname(logfile) or ".", exist_ok=True)
        f = open(logfile, "a", 1)
    except (OSError, PermissionError):
        logfile = "wsstunnel.log"
        f = open(logfile, "a", 1)
    os.dup2(f.fileno(), sys.stdout.fileno())
    os.dup2(f.fileno(), sys.stderr.fileno())


def main() -> None:
    """CLI 入口函数。"""
    cli()


if __name__ == "__main__":
    main()
