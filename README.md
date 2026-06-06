# wsstunnel

wsstunnel 是一款通过 WebSocket 与 HTTP 代理穿透极端受限网络，提供原生 PTY 交互式远程 Shell 的轻量自托管工具。

## 目录

- [缘起](#缘起)
- [为什么选择 wsstunnel？](#为什么选择-wsstunnel)
- [快速开始](#快速开始5-分钟)
- [文件传输](#文件传输v0180-新增)
- [架构](#架构)
- [安装](#安装)
- [详细使用指南](#详细使用指南)
- [多后端路由](#多后端路由)
- [控制协议](#控制协议高级前端用)
- [认证协议](#认证协议)
- [TLS / WSS 加密](#tls--wss-加密)
- [使用第三方客户端连接](#使用第三方客户端连接)
- [使用库 API](#使用库-api在-python-代码中调用)
- [常见工作流](#常见工作流)
- [故障排查](#故障排查)
- [已知限制](#已知限制)
- [从旧版升级](#从旧版升级)
- [开发与测试](#开发与测试)

## 缘起

那段时间，我被沙箱环境折磨得够呛——容器说回收就回收，远程 SSH 总是不通。我把能想到的工具全试了一遍：cloudflared、frp、wireguard……要么配置复杂到令人头秃，要么在“只允许 HTTP 代理出站”的铁壁前直接哑火。

折腾到半夜，我突然想明白了：既然现成的路都走不通，何不自己开一条？于是便有了 wstunnel——一个专为极端受限网络而生的 WebSocket 隧道。它不依赖任何第三方服务，一条命令就能穿透 HTTP 代理，给你一个完整的交互式 Shell。

如今它开源了。希望能帮到每一个曾被沙箱“关住”的你。

## 为什么选择 wsstunnel？

市面上不乏优秀的内网穿透工具（frp、ngrok、chisel 等），但它们大多基于 **端口映射** 模式——要求目标机器上已经有一个监听的服务（如 sshd），然后将该端口暴露到公网。  
然而在真实的受限环境（在线 IDE、CI Runner、仅 HTTP 代理出站的容器）中，你往往 **没有 root 权限、无法安装 sshd、也无法配置入站端口**。

wsstunnel 采用了一条完全不同的技术路线：

- **反向 PTY Shell**：不依赖任何监听服务，直接在目标进程中通过 `pty.openpty()` 拉起 `bash -i`，将标准输入/输出通过 WebSocket 反向推送给中继端。
- **原生 HTTP CONNECT 代理穿透**：利用 `websocket-client` 库的代理参数，在仅允许 HTTP 出站的环境中也能建立隧道。
- **交付“真终端”而非“通道”**：内置 xterm.js Web 终端，支持 PTY 窗口大小同步（`__RESIZE`）、信号转发（`__SIGNAL`）、多后端路由（`@name` / `USE`），开箱即用，无需额外配置 SSH 客户端。

与通用 WebSock 隧道工具不同，wsstunnel 聚焦于 **“从零拿下受限环境的一个真终端”** 这一极致场景。它不是通用管道，而是专为沙箱调试、CI 环境、IoT 边缘设备设计的 **轻量级反向管理平台**。

如果你曾因“只能走 HTTP 代理、无法起 sshd”而束手无策，wsstunnel 或许是目前最直接、最轻量的解决方案。

**WebSocket 远程 Shell 中继工具** — 通过 WebSocket + HTTP 代理穿透受限网络环境，实现远程交互式 Shell。

适用场景：受限容器环境（在线 IDE、CI runner）、仅允许 HTTP 出站的内网设备、IoT 边缘设备、安全测试。

## 架构

```
第三方电脑（浏览器/websocat/Python）
       │
       │  ws://your-vps:8080 或 wss://your-vps:443
       │
       ▼
┌──────────────────────────────────┐
│  VPS（中继服务）                   │
│  wsstunnel relay --port 8080      │
│                                   │
│  角色：中继转发 + 后端路由          │
│  依赖：Python 3.10+ + websockets   │
└──────────┬──────────────┬─────────┘
           │              │
      前端（Frontend）  后端（Backend）
      发送命令           注册并执行
           │              │
           │              ▼
           │     ┌────────────────────────┐
           │     │ 目标容器/设备（客户端）   │
           │     │ wsstunnel client \      │
           │     │  --server ws://...      │
           │     │                        │
           │     │ 通过 HTTP 代理穿透       │
           │     │ 启动交互式 shell         │
           │     └────────────────────────┘
           │
           ▼
     你看到的输出
```

## 快速开始（5 分钟）

### 1️⃣ VPS 端

```bash
# 安装
pip install wsstunnel

# 启动中继（带 token + TLS）
wsstunnel relay --port 8080 --token mysecret --cert /path/to/cert.pem --key /path/to/key.pem
```

### 2️⃣ 容器端

```bash
# 安装
pip install wsstunnel

# 连接（带代理 + token）
wsstunnel client --server wss://your-vps:443 --proxy http://127.0.0.1:18080 --token mysecret
```

### 3️⃣ 连接测试

```bash
# 方式一：websocat
websocat ws://your-vps:8080
# 输入 token 认证（如果中继有 token）
AUTH:mysecret
# 然后就可以执行命令了
whoami
ls -la

# 方式二：Python 一行
python3 -c "
import websocket
ws = websocket.create_connection('ws://your-vps:8080')
ws.send('AUTH:mysecret')
print('Auth:', ws.recv())
ws.send('whoami')
print('Output:', ws.recv())
ws.close()
"
```

## 文件传输（v0.18.0 新增）

wsstunnel 支持在前后端之间直接传输文件，无需第三方工具。

### CLI 命令（本机 ↔ 远端）

```bash
# 上传文件到远端后端
wsstunnel put \
    --server wss://your-vps:443 \
    --token mysecret \
    --backend mybox \
    ./本地文件.txt \
    /root/远程文件.txt

# 从远端后端下载文件
wsstunnel get \
    --server wss://your-vps:443 \
    --token mysecret \
    --backend mybox \
    /root/远程文件.txt \
    ./下载到本地.txt
```

### 交互式 Shell 下载

在 Web 终端中输入 `dl <path>` 即可下载。相对路径自动拼 shell 当前目录：

```bash
dl /var/log/syslog      # 绝对路径
dl 文件名.txt             # 当前目录
```

### Web 终端上传

顶部工具栏 **📁 上传** 按钮，选文件传到 shell 当前目录。

### 协议

| 方向 | 消息格式 | 说明 |
|------|---------|------|
| 前端→后端 | `__FILE_BEGIN:{b64path}:{size}` | 开始上传 |
| 前端→后端 | `__FILE_CHUNK:{b64path}:{idx}:{b64data}` | 数据块（64KB） |
| 前端→后端 | `__FILE_END:{b64path}` | 上传完成 |
| 前端→后端 | `__FILE_DOWNLOAD:{b64path}` | 请求下载 |
| 后端→前端 | `__FILE_OK:{b64path}:{size}` | 上传确认 |
| 后端→前端 | `__FILE_DONE:{b64path}:{size}` | 上传完成 |
| 后端→前端 | `__FILE_BEGIN/END:{b64path}:{size}` | 下载开始/结束 |
| 后端→前端 | `__FILE_ERROR:{b64path}:{reason}` | 错误 |

## 安装

### 从 PyPI 安装（推荐）

```bash
pip install wsstunnel
```

安装后获得 `wsstunnel` 命令和 `wsstunnel` Python 包。

### 从源码安装

```bash
git clone git@github.com:yuanguangshan/wsstunnel.git
cd wsstunnel
pip install -e .
```

### 系统依赖

- Python >= 3.10
- 中继端依赖：`websockets`，`click`，`httpx`（可选，微信推送用）
- 客户端依赖：`websocket-client`，`click`

## 详细使用指南

### VPS 端（中继服务）

```bash
# 最小启动（不安全，仅内网测试）
wsstunnel relay --port 8080

# 生产启动（认证 + TLS）
wsstunnel relay \
    --port 443 \
    --token $(openssl rand -hex 32) \
    --cert /etc/letsencrypt/live/example.com/fullchain.pem \
    --key /etc/letsencrypt/live/example.com/privkey.pem

# 调试启动（看所有 WebSocket 消息）
wsstunnel relay --port 8080 --token mysecret --verbose

# 静默运行（仅错误日志）
wsstunnel relay --port 8080 --token mysecret --quiet
```

#### 所有 relay 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8080` | 监听端口 |
| `-t, --token` | — | 认证令牌，不设则不开启认证 |
| `--cert` | — | TLS 证书路径（提供后启用 wss://） |
| `--key` | — | TLS 私钥路径，未指定时使用 --cert |
| `--wxpush` | — | 微信推送通知，格式 `url:key`。后端上线/下线时发送通知 |
| `--verbose` | — | 输出 DEBUG 级别日志 |
| `--quiet` | — | 仅输出 WARNING 及以上日志 |

### 容器端（客户端）

```bash
# 基本连接（直连）
wsstunnel client --server ws://your-vps:8080 --token mysecret

# 通过 HTTP 代理连接（常见于受限容器）
wsstunnel client \
    --server ws://your-vps:8080 \
    --proxy http://127.0.0.1:18080 \
    --token mysecret

# 使用 wss 加密连接 + 自签名证书
wsstunnel client \
    --server wss://your-vps:443 \
    --token mysecret \
    --insecure

# 指定其他 shell（如 sh、zsh）
wsstunnel client \
    --server ws://your-vps:8080 \
    --token mysecret \
    --shell /bin/zsh

# 缩短重连间隔（快速重试场景）
wsstunnel client --server ws://... --token mysecret --reconnect 2
```

#### 所有 client 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--server` | **必填** | 中继服务器地址，如 `ws://1.2.3.4:8080` |
| `--proxy` | — | HTTP 代理地址，如 `http://127.0.0.1:18080` |
| `--reconnect` | `5` | 初始重连间隔秒数（指数退避，最大 300s） |
| `-t, --token` | — | 认证令牌，需与 relay 端一致 |
| `--insecure` | — | 跳过 TLS 证书验证（自签名证书） |
| `--shell` | `/bin/bash` | 远程 shell 路径 |
| `--name` | — | 后端名称，用于多容器路由（不设则自动命名） |
| `--no-pty` | — | 禁用 PTY，回退到管道模式（不支持 TUI 程序） |
| `--verbose` | — | 输出 DEBUG 级别日志 |
| `--quiet` | — | 仅输出 WARNING 及以上日志 |

### 通过环境变量统一管理 token

```bash
export WS_TUNNEL_TOKEN=mysecret

# 之后 --token 会自动读取，无需再写
wsstunnel relay --port 8080
wsstunnel client --server ws://your-vps:8080
```

## 常见工作流

### 工作流 A：从零搭建生产隧道

```bash
# ── VPS 端 ──
# 1. 安装 wsstunnel
pip install wsstunnel

# 2. 用 Let's Encrypt 申请证书
sudo apt install certbot nginx
sudo certbot certonly --standalone -d tunnel.example.com

# 3. 生成随机 token
export WS_TUNNEL_TOKEN=$(openssl rand -hex 32)
echo "Token: $WS_TUNNEL_TOKEN"  # 保存好

# 4. 启动中继（端口 443）
wsstunnel relay \
    --port 443 \
    --cert /etc/letsencrypt/live/tunnel.example.com/fullchain.pem \
    --key /etc/letsencrypt/live/tunnel.example.com/privkey.pem

# ── 容器端 ──
# 5. 连接（自动读取 WS_TUNNEL_TOKEN）
wsstunnel client --server wss://tunnel.example.com:443
```

### 工作流 B：受限容器穿透（HTTP 代理场景）

```bash
# ── VPS 端（简单启动，只需端口）──
wsstunnel relay --port 8080 --token mysecret

# ── 容器端 ──
# 容器通常有 HTTP 代理环境变量
echo $http_proxy  # 如 http://127.0.0.1:18080

# 连接（需指定代理）
wsstunnel client \
    --server ws://your-vps:8080 \
    --proxy http://127.0.0.1:18080 \
    --token mysecret

# ── 你的电脑 ──
websocat ws://your-vps:8080
# 输入: AUTH:mysecret
# 现在你可以执行远程命令了
```

### 工作流 C：TLS 自签名 + 本地测试

```bash
# 1. 生成自签名证书
openssl req -x509 -newkey rsa:2048 \
    -keyout key.pem -out cert.pem \
    -days 365 -nodes -subj "/CN=localhost"

# 2. 启动中继（wss://）
wsstunnel relay --port 4433 --cert cert.pem --key key.pem --token test123

# 3. 启动客户端（跳过证书验证）
wsstunnel client --server wss://127.0.0.1:4433 --token test123 --insecure

# 4. 前端测试
python3 -c "
import ssl, websocket
ws = websocket.create_connection(
    'wss://127.0.0.1:4433',
    sslopt={'cert_reqs': ssl.CERT_NONE}
)
ws.send('AUTH:test123')
print('Auth:', ws.recv())
ws.send('echo hello_world')
import time; time.sleep(1)
print('Output:', ws.recv())
ws.close()
"
```

### 工作流 D：Web 终端（xterm.js）

wsstunnel 自带一个基于 xterm.js 的 Web 终端，浏览器打开即用：

```bash
# 直接打开
open wsstunnel/web/index.html

# 或传参自动连接（推荐书签）
open "wsstunnel/web/index.html?server=wss://your-vps:443&token=mysecret"
```

页面功能：
- **原生终端体验**：颜色、光标、`vim`、`top`、`htop` 全部原生支持
- **📁 文件上传**：顶部工具栏点按钮选文件，传到 shell 当前目录
- **📥 文件下载**：shell 中输入 `dl <path>`，浏览器自动弹出下载
- **⌨ 移动端控制面板**：右下角浮窗，支持 Ctrl+A/E/U/K/W、方向键、复制
- **`\r` 正确处理**：xterm.js 是完整终端模拟器，不是文本显示
- **窗口大小自适应**：`__RESIZE` 自动同步
- **URL token 认证**：`?token=xxx` 自动连接，无需手动输入
- **连接管理**：断开后可重连，显示连接状态

你也可以通过 `wsstunnel/web/index.html?server=ws://your-vps:8080` 直接连接 ws:// 中继。

### 工作流 E：系统服务（systemd 自动启动）

先确认 wsstunnel 安装路径：

```bash
which wsstunnel
# 常见路径：/usr/local/bin/wsstunnel（pip install）
# 或：/usr/bin/wsstunnel（系统包管理器）
```

创建 systemd 服务 `/etc/systemd/system/wsstunnel.service`：

```ini
[Unit]
Description=wsstunnel WebSocket Relay
After=network.target

[Service]
Type=simple
User=root
Environment=WS_TUNNEL_TOKEN=mysecret
# 用 which wsstunnel 的实际输出替换下面的路径
ExecStart=/usr/local/bin/wsstunnel relay --port 443 --cert /etc/letsencrypt/live/example.com/fullchain.pem --key /etc/letsencrypt/live/example.com/privkey.pem
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wsstunnel
sudo journalctl -u wsstunnel -f  # 查看日志
```

## 认证协议

中继端设置 `--token` 后启用认证：

| 角色 | 第一条消息 | 服务端响应 | 说明 |
|------|-----------|-----------|------|
| **后端（容器）** | `IAM_BACKEND:<token>:<name>:<pty\|pipe>` | — | 注册成功后开始转发 shell 输出 |
| **前端（第三方）** | `AUTH:<token>` | `AUTH_OK` / `AUTH_FAIL` | 收到 `AUTH_OK` 后即可发命令 |
| **前端（URL 认证）** | 连接 `ws://host:port?token=<token>` | `AUTH_OK` | 自动认证，无需手动发 AUTH |
| **任意错误** | 其他消息 | `AUTH_FAIL` + 断开 (1008) | 拒绝连接 |

### 不设 token 时

保持完全向后兼容——第一条消息直接决定角色：

| 消息 | 角色 |
|------|------|
| `IAM_BACKEND` | 注册为后端 |
| 其他任意内容 | 注册为前端，内容作为第一条命令 |

## TLS / WSS 加密

### 方式一：使用已有证书（推荐）

```bash
# VPS 端
wsstunnel relay --port 443 \
    --cert /etc/letsencrypt/live/example.com/fullchain.pem \
    --key /etc/letsencrypt/live/example.com/privkey.pem

# 容器端（使用标准 CA 证书，无需额外参数）
wsstunnel client --server wss://example.com:443 --token mysecret
```

### 方式二：自签名证书

```bash
# 1. 生成证书
openssl req -x509 -newkey rsa:2048 \
    -keyout key.pem -out cert.pem \
    -days 365 -nodes -subj "/CN=your-vps-ip"

# 2. VPS 端
wsstunnel relay --port 443 --cert cert.pem --key key.pem --token mysecret

# 3. 容器端（必须加 --insecure 跳过验证）
wsstunnel client --server wss://your-vps:443 --token mysecret --insecure
```

> **安全提示**：`--insecure` 跳过证书验证，中间人可以解密流量。建议只用于测试，或配合 token 认证使用。

### 方式三：nginx 反向代理（生产推荐）

优点：证书管理交给 nginx（自动续期），wsstunnel 只需监听内网端口，无需 reload。

```nginx
# /etc/nginx/sites-available/tunnel
server {
    listen 443 ssl;
    http2 on;
    server_name tunnel.example.com;

    ssl_certificate     /etc/letsencrypt/live/tunnel.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/tunnel.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 86400s;
    }
}
```

```bash
# wsstunnel 在本地 8080 裸运行
wsstunnel relay --port 8080 --token mysecret
```

## 多后端路由

wsstunnel 支持同时连接多个后端容器。每个后端通过 `--name` 指定名称（不指定则自动命名）。

### 前端命令

| 命令 | 说明 |
|------|------|
| `LIST` | 查看所有已连接的后端（含模式和当前选择标记） |
| `USE <name>` | 切换默认后端，后续命令直接发送（无需 `@` 前缀） |
| `USE` | 查看当前使用的后端 |
| `@<name> <cmd>` | 临时向指定后端发送命令（不改变当前选择） |
| `<cmd>` | 发送给当前后端（`USE` 选中的，或第一个注册的） |

### 示例

```bash
# 连接后查看后端列表
LIST
# → [Info] Connected backends: web-server(pty) *, db-server(pipe)

# 切换到 db-server
USE db-server
# → [Info] Switched to backend: db-server(pipe)

# 之后命令直接发给 db-server
SELECT 1

# 临时向 web-server 发一条命令
@web-server nginx -t
```

### PTY 模式

默认使用 PTY（伪终端）模式，支持 `vim`、`top`、`htop` 等 TUI 程序。如需回退到传统管道模式（行缓冲），使用 `--no-pty`：

```bash
wsstunnel client --server ws://vps:8080 --token mysecret --no-pty
```

## 控制协议（高级前端用）

wsstunnel 定义了一套以 `__` 前缀的控制命令，前端在 PTY 模式下可用于实现类似终端的体验。

### 窗口大小同步

当本地终端窗口大小变化时，向前端发送 `__RESIZE:<rows>,<cols>` 调整远程 PTY 大小：

```python
import os, signal

def send_resize(ws):
    rows, cols = os.get_terminal_size()
    ws.send(f"__RESIZE:{rows},{cols}")

# 捕获终端大小变化信号
signal.signal(signal.SIGWINCH, lambda s, f: send_resize(ws))
send_resize(ws)  # 初始设置
```

### 远程信号发送

向远程进程发送信号（如 `Ctrl+C` 中断当前命令）：

| 控制命令 | 等效操作 |
|----------|----------|
| `__SIGNAL:SIGINT` | `Ctrl+C`，中断当前进程 |
| `__SIGNAL:SIGTERM` | 请求终止 |
| `__SIGNAL:SIGKILL` | 强制终止 |
| `__SIGNAL:SIGQUIT` | `Ctrl+\`，退出 + 生成 core dump |

```python
# 中断正在运行的命令
ws.send("__SIGNAL:SIGINT")
```

> PTY 模式下 `Ctrl+C` 按键本身（作为二进制帧发送）也会触发 SIGINT，无需手动发 `__SIGNAL:SIGINT`。

### URL Token 快速认证

前端连接时可将 token 直接放在 URL 中，连接即认证，无需手动发送 `AUTH:` 消息：

```bash
# 普通方式
websocat ws://your-vps:8080
# 然后手动输入: AUTH:mysecret

# URL 方式（自动认证）
websocat ws://your-vps:8080?token=mysecret
```

URL 方式同样适用于 Python 和浏览器：

```python
ws = websocket.create_connection("ws://your-vps:8080?token=mysecret")
# 无需发送 AUTH，直接发送命令
ws.send("whoami")
```

## 使用第三方客户端连接

### websocat（推荐，交互式体验）

```bash
# 安装
brew install websocat          # macOS
cargo install websocat         # 或从源码

# 连接
websocat ws://your-vps:8080

# 连接后输入认证（如果有 token）：
AUTH:mysecret

# 然后即可交互式操作
```

### Python 脚本

```python
import websocket
import time

ws = websocket.create_connection("ws://your-vps:8080")

# 认证（如果有 token）
ws.send("AUTH:mysecret")
auth_resp = ws.recv()
assert auth_resp == "AUTH_OK", f"Auth failed: {auth_resp}"

# 发送命令
ws.send("uname -a")
time.sleep(0.5)

# 读取输出
ws.settimeout(2)
try:
    while True:
        output = ws.recv()
        print(output, end="")
except websocket.WebSocketTimeoutException:
    pass

ws.close()
```

### 浏览器（F12 控制台）

```javascript
const ws = new WebSocket("ws://your-vps:8080");
ws.onmessage = (e) => console.log(e.data);

// 认证（如果有 token）
ws.send("AUTH:mysecret");

// 发送命令
ws.send("ls -la");
```

## 使用库 API（在 Python 代码中调用）

```python
from wsstunnel import run_relay, run_client

# 启动中继（阻塞）
run_relay("0.0.0.0", 8080, token="mysecret")

# 启动中继 + TLS + 微信通知
run_relay(
    "0.0.0.0", 443,
    token="mysecret",
    cert_path="/path/to/cert.pem",
    key_path="/path/to/key.pem",
    wxpush="https://wxpusher.zjiecode.com/api/send/message:your_app_token",
)

# 启动客户端
run_client(
    "ws://your-vps:8080",
    proxy="http://127.0.0.1:18080",
    token="mysecret",
    shell="/bin/bash",
    reconnect_interval=5,
    insecure=True,
)
```

## 故障排查

### 连接被拒绝

```
Connection refused
```

- 检查 VPS 端的端口是否开放：`ss -tlnp | grep 8080`
- 检查防火墙：`ufw status` 或云平台安全组规则
- 确认中继已在运行：`ps aux | grep wsstunnel`

### 认证失败

```
AUTH_FAIL
```

- 确认 relay 端设置了 `--token`
- 确认 client 端使用了相同的 token
- token 区分大小写

### 代理连接失败

```
Proxy connection failed
```

- 确认容器内有 HTTP 代理可用：`echo $http_proxy`
- 测试代理本身是否正常：`curl -x http://127.0.0.1:18080 http://example.com`
- 代理地址格式：`http://host:port`（必须是 http://，不是 https://）

### TLS 证书错误

```
[SSL: CERTIFICATE_VERIFY_FAILED]
```

- 自签名证书：客户端加 `--insecure`
- 证书过期：检查证书有效期 `openssl x509 -in cert.pem -noout -dates`
- 域名不匹配：证书 CN 需与连接域名一致

### bash 未找到

```
FileNotFoundError: /bin/bash
```

- 容器内可能没有 bash，改用 `--shell /bin/sh`
- 确认指定路径的正确性：`which bash`

### 后端未连接

```
[Error] No backend connected
```

- 确保容器端已启动并在运行
- 检查容器端日志是否有错误
- 容器端的网络可达性：`ping your-vps` 或 `curl ws://your-vps:8080`

## 已知限制

| 限制 | 说明 |
|------|------|
| **消息时序** | 管道模式下多条命令连续发送可能导致输出交错。PTY 模式已解决此问题 |
| **无压缩** | 大量输出（如 `cat largefile`）效率不高，行缓冲模式下逐字节读取 |
| **仅限 Shell** | 当前绑定交互式 Shell（支持自定义），无法直接转发其他 TCP 服务（如 MySQL）。可改造为通用 TCP 隧道 |
| **无审计日志** | 缺少结构化的命令审计记录 |

## 从旧版升级

| 版本 | 变更 | 迁移说明 |
|------|------|---------|
| v0.1.0 → v0.2.0 | 新增 TLS、认证、shell 参数 | 需 Python >= 3.10；旧 `python3 ws_relay.py` 仍可用，但不再维护 |
| v0.2.0 起 | CLI 统一为 `ws-tunnel`（后改名 `wsstunnel`） | 建议通过 `pip install -e .` 安装后使用 |
| v0.5.0 | 多后端支持、`@name` 路由、`LIST` 命令 | 旧版前端自动路由到第一个后端，完全向后兼容 |
| v0.6.0 | PTY 模式（默认）、管道模式回退（`--no-pty`） | 默认行为变更：新客户端使用 PTY，需 `--no-pty` 回退旧行为 |
| v0.6.2 | `USE` 命令切换默认后端、前端连接自动推送后端列表 | — |
| v0.7.0 | URL token 认证（`?token=xxx`）、`__RESIZE`/`__SIGNAL` 控制命令 | — |
| v0.7.1 | PTY 不再回显输入（避免双重显示）、默认终端 200x50 | — |
| v0.8.0 | 重构 `RelayState` 类，新增 `--wxpush` 微信通知 | 新增依赖 `httpx` |
| v0.9.0 | 包名统一为 `wsstunnel`，源目录 `ws_tunnel/` → `wsstunnel/` | `from wsstunnel import ...` |
| v0.9.1 | 显式包发现配置，修复新版 setuptools 打包 | — |
| v0.9.2 | 新增 `wsstunnel --version`，添加 `[dev]` 可选依赖（pytest） | `pip install wsstunnel[dev]` 安装测试依赖 |
| v0.18.0 | **文件传输**：`put`/`get` CLI 命令、`dl <path>` Shell 下载、Web 终端上传按钮、__FILE_* 协议 | 向后兼容，旧客户端不受影响。后端需升级至 v0.18.0 才能使用文件传输 |
| v0.18.5 | 上传到 shell 当前目录（追踪 `cd` 命令） | 不再写死 `/tmp/` |
| v0.18.8 | PTY 按键缓冲检测 `dl`，后台线程发送不阻塞命令行 | 解决移动端 `dl` 失效 |
| v0.18.14 | 移动端浮窗控制面板（⌨ 圆点展开 3 行布局） | 支持 Ctrl+A/E/U/K/W、方向键、复制全屏/本行 |
| v0.18.15 | 面板打开时收起系统键盘，禁止页面自动缩放 | 移动端体验优化 |

## 开发与测试

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest
```

## 微信推送通知

中继端支持通过 `--wxpush` 参数在**后端上线/下线**时发送微信通知。

1. 打开 [wxpusher 官网](https://wxpusher.zjiecode.com) 注册并创建应用
2. 在应用设置中获取 app_token
3. 启动中继时加上 `--wxpush` 参数：

```bash
wsstunnel relay --port 8080 --token mysecret \
    --wxpush https://wxpusher.zjiecode.com/api/send/message:你的app_token
```

当容器连接或断开时，中继会自动向微信发送通知消息。

## 发布到 PyPI

```bash
pip install build twine
python -m build
twine upload dist/*
```

## 许可证

MIT
