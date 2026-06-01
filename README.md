# ws-tunnel

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
│  ws-tunnel relay --port 8080      │
│                                   │
│  角色：中继转发                     │
│  依赖：Python 3.10+ + websockets   │
└──────────┬──────────────┬─────────┘
           │              │
      前端（Frontend）  后端（Backend）
      发送命令           注册并执行
           │              │
           │              ▼
           │     ┌────────────────────────┐
           │     │ 目标容器/设备（客户端）   │
           │     │ ws-tunnel client \      │
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
git clone <your-repo> && cd ws-tunnel
pip install -e .

# 启动中继（带 token + TLS）
ws-tunnel relay --port 8080 --token mysecret --cert /path/to/cert.pem --key /path/to/key.pem
```

### 2️⃣ 容器端

```bash
# 安装依赖
pip install websocket-client

# 下载客户端脚本
curl -O https://your-server/ws_tunnel/client.py

# 运行（带代理 + token）
python3 client.py --server wss://your-vps:443 --proxy http://127.0.0.1:18080 --token mysecret
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

## 安装

### 从源码安装

```bash
git clone <your-repo>
cd ws-tunnel
pip install -e .
```

安装后获得 `ws-tunnel` 命令和 `ws_tunnel` Python 包。

### 仅安装依赖

```bash
pip install -r requirements.txt
```

### 系统依赖

- Python >= 3.10
- 中继端依赖：`websockets`，`click`
- 客户端依赖：`websocket-client`，`click`

## 详细使用指南

### VPS 端（中继服务）

```bash
# 最小启动（不安全，仅内网测试）
ws-tunnel relay --port 8080

# 生产启动（认证 + TLS）
ws-tunnel relay \
    --port 443 \
    --token $(openssl rand -hex 32) \
    --cert /etc/letsencrypt/live/example.com/fullchain.pem \
    --key /etc/letsencrypt/live/example.com/privkey.pem

# 调试启动（看所有 WebSocket 消息）
ws-tunnel relay --port 8080 --token mysecret --verbose

# 静默运行（仅错误日志）
ws-tunnel relay --port 8080 --token mysecret --quiet
```

#### 所有 relay 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8080` | 监听端口 |
| `-t, --token` | — | 认证令牌，不设则不开启认证 |
| `--cert` | — | TLS 证书路径（提供后启用 wss://） |
| `--key` | — | TLS 私钥路径，未指定时使用 --cert |
| `--verbose` | — | 输出 DEBUG 级别日志 |
| `--quiet` | — | 仅输出 WARNING 及以上日志 |

### 容器端（客户端）

```bash
# 基本连接（直连）
ws-tunnel client --server ws://your-vps:8080 --token mysecret

# 通过 HTTP 代理连接（常见于受限容器）
ws-tunnel client \
    --server ws://your-vps:8080 \
    --proxy http://127.0.0.1:18080 \
    --token mysecret

# 使用 wss 加密连接 + 自签名证书
ws-tunnel client \
    --server wss://your-vps:443 \
    --token mysecret \
    --insecure

# 指定其他 shell（如 sh、zsh）
ws-tunnel client \
    --server ws://your-vps:8080 \
    --token mysecret \
    --shell /bin/zsh

# 缩短重连间隔（快速重试场景）
ws-tunnel client --server ws://... --token mysecret --reconnect 2
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
| `--verbose` | — | 输出 DEBUG 级别日志 |
| `--quiet` | — | 仅输出 WARNING 及以上日志 |

### 通过环境变量统一管理 token

```bash
export WS_TUNNEL_TOKEN=mysecret

# 之后 --token 会自动读取，无需再写
ws-tunnel relay --port 8080
ws-tunnel client --server ws://your-vps:8080
```

## 常见工作流

### 工作流 A：从零搭建生产隧道

```bash
# ── VPS 端 ──
# 1. 安装 ws-tunnel
pip install ws-tunnel

# 2. 用 Let's Encrypt 申请证书
sudo apt install certbot nginx
sudo certbot certonly --standalone -d tunnel.example.com

# 3. 生成随机 token
export WS_TUNNEL_TOKEN=$(openssl rand -hex 32)
echo "Token: $WS_TUNNEL_TOKEN"  # 保存好

# 4. 启动中继（端口 443）
ws-tunnel relay \
    --port 443 \
    --cert /etc/letsencrypt/live/tunnel.example.com/fullchain.pem \
    --key /etc/letsencrypt/live/tunnel.example.com/privkey.pem

# ── 容器端 ──
# 5. 连接（自动读取 WS_TUNNEL_TOKEN）
ws-tunnel client --server wss://tunnel.example.com:443
```

### 工作流 B：受限容器穿透（HTTP 代理场景）

```bash
# ── VPS 端（简单启动，只需端口）──
ws-tunnel relay --port 8080 --token mysecret

# ── 容器端 ──
# 容器通常有 HTTP 代理环境变量
echo $http_proxy  # 如 http://127.0.0.1:18080

# 连接（需指定代理）
ws-tunnel client \
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
ws-tunnel relay --port 4433 --cert cert.pem --key key.pem --token test123

# 3. 启动客户端（跳过证书验证）
ws-tunnel client --server wss://127.0.0.1:4433 --token test123 --insecure

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

### 工作流 D：系统服务（systemd 自动启动）

VPS 端的 `/etc/systemd/system/ws-tunnel.service`：

```ini
[Unit]
Description=ws-tunnel WebSocket Relay
After=network.target

[Service]
Type=simple
User=root
Environment=WS_TUNNEL_TOKEN=mysecret
ExecStart=/usr/local/bin/ws-tunnel relay --port 443 --cert /etc/letsencrypt/live/example.com/fullchain.pem --key /etc/letsencrypt/live/example.com/privkey.pem
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ws-tunnel
sudo journalctl -u ws-tunnel -f  # 查看日志
```

## 认证协议

中继端设置 `--token` 后启用认证：

| 角色 | 第一条消息 | 服务端响应 | 说明 |
|------|-----------|-----------|------|
| **后端（容器）** | `IAM_BACKEND:<token>` | — | 注册成功后开始转发 shell 输出 |
| **前端（第三方）** | `AUTH:<token>` | `AUTH_OK` / `AUTH_FAIL` | 收到 `AUTH_OK` 后即可发命令 |
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
ws-tunnel relay --port 443 \
    --cert /etc/letsencrypt/live/example.com/fullchain.pem \
    --key /etc/letsencrypt/live/example.com/privkey.pem

# 容器端（使用标准 CA 证书，无需额外参数）
ws-tunnel client --server wss://example.com:443 --token mysecret
```

### 方式二：自签名证书

```bash
# 1. 生成证书
openssl req -x509 -newkey rsa:2048 \
    -keyout key.pem -out cert.pem \
    -days 365 -nodes -subj "/CN=your-vps-ip"

# 2. VPS 端
ws-tunnel relay --port 443 --cert cert.pem --key key.pem --token mysecret

# 3. 容器端（必须加 --insecure 跳过验证）
ws-tunnel client --server wss://your-vps:443 --token mysecret --insecure
```

> **安全提示**：`--insecure` 跳过证书验证，中间人可以解密流量。建议只用于测试，或配合 token 认证使用。

### 方式三：nginx 反向代理（生产推荐）

优点：证书管理交给 nginx（自动续期），ws-tunnel 只需监听内网端口，无需 reload。

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
# ws-tunnel 在本地 8080 裸运行
ws-tunnel relay --port 8080 --token mysecret
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
from ws_tunnel import run_relay, run_client

# 启动中继（阻塞）
run_relay("0.0.0.0", 8080, token="mysecret")

# 启动中继 + TLS
run_relay(
    "0.0.0.0", 443,
    token="mysecret",
    cert_path="/path/to/cert.pem",
    key_path="/path/to/key.pem",
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
- 确认中继已在运行：`ps aux | grep ws-tunnel`

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
| **单后端** | 同一时间只能有一个容器连接。如需多容器，可启动多个 relay 实例 |
| **消息时序** | 多条命令连续发送可能导致输出交错。建议每条命令后等待响应 |
| **无压缩** | 大量输出（如 `cat largefile`）会逐字节发送，效率不高 |
| **无心跳** | 依赖 TCP keepalive 检测连接断开。长时间无命令不会主动断开 |
| **bash 独占** | 当前绑定了 `/bin/bash -i`，无法直接转发其他 TCP 服务（如 MySQL） |

## 从旧版升级

| 版本 | 变更 | 迁移说明 |
|------|------|---------|
| v0.1.0 → v0.2.0 | 新增 TLS、认证、shell 参数 | 需 Python >= 3.10；旧 `python3 ws_relay.py` 仍可用，但不再维护 |
| v0.2.0 起 | CLI 统一为 `ws-tunnel` | 建议通过 `pip install -e .` 安装后使用 |

## 发布到 PyPI

```bash
pip install build twine
python -m build
twine upload dist/*
```

## 许可证

MIT
