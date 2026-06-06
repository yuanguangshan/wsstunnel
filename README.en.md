# wsstunnel

**WebSocket reverse shell relay** — penetrate restricted networks via HTTP(S) proxy, get a full PTY interactive shell. Zero pre-installed services required on the target.

[中文版 🇨🇳](README.md)

## Why wsstunnel?

Existing tools (frp, ngrok, chisel) rely on port forwarding — they need a listening service (like sshd) on the target machine. But in restricted environments — online IDEs, CI runners, containers with HTTP-only egress — you often have **no root, no sshd, no inbound ports**.

wsstunnel takes a different approach:

- **Reverse PTY shell** — no listening service needed. Uses `pty.openpty()` to spawn `bash -i`, streams I/O over WebSocket.
- **HTTP CONNECT proxy traversal** — works behind corporate proxies, the only allowed egress.
- **True terminal, not a pipe** — supports `vim`/`top`/`htop`, window resize (`__RESIZE`), signal forwarding (`__SIGNAL`), multi-backend routing.

## Quick start (30 seconds)

```bash
# Terminal 1 — VPS (relay)
pip install wsstunnel
wsstunnel relay --port 8080

# Terminal 2 — target container (client)
pip install wsstunnel
wsstunnel client --server ws://your-vps:8080

# Terminal 3 — your machine (frontend, any WebSocket client)
websocat ws://your-vps:8080
# Type a command, see the output
whoami
```

That's it. No token, no TLS, no config file. For production, add `--token` and TLS.

## How it works

```
[You] --ws--> [Relay (VPS)] <--ws-- [Target container]
                                         |
                                    pty.openpty()
                                         |
                                    bash -i
```

Three roles:

| Role | Runs on | What it does |
|------|---------|-------------|
| **Relay** | Public VPS | WebSocket server, routes commands between frontends and backends |
| **Client** (backend) | Target container | Connects to relay, spawns a PTY shell, streams I/O |
| **Frontend** | Your machine | Connects to relay, sends commands, receives output |

The client needs NO listening port — it only makes outbound WebSocket connections. This is the key difference from SSH/frp/ngrok.

## Features

| Feature | Details |
|---------|---------|
| **Reverse PTY** | `pty.openpty()` + `bash -i` — supports `vim`, `top`, `htop` |
| **HTTP proxy** | Traverses HTTP CONNECT proxies out of the box |
| **PTY / Pipe dual mode** | PTY (default) for TUI apps; `--no-pty` falls back to line-buffered pipe |
| **Multi-backend routing** | Multiple containers connect simultaneously; switch with `USE <name>` or `@name cmd` |
| **Control protocol** | `__RESIZE:<rows>,<cols>` for PTY resize; `__SIGNAL:SIGINT` for remote signals |
| **URL token auth** | `?token=xxx` in WebSocket URL — no manual `AUTH:` message needed |
| **TLS / WSS** | Let's Encrypt, self-signed (`--insecure`), nginx reverse proxy |
| **Web terminal** | Built-in xterm.js page served by the relay itself |
| **File transfer** | Upload/download files between frontend and backend (`put`/`get` CLI, `dl` in shell) |
| **Auto-reconnect** | Exponential backoff (max 300s), PTY auto-respawn up to 5x |
| **Daemon mode** | `--daemon` with PID file and log file |
| **Python API** | `from wsstunnel import run_relay, run_client` |

## Install

```bash
pip install wsstunnel
```

Requires Python ≥ 3.10.

From source:
```bash
git clone https://github.com/yuanguangshan/wsstunnel.git
cd wsstunnel
pip install -e .
```

## Production setup

```bash
# VPS: relay with TLS + token
wsstunnel relay \
    --port 443 \
    --token $(openssl rand -hex 32) \
    --cert /etc/letsencrypt/live/example.com/fullchain.pem \
    --key /etc/letsencrypt/live/example.com/privkey.pem

# Container: connect through HTTP proxy
wsstunnel client \
    --server wss://example.com:443 \
    --proxy http://127.0.0.1:18080 \
    --token mysecret
```

### Systemd service

```ini
[Unit]
Description=wsstunnel Relay
After=network.target

[Service]
Environment=WS_TUNNEL_TOKEN=mysecret
ExecStart=/usr/local/bin/wsstunnel relay --port 443 \
    --cert /etc/letsencrypt/live/example.com/fullchain.pem \
    --key /etc/letsencrypt/live/example.com/privkey.pem
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## File transfer (v0.18.0+)

Upload/download files through the WebSocket tunnel:

```bash
# Upload local file to backend
wsstunnel put --server ws://vps:8080 \
    --token secret --backend mybox \
    ./local.txt /remote/path.txt

# Download file from backend
wsstunnel get --server ws://vps:8080 \
    --token secret --backend mybox \
    /remote/path.txt ./local.txt
```

In the web terminal, use `dl <path>` to download, or click the upload button.

## Protocol

| Role | First message | Server response |
|------|--------------|-----------------|
| **Backend** | `IAM_BACKEND:<token>:<name>:<pty\|pipe>` | — (silent) |
| **Frontend** | `AUTH:<token>` | `AUTH_OK` / `AUTH_FAIL` |
| **Frontend (URL auth)** | Connect to `ws://host:port?token=<token>` | `AUTH_OK` |

Frontend commands: `LIST`, `USE <name>`, `@name <cmd>`, `<cmd>` (to current backend).

Control commands: `__RESIZE:<rows>,<cols>`, `__SIGNAL:SIGINT`, `__TEXT`/`__RAW`.

## Comparison

| Feature | wsstunnel | frp/ngrok | chisel | SSH |
|---------|-----------|-----------|--------|-----|
| Requires listening service on target | **No** | Yes | Yes | Yes |
| HTTP proxy traversal | **Native** | No | Partial | No |
| PTY (vim/htop support) | **Yes** | No | No | Yes |
| Multi-backend routing | **Built-in** | No | No | No |
| Built-in web terminal | **Yes** | No | No | No |
| File transfer | **Yes** (v0.18.0+) | No | No | Yes (scp) |
| Single binary | No (Python) | Yes | Yes | Yes |

## Known limitations

- No compression on large output
- Shell-only (not a generic TCP tunnel — though adaptable)
- No structured audit logging

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
