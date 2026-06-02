# ws-tunnel v0.9.0 重构之路：从功能完备到工程健壮

## 引言

在开源项目的生命周期中，有一个阶段是所有开发者都会遇到的：功能已经基本完备，核心逻辑跑得很好，但当你回头审视代码时，会发现它像一栋快速建成的房子——住着没问题，可一旦想加装电梯（新功能）或翻修管道（修 Bug），就发现图纸不够清晰、管线纠缠不清。

ws-tunnel 就走到了这个节点。作为一个通过 WebSocket + HTTP 代理穿透受限网络环境的远程 Shell 中继工具，它从 v0.1.0 的单文件脚本一路演进到 v0.8.0，积累了 PTY 支持、TLS 加密、多后端路由、微信推送通知等实用功能。然而，30 次提交、近 1000 行代码之后，零测试覆盖、闭包中的隐式状态管理、全局变量、低效的 I/O 模式等问题逐渐显现。

v0.9.0 版本的目标很明确：在保持公共 API 完全不变、协议行为零改动的前提下，对项目进行一次深度的工程化重构。本文将详细记录这次重构的每一个决策、每一处改动、以及背后的思考过程。

---

## 一、项目背景回顾

### 1.1 ws-tunnel 是什么

ws-tunnel 是一个轻量级的 WebSocket 反向隧道工具，专为极端受限的网络环境设计。它的典型使用场景是：你身处一个沙箱容器中（比如在线 IDE、CI Runner），该环境只允许通过 HTTP 代理发出站请求，而你希望获得一个远程 VPS 上的完整交互式 Shell。

系统由三个角色组成：

```
Frontend（前端）         Relay（中继，VPS）         Backend（后端，容器）
browser/websocat         relay.py                   client.py
      |                       |                          |
      |-- ws connect -------->|                          |
      |   AUTH:<token>        |                          |
      |<-- AUTH_OK ----------|                          |
      |                       |<--- ws connect ----------|
      |                       |    IAM_BACKEND:token:name |
      |                       |    (via HTTP proxy)       |
      |--- "whoami" -------->|--- "whoami" ------------->|
      |                       |                          |  bash 执行
      |<-- output -----------|<-- output ---------------|
```

- **Relay**（`relay.py`）：运行在 VPS 上的异步 WebSocket 服务器，负责认证、多后端管理和消息路由
- **Client**（`client.py`）：运行在容器内的同步 WebSocket 客户端，启动 PTY shell 并转发 I/O
- **Frontend**：任何 WebSocket 客户端（浏览器、websocat、Python 脚本），连接到 Relay 获取 Shell

### 1.2 v0.8.0 的代码面貌

在重构之前，项目结构如下：

```
ws_tunnel/
  __init__.py      # 10 行，导出 run_relay, run_client
  __main__.py      # 9 行，支持 python -m ws_tunnel
  cli.py           # 98 行，Click CLI 入口
  client.py        # 336 行，同步 WebSocket 客户端
  relay.py         # 490 行，异步 WebSocket 中继服务器
```

总共约 950 行 Python 代码，4 个依赖（websocket-client、websockets、click、httpx），0 个测试文件。

### 1.3 识别出的问题

通过系统的代码审查，我们识别出以下 8 个需要优化的问题，按优先级排列：

1. **零测试覆盖**：整个项目没有一个自动化测试
2. **闭包中的隐式状态**：`relay.py` 的核心逻辑是一个 ~140 行的闭包，共享状态散布在闭包变量和全局变量中
3. **过长的消息处理函数**：`_handle_frontend_message` 有近 100 行的 if-elif 链
4. **管道模式 I/O 低效**：逐字节读取 shell 输出（`read(1)`）
5. **进程清理不够优雅**：直接 SIGKILL shell 子进程，不留清理机会
6. **依赖文件不同步**：`requirements.txt` 缺少 `httpx`
7. **缺少 --version 参数**：CLI 无法查询版本号
8. **缺少类型提示和文档**：内部函数无参数/返回值类型标注

---

## 二、重构策略：自顶向下，保持兼容

### 2.1 核心原则

重构的第一原则是**保持公共 API 不变**。这意味着：

- `run_relay(host, port, token, cert_path, key_path, wxpush)` 的签名和行为不变
- `run_client(server_url, proxy, reconnect_interval, token, insecure, shell, name, no_pty)` 不变
- CLI 命令 `ws-tunnel relay` 和 `ws-tunnel client` 的所有参数不变
- WebSocket 协议格式不变，新旧客户端可以互相通信

这是一个重要的约束。它意味着我们不能"推倒重来"，而是在现有骨架上进行精准的手术。每一次改动都要确保外部行为不变。

### 2.2 实施顺序

重构的顺序至关重要。如果先写测试再重构，测试可能会绑定到即将被替换的内部结构上，导致测试本身也需要大量修改。正确的顺序应该是：

```
1. 清理简单问题（依赖同步、删除过时文件）
2. 重构 relay.py（提取类、拆分函数）
3. 优化 client.py（I/O 性能、进程管理）
4. 完善 CLI（--version、类型提示）
5. 编写测试套件（基于重构后的清晰结构）
6. 验证（运行测试、检查 CLI）
```

这个顺序确保每一步都建立在前一步的稳固基础上，测试是基于最终结构编写的，不需要反复调整。

---

## 三、relay.py 的核心重构

### 3.1 问题：闭包中的隐式状态

重构前的 `relay.py` 中，核心逻辑被封装在 `_make_handler()` 函数返回的闭包中：

```python
_backend_counter = 0  # 模块级全局变量

def _make_handler(token, notifier=None):
    backends: dict = {}        # 闭包变量
    backend_modes: dict = {}   # 闭包变量
    frontends: set = set()     # 闭包变量
    frontend_targets: dict = {} # 闭包变量
    _count = 0                 # 闭包变量

    async def handler(websocket, _path=None):
        nonlocal backends, frontends, _count
        # ... 140 行逻辑

    return handler
```

这种模式的问题在于：

- **全局变量 `_backend_counter`**：如果未来需要多个 Relay 实例（比如不同端口运行多个中继），它们会共享同一个计数器，产生名称冲突
- **闭包变量不可外部访问**：无法从外部检查 `backends` 或 `frontends` 的状态，测试时只能通过模拟完整的 WebSocket 连接来验证
- **所有逻辑集中在一个闭包中**：`handler` 函数同时负责认证、角色检测、后端消息循环、前端消息处理，职责过重

### 3.2 解决方案：RelayState 类

我们将闭包中的所有状态和行为提取为一个 `RelayState` 类：

```python
class RelayState:
    """管理中继服务的所有连接状态和消息路由。"""

    def __init__(self, token, notifier=None):
        self.token = token
        self.notifier = notifier
        self.backends: dict[str, Any] = {}
        self.backend_modes: dict[str, str] = {}
        self.frontends: set[Any] = set()
        self.frontend_targets: dict[Any, str | None] = {}
        self._counter: int = 0

    def _next_backend_name(self) -> str:
        self._counter += 1
        return f"backend-{self._counter}"

    async def handler(self, websocket, _path=None):
        # 主入口，负责认证和角色分发
        ...

    # ... 其他方法
```

这个改动带来的好处是立竿见影的：

1. **状态隔离**：每个 `RelayState` 实例有自己独立的 `_counter`，多个实例互不干扰
2. **可测试性**：可以直接创建 `RelayState` 实例，调用其方法，检查其属性，无需模拟完整的 WebSocket 握手
3. **代码组织**：相关的方法聚集在同一个类中，IDE 的代码导航更加方便

### 3.3 拆分消息处理函数

原始的 `_handle_frontend_message` 函数是一个近 100 行的 if-elif 链：

```python
async def _handle_frontend_message(ws, message, backends, frontends,
                                    backend_modes, frontend_targets):
    if isinstance(message, bytes):
        # 二进制帧处理... 10 行
        return

    msg = message.strip()

    if msg.upper() == "LIST":
        # LIST 处理... 5 行
        return

    if msg.upper() == "USE" or msg.upper().startswith("USE "):
        # USE 处理... 25 行
        return

    if msg.startswith("__RESIZE:") or msg.startswith("__SIGNAL:"):
        # 控制命令处理... 10 行
        return

    if msg.startswith("@"):
        # @name 路由... 20 行
        return

    # 普通命令处理... 15 行
```

虽然逻辑清晰，但当某个分支需要修改或调试时，开发者需要在长函数中定位具体位置。更关键的是，这个函数接收 6 个参数（其中 4 个是可变容器），每个分支都直接修改这些参数——这是典型的"参数过多"和"副作用过多"的代码味道。

重构后，这些分支变成了 `RelayState` 的独立方法：

```python
class RelayState:
    async def _handle_frontend_msg(self, ws, message):
        """路由前端消息到对应处理方法。"""
        if isinstance(message, bytes):
            await self._forward_binary_to_backend(ws, message)
            return
        msg = message.strip()
        if msg.upper() == "LIST":
            await self._handle_list(ws)
        elif msg.upper() == "USE" or msg.upper().startswith("USE "):
            await self._handle_use(ws, msg)
        elif msg.startswith("__RESIZE:") or msg.startswith("__SIGNAL:"):
            await self._handle_control(ws, msg)
        elif msg.startswith("@"):
            await self._handle_at_cmd(ws, msg)
        else:
            await self._send_to_current_backend(ws, msg)

    async def _handle_list(self, ws): ...
    async def _handle_use(self, ws, msg): ...
    async def _handle_at_cmd(self, ws, msg): ...
    async def _handle_control(self, ws, msg): ...
    async def _send_to_current_backend(self, ws, msg): ...
    async def _forward_binary_to_backend(self, ws, data): ...
```

路由方法 `_handle_frontend_msg` 现在只做分发，每个具体行为被隔离在独立方法中。方法名即文档，代码的自解释性大大提升。

### 3.4 后端注册/注销的生命周期管理

原始代码中，后端的注册和注销逻辑分散在 `handler` 闭包的不同位置：注册在检测到 `IAM_BACKEND` 消息后，注销在 `except websockets.exceptions.ConnectionClosed` 后和 `finally` 块中。

重构后，这两个操作被集中到 `_register_backend` 和 `_unregister_backend` 方法中：

```python
async def _register_backend(self, ws, name, mode):
    if not name:
        name = self._next_backend_name()
    self.backends[name] = ws
    self.backend_modes[name] = mode
    logger.info(f"Backend registered: '{name}' mode={mode}")
    if self.notifier:
        await self.notifier.send(f"✅ ws-tunnel: 后端 '{name}' 已上线 ({mode})")
    await _broadcast_backend_list(
        self.frontends, self.backends, self.backend_modes,
        self.frontend_targets,
    )
    return name

async def _unregister_backend(self, name):
    self.backends.pop(name, None)
    self.backend_modes.pop(name, None)
    # 清除关联的前端目标
    for f in list(self.frontend_targets):
        if self.frontend_targets.get(f) == name:
            self.frontend_targets[f] = None
    logger.info(f"Backend disconnected: '{name}'")
    if self.notifier:
        await self.notifier.send(f"❌ ws-tunnel: 后端 '{name}' 已下线")
    await _broadcast_backend_list(
        self.frontends, self.backends, self.backend_modes,
        self.frontend_targets,
    )
```

这使得 `handler` 方法的主循环变得极其清晰：

```python
async def handler(self, websocket, _path=None):
    # 1. 认证 + 角色检测
    backend_info = _parse_backend_auth(first, self.token)
    if backend_info:
        name, mode = backend_info
        actual_name = await self._register_backend(websocket, name, mode)
        try:
            async for message in websocket:
                # 转发后端输出给前端
                ...
        except websockets.exceptions.ConnectionClosed:
            pass
        await self._unregister_backend(actual_name)
    elif _is_frontend_auth(first, self.token):
        await self._register_frontend(websocket)
```

注册和注销成对出现，生命周期一目了然。

### 3.5 保持独立：模块级函数

并非所有函数都需要放入类中。以下函数被有意保留为模块级函数：

- **`_parse_backend_auth(msg, token)`**：纯函数，输入字符串输出元组，无副作用，非常适合单元测试
- **`_is_frontend_auth(msg, token)`**：同上
- **`_forward_to_frontends(frontends, message, tag)`**：接收参数而非依赖实例状态
- **`_send_backend_list(ws, backends, ...)`**：同上
- **`_create_ssl_context(cert_path, key_path)`**：工厂函数，与状态无关
- **`_WxPushNotifier`**：独立的通知类，有自己的状态（url、key），不应绑定到 RelayState

这个决策背后的原则是：**如果一个函数不需要访问实例状态，就不应该成为实例方法。** 模块级函数更容易测试（直接调用即可），也更容易复用。

---

## 四、client.py 的性能优化

### 4.1 管道模式：从逐字节到缓冲读取

ws-tunnel 的客户端支持两种 shell 模式：PTY 模式（默认）和管道模式（`--no-pty`）。管道模式是为向后兼容而保留的，适用于不需要 TUI 程序支持的简单场景。

重构前，管道模式的输出读取方式是这样的：

```python
def read_and_forward():
    buf = bytearray()
    while True:
        byte = shell_proc.stdout.read(1)  # 每次读 1 字节
        if not byte:
            break
        buf.extend(byte)
        if byte == b"\n" or len(buf) >= 4096:
            ws.send(buf.decode("utf-8", errors="replace"))
            buf.clear()
```

`shell_proc.stdout.read(1)` 意味着每读一个字节就发生一次系统调用。当 shell 输出大量数据时（比如 `cat /var/log/syslog` 或 `find /`），这会产生极高的系统调用频率，严重影响性能。

重构后，改为 4096 字节的缓冲读取：

```python
_PIPE_READ_BUF = 4096

def read_and_forward():
    buf = bytearray()
    while True:
        data = shell_proc.stdout.read(_PIPE_READ_BUF)  # 每次读 4KB
        if not data:
            break
        buf.extend(data)
        # 找到最后一个换行符，发送完整行
        last_nl = buf.rfind(b"\n")
        if last_nl >= 0:
            ws.send(buf[:last_nl + 1].decode("utf-8", errors="replace"))
            buf = buf[last_nl + 1:]
        elif len(buf) >= 4096:
            # 缓冲区满且无换行，直接发送（处理超长行）
            ws.send(buf.decode("utf-8", errors="replace"))
            buf.clear()
```

这个改动带来了几个提升：

1. **系统调用减少 ~4000 倍**：每读 4096 字节才一次系统调用，而非每字节一次
2. **行边界保留**：通过 `rfind(b"\n")` 找到最后一个换行符，确保发送的是完整行
3. **超长行处理**：当一行超过 4096 字节且没有换行符时，直接发送避免无限缓冲
4. **剩余数据处理**：循环结束后检查 `buf` 是否还有未发送的数据

### 4.2 进程清理：从暴力 SIGKILL 到优雅终止

PTY 模式的 `finally` 块中，原始代码直接调用 `shell_proc.kill()`：

```python
finally:
    reconnect_event.set()
    shell_proc.kill()  # 直接 SIGKILL
    ws.close()
    os.close(master_fd)
```

SIGKILL 是不可捕获、不可忽略的信号。进程收到 SIGKILL 后会立即终止，没有机会执行任何清理操作——不写 bash history、不清理临时文件、不通知子进程。

重构后，我们引入了 `_terminate_process` 辅助函数：

```python
def _terminate_process(proc, timeout=5.0):
    """优雅终止子进程：先 SIGTERM，超时后 SIGKILL。"""
    try:
        proc.terminate()  # 发送 SIGTERM
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()       # 超时后 SIGKILL
        proc.wait()       # 回收资源，避免僵尸进程
```

这个函数的行为是：

1. 先发送 SIGTERM（可捕获），给进程 5 秒的清理时间
2. 如果进程在 5 秒内正常退出，皆大欢喜
3. 如果超时，发送 SIGKILL 强制终止
4. 无论哪种情况，都调用 `wait()` 回收子进程资源

在 PTY 模式和管道模式中都使用了这个函数：

```python
# PTY 模式
finally:
    reconnect_event.set()
    _terminate_process(shell_proc)
    ws.close()
    os.close(master_fd)

# 管道模式
finally:
    reconnect_event.set()
    _terminate_process(shell_proc)
    ws.close()
```

两处使用同一个函数，消除了代码重复，也确保了进程管理策略的一致性。

---

## 五、CLI 和工程化改进

### 5.1 添加 --version 参数

这是一个看似简单但经常被忽略的功能。在 v0.8.0 之前，用户无法通过命令行查询 ws-tunnel 的版本号。

实现方式使用了 Click 内置的 `version_option` 装饰器：

```python
@click.group()
@click.version_option(package_name="wsstunnel")
def cli():
    """WebSocket Tunnel - 远程 Shell 中继工具"""
```

`package_name="wsstunnel"` 参数告诉 Click 从 Python 包的元数据（`pyproject.toml` 中的 `version` 字段）读取版本号。这意味着版本号只需要在 `pyproject.toml` 中维护一处，CLI 会自动同步。

效果：

```bash
$ ws-tunnel --version
ws-tunnel, version 0.9.0
```

### 5.2 全面的类型提示

Python 3.10 引入了 `X | Y` 联合类型语法（不再需要 `Optional[X]` 或 `Union[X, Y]`），而 ws-tunnel 的最低要求正好是 Python 3.10，因此我们可以充分利用这个语法。

重构前的函数签名：

```python
def _send_signal(shell_proc, sig_name):
    ...

def _heartbeat(ws, reconnect_event):
    ...
```

重构后：

```python
def _send_signal(shell_proc: subprocess.Popen, sig_name: str) -> None:
    ...

def _heartbeat(ws: websocket.WebSocket, reconnect_event: threading.Event) -> None:
    ...
```

所有公共函数和内部函数都补充了完整的类型提示。这不仅有助于 IDE 的自动补全和静态检查（mypy/pyright），更重要的是让代码本身成为文档——函数签名就能告诉读者期望的参数类型和返回值类型。

### 5.3 完善的文档字符串

除了类型提示，我们还为所有内部函数补充了 Google 风格的 docstring：

```python
async def _forward_to_frontends(
    frontends: set[Any],
    message: str,
    tag: str | None = None,
) -> None:
    """转发文本消息给所有前端，可选加 ``[@tag]`` 标签。

    自动清理已断开的前端连接。

    Args:
        frontends: 前端 WebSocket 连接集合（会被原地修改）。
        message: 要转发的文本消息。
        tag: 可选标签，多后端时区分来源。
    """
```

文档字符串中特别标注了"会被原地修改"这样的副作用，这对后续维护者理解代码行为至关重要。

### 5.4 依赖文件同步和清理

项目中存在一个 `requirements.txt` 文件，内容为：

```
websocket-client>=1.3.0
websockets>=10.0
click>=8.0
```

而 `pyproject.toml` 中声明的依赖是：

```toml
dependencies = [
    "websocket-client >= 1.3.0",
    "websockets >= 10.0",
    "click >= 8.0",
    "httpx >= 0.24.0",
]
```

`requirements.txt` 缺少了 `httpx`（这是 `--wxpush` 功能的依赖）。在 v0.8.0 中添加微信推送功能时忘记同步了。

修复方法很简单，补充 `httpx>=0.24.0` 到 `requirements.txt`。

此外，项目根目录下还有两个过时的 `.egg-info` 目录：

- `ws_tunnel.egg-info/`：来自 v0.1.0 时代，包名还是 `ws-tunnel`，作者信息是占位符
- `wsstunnel.egg-info/`：来自 v0.4.0，版本已经过时

这两个都是构建产物，应该由 `.gitignore` 排除（实际上 `.gitignore` 中已经有 `*.egg-info/`，但它们在添加 gitignore 规则之前就已经被跟踪了）。直接删除即可。

---

## 六、从零到 68 个测试

### 6.1 测试策略

在没有测试的项目上开始写测试，最重要的是选择正确的切入点。我们采用了自底向上的策略：

1. **协议解析函数**（最容易测试，纯函数）→ `test_protocol.py`
2. **客户端工具函数**（信号映射、PTY 操作）→ `test_client.py`
3. **中继核心逻辑**（状态管理、消息路由）→ `test_relay.py`
4. **CLI 参数解析**（Click 测试工具）→ `test_cli.py`

### 6.2 协议解析测试

`_parse_backend_auth` 和 `_is_frontend_auth` 是纯函数——给定输入，返回输出，无副作用。这是最理想的测试对象。

```python
class TestParseBackendAuth:
    def test_full_format_pty(self):
        result = _parse_backend_auth("IAM_BACKEND:secret:mybox:pty", "secret")
        assert result == ("mybox", "pty")

    def test_token_and_name_no_mode(self):
        """旧客户端：有 token 和 name 但没有 mode 标记。"""
        result = _parse_backend_auth("IAM_BACKEND:secret:mybox", "secret")
        assert result == ("mybox", "pipe")

    def test_binary_message_rejected(self):
        result = _parse_backend_auth(b"IAM_BACKEND:secret:mybox:pty", "secret")
        assert result is None

    def test_wrong_token(self):
        result = _parse_backend_auth("IAM_BACKEND:wrong:mybox:pty", "secret")
        assert result is None
```

这些测试覆盖了 5 种合法的消息格式和各种边界情况（二进制消息、错误 token、空字符串、多余空格）。因为 `_parse_backend_auth` 被保留为模块级纯函数，测试可以直接调用，不需要任何 mock。

### 6.3 RelayState 测试

重构后的 `RelayState` 类使得测试变得极其自然。我们创建了一个简单的 `MockWebSocket`：

```python
class MockWebSocket:
    """模拟 WebSocket 连接，记录发送的消息。"""

    def __init__(self):
        self.sent: list[str | bytes] = []
        self.closed: bool = False

    async def send(self, message):
        self.sent.append(message)

    async def close(self, code=1000, reason=""):
        self.closed = True

    def __hash__(self):
        return id(self)

    def __eq__(self, other):
        return self is other
```

有了这个 Mock，测试可以直接操作 `RelayState` 的内部状态：

```python
class TestRelayState:
    @pytest.mark.asyncio
    async def test_register_backend(self):
        state = RelayState(token="secret")
        ws = MockWebSocket()
        name = await state._register_backend(ws, "mybox", "pty")
        assert name == "mybox"
        assert "mybox" in state.backends
        assert state.backend_modes["mybox"] == "pty"

    @pytest.mark.asyncio
    async def test_unregister_clears_frontend_targets(self):
        state = RelayState(token="secret")
        backend_ws = MockWebSocket()
        frontend_ws = MockWebSocket()
        await state._register_backend(backend_ws, "mybox", "pty")
        state.frontends.add(frontend_ws)
        state.frontend_targets[frontend_ws] = "mybox"
        await state._unregister_backend("mybox")
        assert state.frontend_targets[frontend_ws] is None
```

对比一下：如果测试原始的闭包版本，我们需要调用 `_make_handler()` 获取闭包，然后模拟完整的 WebSocket 连接生命周期。而重构后，可以直接创建 `RelayState` 实例，调用特定方法，检查特定属性——粒度精细得多。

### 6.4 消息路由测试

消息路由是 ws-tunnel 最核心的功能之一。前端通过不同的命令格式与后端交互，每一条路径都需要测试：

```python
class TestRelayMessageRouting:
    @pytest.mark.asyncio
    async def test_handle_list(self):
        # LIST 命令：列举所有后端
        ...

    @pytest.mark.asyncio
    async def test_handle_use_switch(self):
        # USE <name>：切换后端
        ...

    @pytest.mark.asyncio
    async def test_handle_at_command(self):
        # @name <cmd>：临时路由
        ...

    @pytest.mark.asyncio
    async def test_handle_control_resize(self):
        # __RESIZE:rows,cols：窗口大小
        ...

    @pytest.mark.asyncio
    async def test_forward_binary_to_backend(self):
        # 二进制帧：原始按键输入
        ...
```

每个路由分支都有成功路径和失败路径的测试。例如 `_handle_at_command` 测试了三种情况：正常路由、缺少空格（`@mybox` 无命令）、目标后端不存在。

### 6.5 死连接清理测试

死连接清理是一个容易被忽略但又至关重要的功能。当 WebSocket 连接意外断开时，relay 需要从 `frontends` 集合中移除它，否则后续的广播操作会不断触发异常。

为此，我们创建了一个 `FailingWebSocket`：`send()` 方法永远抛出 `ConnectionError`：

```python
class FailingWebSocket(MockWebSocket):
    async def send(self, message):
        raise ConnectionError("Connection closed")
```

测试验证了转发函数在遇到死连接时能正确清理：

```python
@pytest.mark.asyncio
async def test_forward_removes_dead_connections(self):
    ws_ok = MockWebSocket()
    ws_dead = FailingWebSocket()
    frontends = {ws_ok, ws_dead}
    await _forward_to_frontends(frontends, "output")
    assert ws_dead not in frontends  # 死连接被移除
    assert ws_ok in frontends        # 活连接保留
```

### 6.6 CLI 测试

CLI 测试使用了 Click 自带的 `CliRunner`，它可以在不启动真实进程的情况下测试命令行参数解析：

```python
from click.testing import CliRunner
from ws_tunnel.cli import cli

class TestCLI:
    def setup_method(self):
        self.runner = CliRunner()

    def test_version(self):
        result = self.runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "wsstunnel" in result.output

    def test_client_requires_server(self):
        result = self.runner.invoke(cli, ["client"])
        assert result.exit_code != 0
        assert "Missing option" in result.output or "--server" in result.output
```

这些测试确保了 CLI 的各个参数都能正确解析，必填参数在缺失时报错，帮助信息完整。

### 6.7 pytest 配置

为了让测试运行顺畅，我们在 `pyproject.toml` 中添加了配置：

```toml
[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

`asyncio_mode = "auto"` 是关键配置——它会自动检测标记了 `@pytest.mark.asyncio` 的测试函数并用 asyncio 事件循环运行，无需手动 `asyncio.run()`。dev 依赖中同时声明了 `pytest` 和 `pytest-asyncio`，通过 `pip install -e ".[dev]"` 一步安装。

最终结果：**68 个测试，全部通过，耗时 0.09 秒。**

```
tests/test_cli.py      9 passed
tests/test_client.py  10 passed
tests/test_protocol.py 19 passed
tests/test_relay.py   30 passed
======================== 68 passed in 0.09s =========================
```

---

## 七、重构的哲学思考

### 7.1 何时重构

这次重构的时机选择值得讨论。ws-tunnel 在 v0.7.0 添加了多后端路由后，核心功能已经稳定。v0.7.x 系列主要是 bug 修复（PTY 输出缓冲、bash 退出问题），v0.8.0 添加了微信推送。功能增长放缓，这正是重构的最佳时机：

- 功能足够丰富，重构的覆盖面足够广
- 核心逻辑已经过实际使用验证，不会因为理解偏差而改错
- 还没有太多外部用户依赖内部 API（因为本来就没有公共 API 文档之外的接口）

### 7.2 纯函数优先

在重构中，我们始终遵循一个原则：**如果一个函数可以写成纯函数，就应该写成纯函数。**

`_parse_backend_auth` 和 `_is_frontend_auth` 就是最典型的例子。它们接收消息和 token，返回解析结果，不修改任何外部状态。这意味着：

- 测试时不需要 mock 任何东西
- 调用时不会有意外的副作用
- 可以在任何上下文中安全复用

相比之下，如果它们被做成 `RelayState` 的方法（依赖 `self.token`），虽然代码稍微简洁一点（少传一个参数），但测试时必须先创建实例，增加了不必要的耦合。

### 7.3 渐进式重构

这次重构的一个关键特点是"渐进式"。我们没有一次性重写所有代码，而是分步进行：

1. 先改结构（提取类、拆分函数）
2. 再改行为（优化 I/O、改进进程管理）
3. 最后加测试（基于新结构编写）

每一步都可以独立验证——`python -c "from ws_tunnel import run_relay, run_client"` 确认导入正常，`ws-tunnel --help` 确认 CLI 正常。如果任何一步出了问题，可以快速定位到具体的改动。

---

## 八、数据与度量

### 8.1 代码行数变化

| 文件 | 改前 | 改后 | 变化 |
|------|------|------|------|
| `relay.py` | 490 行 | ~520 行 | 逻辑更清晰，类型提示和文档增加了行数 |
| `client.py` | 336 行 | ~350 行 | 新增 `_terminate_process`，`_run_pipe_mode` 更长但更高效 |
| `cli.py` | 98 行 | ~130 行 | 类型提示和 `--version` |
| `tests/` | 0 行 | ~665 行 | 全新 |
| **总计** | ~924 行 | ~1665 行 | +80% |

代码行数增加了约 80%，但其中大部分（~665 行）是测试代码。实际生产代码的增量主要来自类型提示和文档字符串——这是对可维护性的投资，而非功能膨胀。

### 8.2 测试覆盖范围

| 模块 | 测试数 | 覆盖的关键路径 |
|------|--------|--------------|
| 协议解析 | 19 | 5 种后端格式、前端认证、边界情况 |
| 中继状态 | 11 | 注册、注销、自动命名、目标解析 |
| 消息路由 | 13 | LIST、USE、@name、RESIZE、SIGNAL、普通命令 |
| 广播函数 | 7 | 文本/二进制转发、死连接清理、列表广播 |
| 客户端工具 | 10 | 信号映射、PTY 窗口、重连退避 |
| CLI | 9 | version、help、参数校验 |

总计 68 个测试，覆盖了所有核心业务逻辑的公共路径和主要错误路径。

### 8.3 性能改进预期

管道模式的 I/O 优化预期效果：

| 指标 | 改前 | 改后 | 改善 |
|------|------|------|------|
| 系统调用频率 | 每 1 字节 | 每 4096 字节 | ~4000x |
| `cat 1MB_file` 的系统调用数 | ~1,000,000 | ~250 | 显著降低 |
| CPU 占用（高输出场景） | 高 | 低 | 用户可感知 |

进程清理改进：

| 指标 | 改前 | 改后 |
|------|------|------|
| 终止方式 | SIGKILL | SIGTERM → SIGKILL |
| 僵尸进程风险 | 存在（无 wait） | 消除（有 wait） |
| bash history 保存 | 不会 | 会（5 秒内） |

---

## 九、后续展望

v0.9.0 的重构为后续开发打下了坚实的基础。以下几个方向值得在未来版本中探索：

### 9.1 集成测试

当前的 68 个测试都是单元测试，测试的是独立的函数和方法。未来可以添加集成测试：启动一个真实的 relay 进程，用模拟的 client 连接上去，验证完整的数据流通路。Python 的 `multiprocessing` 模块或 `subprocess` 可以用来在测试中启动真实的 WebSocket 服务器。

### 9.2 CI 测试流水线

目前 GitHub Actions 只有一个 publish workflow（打 tag 时发布到 PyPI）。应该添加一个 test workflow，在每次 push 和 PR 时自动运行测试：

```yaml
name: Test
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: pip install -e ".[dev]"
      - run: pytest -v
```

### 9.3 静态类型检查

既然已经添加了完整的类型提示，下一步可以引入 `mypy` 或 `pyright` 进行静态类型检查，在 CI 中作为一道额外的质量关卡。

### 9.4 连接级超时和限流

当前 relay 对单个连接的认证超时是 30 秒，但没有对消息频率做限制。恶意客户端可以发送大量消息淹没 relay。未来可以考虑添加基于 token bucket 的限流机制。

---

## 十、结语

v0.9.0 的重构是一次典型的"先让代码跑起来，再让代码跑得好"的工程实践。它没有引入任何新功能，但让项目的工程基础发生了质变：

- **从零测试到 68 个测试**：每一次协议改动、路由调整都能自动验证
- **从闭包到类**：状态管理清晰、生命周期明确、可测试性大幅提升
- **从逐字节到缓冲**：管道模式性能改善数量级
- **从暴力终止到优雅关闭**：进程管理更健壮、更符合 Unix 哲学
- **从无版本号到 `--version`**：CLI 更完整、更专业

这些改动的核心思想只有一个：**让代码服务于人，而不仅仅是机器。** 机器不在乎代码是闭包还是类、是 `read(1)` 还是 `read(4096)`——但下一个阅读代码的开发者会在乎，下一个排查 Bug 的维护者会在乎，六个月后的你也会在乎。

好的代码不是一次写成的，而是一次又一次打磨出来的。ws-tunnel 的 v0.9.0，就是一次认真的打磨。
