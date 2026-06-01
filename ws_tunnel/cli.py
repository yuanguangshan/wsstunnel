#!/usr/bin/env python3
"""
ws_tunnel/cli.py — 统一命令行入口

通过 click 提供 relay 和 client 两个子命令。
"""

import logging
import os

import click
from .relay import run_relay
from .client import run_client

# 从环境变量读取默认 token
_DEFAULT_TOKEN = os.environ.get("WS_TUNNEL_TOKEN", None)


def _setup_logging(verbose: bool, quiet: bool):
    """配置日志级别"""
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
def cli():
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
@click.option("--verbose", is_flag=True, default=False, help="详细日志 (DEBUG)")
@click.option("--quiet", is_flag=True, default=False, help="静默模式，仅显示警告和错误")
def relay(host, port, token, cert, key, verbose, quiet):
    """启动中继服务（VPS 端）"""
    _setup_logging(verbose, quiet)
    run_relay(host, port, token, cert, key)


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
@click.option("--verbose", is_flag=True, default=False, help="详细日志 (DEBUG)")
@click.option("--quiet", is_flag=True, default=False, help="静默模式，仅显示警告和错误")
def client(server, proxy, reconnect, token, insecure, shell, verbose, quiet):
    """启动客户端（容器端）"""
    _setup_logging(verbose, quiet)
    run_client(server, proxy, reconnect, token, insecure, shell)


def main():
    cli()


if __name__ == "__main__":
    main()
