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
@click.option("--verbose", is_flag=True, default=False, help="详细日志 (DEBUG)")
@click.option("--quiet", is_flag=True, default=False, help="静默模式，仅显示警告和错误")
def relay(
    host: str,
    port: int,
    token: str | None,
    cert: str | None,
    key: str | None,
    wxpush: str | None,
    verbose: bool,
    quiet: bool,
) -> None:
    """启动中继服务（VPS 端）"""
    _setup_logging(verbose, quiet)
    run_relay(host, port, token, cert, key, wxpush)


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
