# wsstunnel 1.0.2 源码解析

> **分析对象**：`/usr/local/lib/python3.12/dist-packages/wsstunnel/`
> **规模**：Python 2705 行 + Web 终端 35517 字节
> **依赖**：`click` `httpx` `websocket-client` `websockets`
> **许可**：MIT ｜ 作者：yuanguangshan
> **分析日期**：2026-07-14（基于本机实际安装的 1.0.2）

---

## 一、三十秒速览

wsstunnel 是一个**反向 WebSocket 隧道**：被控端（后端）主动向外连接中继服务器，控制端（前端）连到同一个中继，双方通过中继交换数据。核心用途是**穿透 NAT / 内网，拿到一个可交互的远程 shell**。

它解决了传统反向 shell 的三个痛点：

| 痛点 | 传统反向 shell | wsstunnel |
|------|---------------|-----------|
| TUI 程序（vim/top/htop） | ❌ 无 TTY，全部崩坏 | ✅ 真 PTY，`pty.openpty()` |
| 多容器管理 | ❌ 一个端口一个 shell | ✅ 命名注册 + `USE`/`@name` 路由 |
| 断线恢复 | ❌ 一次断开就失联 | ✅ 指数退避重连 + 心跳降级检测 |

设计取向非常明确：**协议极简（纯文本前缀 + Base64）、零外部中间件、单文件可跑通**。

---

## 二、模块构成

```
wsstunnel/
├── __init__.py     10 行    导出 run_relay / run_client
├── __main__.py      9 行    python -m wsstunnel 入口
├── cli.py         457 行    Click 命令行：put / get / relay / client
├── client.py      737 行    后端客户端：连接、注册、PTY、重连、文件收发
├── relay.py      1119 行    中继服务：状态机、路由、认证、广播、静态页
├── security.py    373 行    安全层：角色、Token、IP 白名单、防爆破、审计
└── web/
    └── index.html 35517 B   xterm.js 网页终端（jsDelivr CDN 加载）
```

**职责边界很干净**：`client.py` 只管自己那台机器，`relay.py` 只管转发和状态，`security.py` 无外部依赖（纯标准库），三者通过**文本协议**解耦，不共享任何 Python 对象。

> 值得点赞的一点：`security.py` 明确自我约束「不改变现有协议，不增加复杂度」，且只用标准库。这让它可以被单独复用或替换。

---

## 三、角色模型与数据流

### 3.1 三种角色

| 角色 | 谁 | 第一条消息 | 能力 |
|------|-----|-----------|------|
| **Backend** | 被控端 `wsstunnel client` | `IAM_BACKEND:<token>:<name>:<mode>` | 提供 shell |
| **Frontend** | 控制端（Web 终端 / `put` / `get`） | `AUTH:<token>` 或 URL `?token=` | 下发命令 |
| **Relay** | 服务端 `wsstunnel relay` | — | 中转，不解析业务内容 |

### 3.2 数据流

```
 ┌────────────────┐                                    ┌─────────────────┐
 │  前端 Web 终端  │ ── AUTH:token ─────────────────►   │                 │
 │  前端 CLI      │ ◄── [Info] Connected backends ───  │   relay 1.0.2   │
 │  (put/get)     │ ── 命令 / __RESIZE / 文件帧 ────►  │   RelayState    │
 └────────────────┘                                    │                 │
                                                       │  backends{}     │
        ▲  输出（文本帧带 [@name] 标签 / 二进制帧）     │  frontends{}    │
        └───────────────────────────────────────────── │  *_targets{}    │
                                                       └────────┬────────┘
                                                                │
                                          IAM_BACKEND:<token>:<name>:pty
                                                                │
                                                       ┌────────▼─────────┐
                                                       │  后端 client.py   │
                                                       │  pty.openpty()   │
                                                       │  bash -i         │
                                                       └──────────────────┘
```

**关键设计**：中继对**所有前端广播**后端输出。多后端时文本输出会加 `[@name]` 前缀区分来源；二进制帧（PTY 原始输出）不加标签，因为没法在字节流里安全插入文本。

---

## 四、线协议全表

### 4.1 连接建立（HTTP 层）

`relay.py` 的 `_run_async` 用 `process_request=_http_request_handler` 拦截**非升级**的 HTTP 请求，直接吐出 Web 终端页面：

| 路径 | 行为 |
|------|------|
| `/` `/index.html` `/wstunnel` `/wsstunnel` | 返回 `index.html`（`text/html`） |
| 带 `Upgrade: websocket` 头 | 返回 `None`，放行升级 |
| 其他路径 | 返回 `None` → 404 |

这里有个**兼容性细节**处理得很到位：websockets 库 10.x 与 11+ 的 `process_request` 回调参数形状不同（`(path, headers)` vs `(connection, request)`），代码用鸭子类型判断：

```python
path = getattr(request, "path", None)   # Request 对象有 .path，Headers 没有
legacy = path is None
if legacy:
    path = connection          # 第一个参数是 path 字符串
    request_headers = request
else:
    request_headers = request.headers
```

### 4.2 后端注册：`IAM_BACKEND`

客户端构造（client.py:433-441）：

```python
mode_flag = "pipe" if no_pty else "pty"
if token and name:   reg_msg = f"IAM_BACKEND:{token}:{name}:{mode_flag}"
elif token:          reg_msg = f"IAM_BACKEND:{token}::{mode_flag}"
elif name:           reg_msg = f"IAM_BACKEND:{name}:{mode_flag}"
else:                reg_msg = f"IAM_BACKEND:{mode_flag}"
```

服务端解析（`_parse_backend_auth`）支持 **6 种格式**，全部向后兼容：

| 消息 | 解析结果 | 兼容目标 |
|------|---------|---------|
| `IAM_BACKEND:<token>:<name>:<mode>` | `(name, mode)` | ✅ 1.0.x 新格式 |
| `IAM_BACKEND:<token>:<name>` | `(name, "pipe")` | 旧客户端 |
| `IAM_BACKEND:<token>` | `(None→auto, "pipe")` | 旧客户端 |
| `IAM_BACKEND:<name>:<mode>` | `(name, mode)` | 无 token 模式 |
| `IAM_BACKEND:<mode>` | `(auto, mode)` | 无 token 模式 |
| `IAM_BACKEND` | `(auto, "pipe")` | 最古早版本 |

解析逻辑的巧妙之处——**从尾部试探 mode 字段**：

```python
mode = "pipe"
if parts and parts[-1] in ("pty", "pipe"):
    mode = parts.pop()
name = parts[0] if parts else None
```

这样新旧格式共用一条解析路径，不用写 if-else 版本分支。

**后端名安全校验**（relay.py:899）：名字会原样进入 `[Info]` 文本和 Web 终端 DOM，因此用正则白名单：

```python
_BACKEND_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
if name is not None and not _BACKEND_NAME_RE.fullmatch(name):
    logger.warning(f"Security: invalid backend name rejected, auto-naming: {name[:32]!r}")
    name = None   # 降级为 backend-N 自动命名
```

**重连顶替**（relay.py:906-915）：同名后端再次注册时，主动关掉旧连接并清理状态，然后新连接注册。旧 handler 醒来时因身份不匹配而跳过注销：

```python
async def _unregister_backend(self, name: str, ws: Any = None) -> None:
    if ws is not None and self.backends.get(name) is not ws:
        logger.debug(f"Backend '{name}' already replaced, skip unregister")
        return   # 避免误删新后端的注册
```

这个「身份校验」参数是**重连竞态的正确解法**——比常见的「先 pop 再注册」干净得多。

### 4.3 前端认证：`AUTH`

三条认证路径，按优先级：

| # | 路径 | 触发条件 | 代码位置 |
|---|------|---------|---------|
| 1 | **URL token** | 连接 URL 带 `?token=xxx` | relay.py:881-888 |
| 2 | **消息 token** | 首帧 `AUTH:<token>` | relay.py:952-965 |
| 3 | **无认证** | relay 未设 `--token` | 任何消息都放行 |

URL token 路径的好处：**浏览器可以直接把 token 放在地址栏里，Web 终端连上即认证**，无需在 JS 里硬编码或弹窗输入。

CLI 侧对应实现（`_connect_frontend`）也分三种情况，注释里还留了一句「relay may be too old」——说明作者考虑过版本混杂部署。

### 4.4 保活与控制帧

| 帧 | 方向 | 说明 |
|----|------|------|
| `__PING__` | 后端 → relay | 30 秒一次（`_HEARTBEAT_INTERVAL`） |
| `__PONG__` | relay → 后端 | 立即回包 |
| `__RESIZE:rows,cols` | 前端 → 后端 | 终端窗口大小变化，触发 `TIOCSWINSZ` |
| `__SIGNAL:SIGINT` | 前端 → 后端 | 发送信号给 shell 进程 |
| `__TEXT` / `__RAW` | 前端 → relay | 前端声明输出模式，不转发给后端 |

**一个重要取舍**（relay.py:1035-1036）：

```python
ping_interval=None,   # 关闭 WebSocket 协议层 ping
ping_timeout=None,
```

作者**主动关掉**了 RFC 6455 的协议层心跳，改用应用层 `__PING__/__PONG__`。启动日志也明确打印这一选择。原因大概率是协议层 ping 与二进制帧、代理中间件的交互不可控，而应用层心跳可观测、可调试。

### 4.5 文件传输帧

整套协议基于 **Base64 路径 + 分块**：

| 帧 | 方向 | 格式 |
|----|------|------|
| `__FILE_BEGIN:<b64path>:<size>` | 双向 | 开始传输 |
| `__FILE_OK:<b64path>:<size>` | 后端 → 前端 | 上传就绪确认 |
| `__FILE_CHUNK:<b64path>:<idx>:<b64data>` | 双向 | 数据块（64KB） |
| `__FILE_END:<b64path>:<total>` | 双向 | 传输结束 |
| `__FILE_DONE:<b64path>:<size>` | 后端 → 前端 | 完成确认 |
| `__FILE_ERROR:<b64path>:<msg>` | 双向 | 错误 |
| `__FILE_DOWNLOAD:<b64path>` | 前端 → 后端 | 请求下载 |
| `__FILE_CANCEL:<b64path>` | 前端 → 后端 | 取消上传 |

**协议命名的一个陷阱**，作者在注释里专门解释了：

> `__FILE_OK:` 用于上传确认，`__FILE_DONE:` 用于完成确认 —— 因为**下载方向也用 `__FILE_BEGIN:`/`__FILE_END:`**。如果上传确认复用 `__FILE_END:`，客户端 `put` 时会把后端回的确认误判成"后端给我发文件了"。

这是个真实的坑，靠命名错开解决，比加方向字段更省事。

**Shell 友好命令**：除了 Web 端的文件面板，后端还支持在 shell 里直接敲 `dl <path>` 触发下载。

---

## 五、client.py：后端客户端

### 5.1 重连状态机

```python
_RECONNECT_MAX_DELAY = 300   # 最大重连间隔 5 分钟
_HEARTBEAT_INTERVAL = 30     # 心跳间隔
_PIPE_READ_BUF = 4096        # 管道模式读缓冲
_FILE_CHUNK_SIZE = 65536     # 文件分块 64KB
```

主循环（client.py:411-492）的三段式结构：

```
while True:
    try:
        连接 → 注册 → 启动心跳 → 进入 PTY/pipe 模式（阻塞）
    except Exception:
        attempt += 1
        delay = min(reconnect_interval * 2**(attempt-1), 300)
        sleep(delay) → 重连
    else:
        if reconnect_event.is_set():    # ← 关键
            continue                    #   降级，重连
        break                           #   正常退出
```

**`try/except/else` 的这个 `else` 分支是整个文件最值得称道的地方**（client.py:478-492）：

```python
else:
    # _run_*mode 正常返回 ≠ 连接健康
    # PTY 线程退出 / 心跳失败 / WebSocket 关闭 都会触发
    # reconnect_event.set() 后让 run_*mode 优雅退出（无异常）
    # 这里检测到 event 已 set 就走重连，而不是 break 退出进程
    if reconnect_event.is_set():
        attempt += 1
        ...
        continue
```

**为什么这很重要**：心跳线程不直接调 `sys.exit()` 或抛异常，而是 `set()` 一个 Event，让 `run_*_mode` 的主循环自己检查标志位优雅退出。这是**多线程协作的正确姿势**——避免了"在子线程里强行终止主线程"这类经典错误。如果这里写成 `break`，那么心跳失败后进程会静默退出，`--daemon` 模式下就变成"活着但没连上"的僵尸。

### 5.2 PTY 模式（默认）

`_run_pty_mode`（client.py:499-652）的核心结构：

```python
master_fd, slave_fd = pty.openpty()
try:    cols, rows = os.get_terminal_size()
except OSError:  rows, cols = 50, 200     # 无终端时的兜底尺寸
_set_winsize(master_fd, rows, cols)

shell_proc = subprocess.Popen(
    [shell, "-i"],
    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
    close_fds=False,
    pass_fds=(master_fd,),
    preexec_fn=os.setsid,      # 脱离进程组，避免信号串扰
)
os.close(slave_fd)             # 父进程只留 master 端
```

输出读取线程用 `select` + 65536 字节读，然后 `ws.send_binary(data)`：

```python
rlist, _, _ = select.select([mfd], [], [], 0.5)   # 0.5s 超时，保证可响应退出
if rlist:
    data = os.read(mfd, 65536)
    if not data: break      # EOF = shell 退出
    ws.send_binary(data)
```

**0.5 秒的 select 超时**是关键——如果用阻塞读，线程无法感知 `reconnect_event`，断线时读线程会卡死。

**Shell 自动重生**（最多 5 次，client.py:644-651）：

```python
if not reconnect_event.is_set():
    restart_count += 1
    logger.warning(f"Shell respawning ({restart_count}/{max_restarts})...")
    time.sleep(2)
```

设计意图很清楚：**shell 崩了不必重连整个 WebSocket**，在本连接内重生即可，避免给中继和微信推送造成抖动。

**`dl` 命令的按键缓冲**（client.py:576-604）——这里有一段很有意思的取舍：

```python
# 缓冲按键，检测 dl 命令（前端拦截不可靠，特别是移动端）
if ch == "\x03":      _key_buffer = ""          # Ctrl+C 清空缓冲
elif ch.isprintable() or ch in ("\r", "\n", "\x7f"):
    _key_buffer += ch
    if len(_key_buffer) > _KEY_BUFFER_MAX:
        _key_buffer = _key_buffer[-_KEY_BUFFER_MAX:]   # 超长保留尾部
if ch in ("\r", "\n"):
    line = _key_buffer.replace("\r", "").replace("\n", "").strip()
    if line.startswith("dl "):
        threading.Thread(target=_send_file, args=(path, ws), daemon=True).start()
        os.write(master_fd, b"\x03\r")    # 清掉 bash 输入缓冲并出新提示符
        _key_buffer = ""
        continue                          # 不把 dl 这行发给 shell
```

因为 PTY 模式下每个按键都是独立的二进制帧，**前端无法可靠判断用户是否敲完了 `dl /path`**（尤其移动端软键盘）。所以后端自己做行缓冲，回车时判定，命中就拦截并用后台线程发文件，同时补一个 `Ctrl+C + \r` 让 bash 显示新提示符——用户完全感知不到这行命令没进过 shell。

`_KEY_BUFFER_MAX = 4096` 且超长时**保留尾部**，防止粘贴大段文本撑爆内存。

### 5.3 管道模式（`--no-pty`）

向后兼容路径，用普通管道 + 行缓冲：

```python
shell_proc = subprocess.Popen([shell, "-i"], stdin=PIPE, stdout=PIPE,
                              stderr=STDOUT, bufsize=0)
```

读取策略：**找到最后一个换行符才发送完整行**，缓冲区满 4096 字节且无换行则强行发送。

```python
last_nl = buf.rfind(b"\n")
if last_nl >= 0:
    ws.send(buf[:last_nl + 1].decode("utf-8", errors="replace"))
    buf = buf[last_nl + 1:]
elif len(buf) >= 4096:
    ws.send(buf.decode("utf-8", errors="replace"))
    buf.clear()
```

不支持 TUI、不支持窗口调整，但对**只跑批处理命令**的场景足够，且实现更简单、资源占用更低。

### 5.4 心跳与降级检测

```python
def _heartbeat(ws, reconnect_event):
    while not reconnect_event.is_set():
        try:
            ws.send("__PING__")
            time.sleep(_HEARTBEAT_INTERVAL)      # 30s
        except Exception:
            logger.warning("Heartbeat failed, triggering reconnection")
            reconnect_event.set()                # 只 set，不暴力终止
            break
```

守护线程 + 失败即 `set()`，与 5.1 的 `else` 分支形成闭环。

---

## 六、relay.py：中继服务

### 6.1 RelayState 状态机

```python
self.backends: dict[str, Any] = {}              # name → ws
self.backend_modes: dict[str, str] = {}         # name → "pty"|"pipe"
self.backend_connected_at: dict[str, float] = {}# name → 上线时间戳
self.frontends: set[Any] = set()
self.frontend_targets: dict[Any, str|None] = {} # ws → 选中的后端名
self.frontend_text_modes: dict[Any, bool] = {}  # ws → 是否剥离 ANSI
# 限制
self._max_frontends = 100
self._max_connections_per_ip = 10
self._max_file_size = 500 * 2**20               # 500MB
```

**目标解析的兜底**（`_resolve_target`）：前端没 `USE` 过任何后端时，自动用**第一个注册的后端**：

```python
target_name = self.frontend_targets.get(ws)
if target_name and target_name in self.backends:
    return (target_name, self.backends[target_name])
if self.backends:
    return next(iter(self.backends.items()))     # auto：第一个
return None
```

### 6.2 handler 主流程

```
连接进入
  ↓
① IP 白名单检查        → 不在名单：close(1008)
② 防爆破检查           → 被锁定：close(1008)
③ 连接数限制（本地 IP 豁免）
  ↓
④ URL ?token= 认证成功？ → 是 → 直接注册为前端
  ↓ 否
⑤ 等待首帧（30s 超时）
  ↓
  ├─ IAM_BACKEND → 后端注册 → 消息转发循环 → 注销
  ├─ AUTH:token  → 前端注册 → 命令路由循环 → 注销
  └─ 其他        → AUTH_FAIL + close(1008)
  ↓
finally：释放 IP 计数、清理后端注册、清理前端状态
```

**后端消息转发的三分支**（relay.py:918-944）：

```python
async for message in websocket:
    if isinstance(message, str) and message == "__PING__":
        await websocket.send("__PONG__"); continue
    if isinstance(message, bytes):        # PTY 原始输出
        await _forward_binary_to_frontends(..., actual_name if len(self.backends) > 1 else None)
        continue
    if isinstance(message, str) and message.startswith("__FILE_"):
        await _forward_to_frontends_untagged(...)   # 文件帧不能加标签
        continue
    await _forward_to_frontends(..., actual_name if len(self.backends) > 1 else None)
```

注意 `len(self.backends) > 1` 这个条件：**只有多后端时才加 `[@name]` 标签**。单后端场景下输出保持纯净，不会被无谓的前缀污染。

### 6.3 前端命令路由与权限矩阵

| 命令 | 语法 | 所需角色 |
|------|------|---------|
| `LIST` | 列举后端 | 所有 |
| `USE [name]` | 切换/查看当前后端 | 所有 |
| `@name <cmd>` | 临时发给指定后端 | ADMIN |
| `<cmd>` | 发给当前后端 | ADMIN |
| `__RESIZE:` / `__SIGNAL:` | 控制帧 | ADMIN |
| `__TEXT` / `__RAW` | 切换输出模式 | 所有 |
| `__FILE_BEGIN:` / `__FILE_CHUNK:` / `__FILE_DOWNLOAD:` | 文件传输 | FILE |
| 二进制帧（原始按键） | 直接输入 | ADMIN |

角色层级用 `IntEnum` 实现，权限检查就是一次比较：

```python
class Role(IntEnum):
    READONLY = auto()   # 1
    FILE = auto()       # 2
    ADMIN = auto()      # 3
```

**`_handle_frontend_msg` 里有一段注释暴露了一个真实修复**（relay.py:659-661）：

> 此前依赖 fall-through 碰巧让 ADMIN 走到普通命令分支转发，而 FILE 角色反而被末尾的 ADMIN 检查拦截——**专属角色形同虚设**。

这是把隐式控制流改成显式转发时顺手修掉的 bug，注释留得很诚实。

**文件大小前置校验**（relay.py:670-679）在权限检查**之前**，避免为大文件白跑一趟：

```python
if file_size is not None and file_size > self._max_file_size:
    await ws.send(f"__FILE_ERROR::File too large ({file_size} > {self._max_file_size} bytes)")
    return
```

### 6.4 广播与背压

`_gather_send` 是广播的核心，注释解释了两个并发陷阱：

```python
targets = list(frontends)      # 快照：避免遍历时集合被修改
...
await asyncio.gather(*(_send_one(f) for f in targets))
frontends -= dead              # 清理已断开的连接
```

1. **`list()` 快照**：防止 `RuntimeError: Set changed size during iteration`（并发注册/注销）
2. **`asyncio.gather` 并发**：避免**慢前端的 TCP 背压造成队头阻塞**（一个卡住的前端拖慢所有前端）

二进制转发还做了一次**共享计算优化**：

```python
text = _strip_ansi(data)       # ANSI 清洗只做一次，所有文本模式前端共享
async def _send(f):
    if frontend_text_modes.get(f):
        if text: await f.send(text)
    else:
        await f.send(data)
```

---

## 七、security.py：安全模型

### 7.1 五个组件

| 组件 | 职责 | 关键实现 |
|------|------|---------|
| `TokenManager` | Token 加载/校验/过期 | `hmac.compare_digest` 常数时间比较 |
| `IPAllowList` | IP/CIDR 白名单 | `ipaddress` 模块 |
| `BruteForceGuard` | 失败计数与延迟 | 惰性过期清理，防无界增长 |
| `DenyList` | 命令黑名单 | 只匹配首个单词 |
| `AuditLogger` | 审计日志 | JSON 输出到 logger |

### 7.2 Token 校验

```python
token_bytes = token.encode("utf-8", "replace")
for known, info in self._tokens.items():
    if hmac.compare_digest(known.encode("utf-8", "replace"), token_bytes):
        matched = info; break
```

用 `hmac.compare_digest` 而非 `==`，**规避时序侧信道**。过期判断还修过一个细节：

```python
# datetime.now(tz) 在 tz=None 时返回 naive 本地时间，
# 与 naive/aware 的 expires 都能安全比较（此前 aware 会 TypeError）
if datetime.now(expires.tzinfo) > expires:
```

`datetime.now(expires.tzinfo)` 这个写法很巧妙——**用 expires 自己的时区信息去生成"现在"**，无论配置里写的是 naive 还是 aware 格式都不会抛 `TypeError`。

### 7.3 防爆破的内存安全

```python
_CLEANUP_INTERVAL = 60.0    # 清理扫描最小间隔
_FAILURE_TTL = 600.0        # 失败记录无活动过期秒数
```

注释点明了动机：**避免公网扫描器长期灌入导致 `_failures` / `_locked_until` 无界增长**。这是长期运行服务的必要防护。

### 7.4 审计日志

输出结构化 JSON，前缀 `AUDIT`：

```
AUDIT {"event": "exec", "client_id": "default", "cmd": "ls -la", "role": "admin"}
AUDIT {"event": "auth_failed", "ip": "1.2.3.4", "token_prefix": "abc12345"}
```

命令只记前 100 字符（`cmd[:100]`）；文件上传**只在 BEGIN 记一次**，`__FILE_CHUNK` 不记——注释说明是"否则大文件每次传输产生上千条日志"。细节考虑周到。

---

## 八、Web 终端

`web/index.html`（35517 字节，单文件，xterm.js 从 jsDelivr CDN 加载）。

主要 JS 函数：

| 函数 | 功能 |
|------|------|
| `connect` | 建立 WebSocket、认证、重连 |
| `initTerminal` / `fitTerminal` | xterm 初始化与尺寸自适应 |
| `handleRelayText` | 处理 `[Info]` / `[Error]` / 后端列表 |
| `handleFileData` | 文件传输协议状态机 |
| `renderCluster` | 后端集群列表渲染 |
| `loadStoredConfig` / `saveConfig` | localStorage 持久化配置 |
| `mobileSend` / `fabSend` | 移动端输入栏（软键盘适配） |
| `stripBackendName` | 剥离 `[@name]` 标签 |
| `escHtml` | HTML 转义（防注入） |

功能覆盖：上传（`input type="file"`）、下载、窗口自适应、多后端切换、移动端适配、配置持久化。

> `escHtml` 的存在与后端名正则校验形成**双保险**——这正是纵深防御该有的样子。

---

## 九、设计亮点

按我认为的价值排序：

1. **`try/except/else` 的重连降级检测**（client.py:478-492）—— 把「正常返回」和「连接健康」正确区分开，是多线程协作的范本。
2. **身份校验式的注销**（`_unregister_backend(name, ws)`）—— 优雅解决重连顶替竞态，比"先删除再注册"干净。
3. **协议向后兼容的尾部试探解析**（`_parse_backend_auth`）—— 6 种格式共用一条解析路径，无需版本分支。
4. **`list()` 快照 + `asyncio.gather` 广播** —— 同时解决集合变异和队头阻塞两个并发问题。
5. **`dl` 命令的后端行缓冲拦截** —— 用 `Ctrl+C + \r` 补齐提示符，用户完全无感。
6. **关闭协议层 ping，改用应用层心跳** —— 可观测性优先的取舍，且主动在日志中说明。
7. **安全层零依赖 + 自我约束不改变协议** —— 可独立复用、可替换。

---

## 十、缺陷清单与修复建议

### 🔴 P0：后端连接的 IP 计数只增不减（已实证）

**位置**：`relay.py:868-878`（加）vs `relay.py:977-983`（放）

**问题**：连接数配额在 `handler` 开头对非本地 IP `+1`，但释放逻辑在 `finally` 里依赖 `self._client_info` 中有条目。而 **`_register_backend` 从不写入 `_client_info`**（只有 `_register_frontend` 会写），所以**后端连接断开时计数永不释放**。

**实验结果**（本机实测，构造 fake websocket 走完整生命周期）：

```
=== 后端 backend ===
  第  1 次 → ✅ 接受   | 计数 = 1
  ...
  第 10 次 → ✅ 接受   | 计数 = 10
  第 11 次 → ❌ 拒绝 Too many connections from your IP | 计数 = 10
  第 12 次 → ❌ 拒绝 Too many connections from your IP | 计数 = 10

=== 前端 frontend ===
  第  1~12 次 → ✅ 接受  | 计数 = 0   （始终正常释放）

后端最终残留计数 = 10
前端最终残留计数 = 0
```

**影响**：后端直连 relay（不经本地反代）时，**重连 10 次后该 IP 被永久封禁**，后端再也连不上——而 wsstunnel 的后端恰恰是**最需要频繁重连**的角色（网络抖动、watchdog 重启、沙箱回收）。

**为何线上没触发**：relay 前置 Nginx 反代时 `peer_ip` 是 `127.0.0.1`，走豁免分支。所以只有**直连 relay 端口**的部署会中招。

**修复**：让后端也注册进 `_client_info`，复用现有释放路径：

```python
async def _register_backend(self, ws: Any, name: str | None, mode: str) -> str:
    if not name:
        name = self._next_backend_name()
    # ↓ 新增：写入 _client_info，使 finally 的 IP 计数释放对后端也生效
    peer_ip = "0.0.0.0"
    try:
        peer_ip, _ = ws.remote_address
    except Exception:
        pass
    self._client_info[ws] = {
        "id": name, "ip": peer_ip,
        "role": Role.ADMIN, "connected_at": time.time(),
    }
    ...
```

同时建议把连接数限制从「只统计前端」改成前后端分开计数，或至少给后端一个更大的配额。

### 🟡 P1-1：防爆破锁定只有 3 秒，形同虚设

**位置**：`security.py:190-224`

```python
def __init__(self, max_attempts: int = 5, lockout_sec: float = 3.0):
```

连续失败 5 次后只锁 **3 秒**。攻击者每秒 5 次尝试 → 稳态速率约 **1.67 次/秒**，一天的尝试量约 14 万次。对一个 64 位十六进制 token 当然不够，但如果用户配了弱 token，这个防护基本没意义。

**建议**：改成指数退避（3s → 30s → 5min → 30min），或至少把默认锁提到 60 秒。

### 🟡 P1-2：无 token 模式下任何人可注册后端

**位置**：`relay.py:220`（`prefix = "IAM_BACKEND"` 当 `token is None`）、`relay.py:1079`

无 token 时，任何以 `IAM_BACKEND` 开头的消息都能注册为后端并广播给所有前端。代码已有警告日志：

```python
logger.warning("No token set — anyone can connect!")
```

这是刻意的向后兼容取舍，日志也做了提示。**建议**：无 token 时若绑定在 `0.0.0.0`（而非 `127.0.0.1`），把警告升级为更醒目的横幅，并在启动前短暂等待确认。

### 🟡 P1-3：relay 侧无主动心跳，死连接靠 TCP 超时回收

**位置**：`relay.py:1035-1036`

`ping_interval=None, ping_timeout=None` 关闭了协议层心跳，而 relay **只被动应答 `__PING__`**，从不主动探测。若后端的心跳线程因异常静默死亡（不抛异常、不 set event），relay 会一直持有这条半开连接，`LIST` 里显示"在线"，但实际发命令无响应——直到 TCP 层超时（可能是小时级）。

**建议**：relay 侧记录每个后端的最后一次 `__PING__` 时间，超过 90 秒未收到则主动关闭并触发下线通知。

### 🟢 P2-1：`close_fds=False` 与 `pass_fds` 冲突，产生 RuntimeWarning

**位置**：`client.py:529-530`

```python
close_fds=False,
pass_fds=(master_fd,),
```

Python 中 `pass_fds` 会隐式强制 `close_fds=True`，两者同时指定会告警。**本机日志已实测出现**：

```
/usr/lib/python3.12/subprocess.py:849: RuntimeWarning: pass_fds overriding close_fds.
  warnings.warn("pass_fds overriding close_fds.", RuntimeWarning)
```

无害但污染日志。删除 `close_fds=False` 即可（行为不变，因为 `pass_fds` 已经强制了）。

### 🟢 P2-2：前端连接无心跳

`_heartbeat` 只在后端实现。`put`/`get`/Web 终端的连接在**长时间无数据**（如大文件上传、用户离开）时可能被 Nginx 默认的 60 秒 `proxy_read_timeout` 切断。

**建议**：前端也发 `__PING__`（relay 已有应答逻辑），或调大反代的超时。

### 🟢 P2-3：`max_size=1MB` 与 `_max_file_size=500MB` 的隐式耦合

`_run_async` 设 `max_size=2**20`（单帧上限 1MB），而文件分块是 64KB → Base64 后约 **87KB**，加协议头远低于 1MB，当前安全。但如果将来调大 `_FILE_CHUNK_SIZE` 超过约 **750KB**，单帧会超限被静默断开，而错误信息不直观。

**建议**：加一条启动期断言或常量注释，把两者的约束关系写清楚。

### 🟢 P2-4：Token 明文存储 + O(n) 校验

`TokenManager.validate` 线性遍历所有 token 做常数时间比较。单/少 token 场景无关紧要，若 `token-file` 配了上百个 token，每次认证都是 O(n)——但真正的风险是**明文存储**：token 文件泄露等于全量凭证泄露。

**建议**：文档建议存储 SHA-256 摘要，校验时比较摘要。

---

## 十一、本环境运行对照

| 项 | 值 |
|----|-----|
| 版本 | 1.0.2（客户端与服务端已对齐） |
| 安装位置 | `/usr/local/lib/python3.12/dist-packages/wsstunnel/` |
| Python | 3.12（注意：`pip3` 默认指向 3.11，需用 `/usr/bin/python3 -m pip`） |
| 运行模式 | **PTY**（默认，未加 `--no-pty`） |
| 实例名 | `workbuddy_YYYYMMDD_HHMMSS`（watchdog 动态生成） |
| 服务端 | `wss://yuangs.cc/wsstunnel` |
| 保活 | `watchdog.sh`（10 秒探活 + 自动重启） |

**实测注册日志**（`/tmp/wstunnel.log`）：

```
INFO:wsstunnel.client:Connected to wss://yuangs.cc/wsstunnel
INFO:wsstunnel.client:Registered as backend (name=workbuddy_20260903_213826, mode=pty)
/usr/lib/python3.12/subprocess.py:849: RuntimeWarning: pass_fds overriding close_fds.
```

第 2 行的 `mode=pty` 正是 `mode_flag = "pipe" if no_pty else "pty"` 的落地印证；第 3 行即 P2-1 描述的告警。

### 与 P0 缺陷的关系

本环境走 `wss://yuangs.cc` → Nginx 反代 → 本地 relay，`peer_ip` 为 `127.0.0.1`，**命中豁免分支，不受 P0 影响**。但如果哪天改成容器直连 relay 端口暴露到公网，watchdog 的反复重连会在 10 次后把自己锁死——**这个坑现在记下来，比事后排查便宜得多**。

---

## 附：本次分析用到的验证方法

| 结论 | 验证方式 |
|------|---------|
| 版本与依赖 | `/usr/bin/python3 -m pip show wsstunnel` |
| PTY 模式确认 | 读取 `/tmp/wstunnel.log` 实跑日志 |
| P0 计数泄漏 | 构造 `FakeWS` 直接驱动 `RelayState.handler()`，后端/前端各 12 轮对照 |
| P2-1 告警 | 本机实际运行日志中存在该 RuntimeWarning |

---

*文档基于 wsstunnel 1.0.2 源码逐行阅读 + 实证验证撰写。*
