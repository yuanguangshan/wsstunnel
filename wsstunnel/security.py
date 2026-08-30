"""
wsstunnel/security.py — 轻量安全增强层

无外部依赖，仅使用 Python 标准库。
设计目标：不改变现有协议，不增加复杂度。
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, auto
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
#  角色权限
# ──────────────────────────────────────────────

class Role(IntEnum):
    READONLY = auto()  # 只看输出，不能执行命令
    FILE = auto()      # 只能文件传输
    ADMIN = auto()     # 完全控制


_ROLE_NAMES: dict[str, Role] = {
    "readonly": Role.READONLY,
    "file": Role.FILE,
    "admin": Role.ADMIN,
}

_ROLE_NAMES_REV: dict[Role, str] = {v: k for k, v in _ROLE_NAMES.items()}


def parse_role(name: str) -> Role:
    """将角色名转为 Role 枚举，未知名称返回 READONLY。"""
    return _ROLE_NAMES.get(name.lower(), Role.READONLY)


def role_name(role: Role) -> str:
    return _ROLE_NAMES_REV.get(role, "readonly")


# ──────────────────────────────────────────────
#  Token 模型
# ──────────────────────────────────────────────

@dataclass
class TokenInfo:
    id: str
    token: str
    role: Role = Role.ADMIN
    expires: datetime | None = None


# ──────────────────────────────────────────────
#  TokenManager
# ──────────────────────────────────────────────

class TokenManager:
    """Token 管理：加载、验证、过期检查。"""

    def __init__(self, single_token: str | None = None) -> None:
        self._tokens: dict[str, TokenInfo] = {}
        if single_token:
            self._tokens[single_token] = TokenInfo(
                id="default",
                token=single_token,
                role=Role.ADMIN,
            )
            logger.info(f"Security: single token mode (id=default, role=admin)")

    def load_file(self, path: str) -> None:
        """从 JSON 文件加载多 token。"""
        with open(path, "r") as f:
            raw = json.load(f)
        if not isinstance(raw, list):
            raise ValueError("token-file must be a JSON array")
        for item in raw:
            tid = item.get("id", "unknown")
            token = item.get("token", "")
            if not token:
                logger.warning(f"Security: skip token '{tid}' — empty token string")
                continue
            role = parse_role(item.get("role", "readonly"))
            expires: datetime | None = None
            if "expires" in item:
                try:
                    expires = datetime.fromisoformat(item["expires"])
                except (ValueError, TypeError):
                    logger.warning(
                        f"Security: invalid expires for token '{tid}', ignoring"
                    )
            self._tokens[token] = TokenInfo(
                id=tid,
                token=token,
                role=role,
                expires=expires,
            )
        logger.info(
            f"Security: loaded {len(self._tokens)} token(s) from {path}"
        )

    def validate(self, token: str) -> TokenInfo | None:
        """验证 token，返回 TokenInfo 或 None。

        使用 ``hmac.compare_digest`` 做常数时间比较，避免时序侧信道；
        过期时间同时兼容 naive 和 timezone-aware 的 ISO 格式。
        """
        matched: TokenInfo | None = None
        token_bytes = token.encode("utf-8", "replace")
        for known, info in self._tokens.items():
            if hmac.compare_digest(known.encode("utf-8", "replace"), token_bytes):
                matched = info
                break
        if matched is None:
            return None
        expires = matched.expires
        if expires:
            # datetime.now(tz) 在 tz=None 时返回 naive 本地时间，
            # 与 naive/aware 的 expires 都能安全比较（此前 aware 会 TypeError）
            if datetime.now(expires.tzinfo) > expires:
                logger.info(
                    f"Security: token '{matched.id}' expired (since {expires})"
                )
                return None
        return matched

    @property
    def enabled(self) -> bool:
        return len(self._tokens) > 0

    @property
    def count(self) -> int:
        return len(self._tokens)


# ──────────────────────────────────────────────
#  IP 白名单
# ──────────────────────────────────────────────

class IPAllowList:
    """IP 白名单，支持 CIDR 和单个 IP。"""

    def __init__(self, networks: list[str] | None = None) -> None:
        self._networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        if networks:
            for n in networks:
                try:
                    self._networks.append(ipaddress.ip_network(n, strict=False))
                except ValueError as e:
                    logger.warning(f"Security: invalid network '{n}': {e}")

    @property
    def enabled(self) -> bool:
        return len(self._networks) > 0

    def allow(self, ip_str: str) -> bool:
        """检查 IP 是否在白名单中。"""
        if not self.enabled:
            return True
        try:
            ip = ipaddress.ip_address(ip_str)
            return any(ip in net for net in self._networks)
        except ValueError:
            return False


# ──────────────────────────────────────────────
#  防爆破
# ──────────────────────────────────────────────

class BruteForceGuard:
    """简单防爆破：连续失败后延迟。

    失败记录带时间戳并惰性过期清理，避免公网扫描器长期灌入
    导致 ``_failures`` / ``_locked_until`` 无界增长。
    """

    _CLEANUP_INTERVAL = 60.0   # 清理扫描最小间隔秒数
    _FAILURE_TTL = 600.0       # 失败记录无活动过期秒数

    def __init__(
        self,
        max_attempts: int = 5,
        lockout_sec: float = 3.0,
    ) -> None:
        self._max = max_attempts
        self._lockout = lockout_sec
        self._failures: dict[str, tuple[int, float]] = {}  # ip -> (次数, 最后失败时间)
        self._locked_until: dict[str, float] = {}
        self._last_cleanup: float = time.time()

    def _cleanup_if_due(self, now: float) -> None:
        """惰性清理过期的失败记录与已过期的封禁条目。"""
        if now - self._last_cleanup < self._CLEANUP_INTERVAL:
            return
        self._last_cleanup = now
        stale = [
            ip for ip, (_, ts) in self._failures.items()
            if now - ts > self._FAILURE_TTL
        ]
        for ip in stale:
            self._failures.pop(ip, None)
        expired = [ip for ip, until in self._locked_until.items() if until < now]
        for ip in expired:
            self._locked_until.pop(ip, None)

    def record_failure(self, ip: str) -> None:
        """记录一次失败。"""
        now = time.time()
        self._cleanup_if_due(now)
        count, _ = self._failures.get(ip, (0, 0.0))
        count += 1
        self._failures[ip] = (count, now)
        if count >= self._max:
            self._locked_until[ip] = now + self._lockout
            logger.info(
                f"Security: IP {ip} locked out for {self._lockout}s "
                f"(after {self._max} failures)"
            )

    def record_success(self, ip: str) -> None:
        """成功后清除失败计数。"""
        self._failures.pop(ip, None)
        self._locked_until.pop(ip, None)

    def is_locked(self, ip: str) -> bool:
        """检查 IP 是否被暂时封禁。"""
        until = self._locked_until.get(ip, 0)
        if until > time.time():
            return True
        self._locked_until.pop(ip, None)
        return False

    @property
    def enabled(self) -> bool:
        return self._max > 0


# ──────────────────────────────────────────────
#  审计日志
# ──────────────────────────────────────────────

class AuditLogger:
    """审计日志：JSON 格式输出到 logger。"""

    @staticmethod
    def _log(event: str, **kwargs: Any) -> None:
        record = {"event": event, **kwargs}
        logger.info("AUDIT " + json.dumps(record, default=str))

    def connect(
        self, client_id: str, ip: str, role: Role, token_id: str
    ) -> None:
        self._log(
            "connect",
            client_id=client_id,
            ip=ip,
            role=role_name(role),
            token_id=token_id,
        )

    def disconnect(self, client_id: str, ip: str, duration_sec: float) -> None:
        self._log(
            "disconnect",
            client_id=client_id,
            ip=ip,
            duration_sec=round(duration_sec, 1),
        )

    def command(self, client_id: str, cmd: str, role: Role) -> None:
        self._log(
            "exec",
            client_id=client_id,
            cmd=cmd[:100],
            role=role_name(role),
        )

    def file_upload(
        self, client_id: str, path: str, size: int, role: Role
    ) -> None:
        self._log(
            "upload",
            client_id=client_id,
            path=path,
            size=size,
            role=role_name(role),
        )

    def file_download(
        self, client_id: str, path: str, role: Role
    ) -> None:
        self._log(
            "download",
            client_id=client_id,
            path=path,
            role=role_name(role),
        )

    def auth_failed(self, ip: str, token_prefix: str) -> None:
        self._log(
            "auth_failed",
            ip=ip,
            token_prefix=token_prefix,
        )

    def permission_denied(
        self, client_id: str, ip: str, operation: str, role: Role
    ) -> None:
        self._log(
            "permission_denied",
            client_id=client_id,
            ip=ip,
            operation=operation,
            role=role_name(role),
        )


# ──────────────────────────────────────────────
#  命令黑名单
# ──────────────────────────────────────────────

class DenyList:
    """命令黑名单：禁止执行的命令前缀。"""

    def __init__(self, commands: list[str] | None = None) -> None:
        self._commands: set[str] = set(commands or [])

    @property
    def enabled(self) -> bool:
        return len(self._commands) > 0

    def is_denied(self, cmd: str) -> bool:
        """检查命令是否在黑名单中。"""
        if not self.enabled:
            return False
        first_word = cmd.strip().split()[0] if cmd.strip() else ""
        return first_word in self._commands


# ──────────────────────────────────────────────
#  简易权限检查：装饰器式
# ──────────────────────────────────────────────

class PermissionError_(Exception):
    """权限不足异常。"""

    def __init__(self, operation: str, required: Role, actual: Role) -> None:
        self.operation = operation
        self.required = required
        self.actual = actual
        super().__init__(
            f"permission denied: {operation} requires {role_name(required)}, "
            f"client has {role_name(actual)}"
        )


def require_role(token_info: TokenInfo | None, required: Role) -> None:
    """检查 token 是否有足够权限。"""
    if token_info is None:
        role = Role.READONLY
    else:
        role = token_info.role
    if role < required:
        raise PermissionError_("command", required, role)
