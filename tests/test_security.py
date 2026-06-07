"""tests/test_security.py — 安全增强层单元测试"""

import json
import os
import tempfile
import time
from datetime import datetime, timedelta

import pytest

from wsstunnel.security import (
    IPAllowList,
    BruteForceGuard,
    DenyList,
    Role,
    TokenInfo,
    TokenManager,
    AuditLogger,
    parse_role,
    role_name,
    require_role,
    PermissionError_,
)


# ──────────────────────────────────────────────
#  Role
# ──────────────────────────────────────────────

class TestRole:
    def test_ordering(self):
        assert Role.READONLY < Role.FILE < Role.ADMIN

    def test_parse_role(self):
        assert parse_role("admin") == Role.ADMIN
        assert parse_role("file") == Role.FILE
        assert parse_role("readonly") == Role.READONLY
        assert parse_role("unknown") == Role.READONLY

    def test_role_name(self):
        assert role_name(Role.ADMIN) == "admin"
        assert role_name(Role.FILE) == "file"
        assert role_name(Role.READONLY) == "readonly"


# ──────────────────────────────────────────────
#  TokenManager
# ──────────────────────────────────────────────

class TestTokenManager:
    def test_single_token(self):
        mgr = TokenManager("abc123")
        info = mgr.validate("abc123")
        assert info is not None
        assert info.id == "default"
        assert info.role == Role.ADMIN

    def test_single_token_wrong(self):
        mgr = TokenManager("abc123")
        assert mgr.validate("wrong") is None

    def test_single_token_disabled(self):
        mgr = TokenManager()
        assert not mgr.enabled
        assert mgr.count == 0

    def test_load_file(self):
        tokens = [
            {"id": "admin1", "token": "tok1", "role": "admin"},
            {"id": "file1", "token": "tok2", "role": "file"},
            {"id": "ro1", "token": "tok3", "role": "readonly"},
        ]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(tokens, f)
            path = f.name
        try:
            mgr = TokenManager()
            mgr.load_file(path)
            assert mgr.count == 3
            assert mgr.validate("tok1").role == Role.ADMIN
            assert mgr.validate("tok2").role == Role.FILE
            assert mgr.validate("tok3").role == Role.READONLY
            assert mgr.validate("nonexistent") is None
        finally:
            os.unlink(path)

    def test_load_file_expired(self):
        tokens = [
            {
                "id": "expired",
                "token": "tok_exp",
                "role": "admin",
                "expires": (datetime.now() - timedelta(days=1)).isoformat(),
            },
            {
                "id": "valid",
                "token": "tok_val",
                "role": "file",
                "expires": (datetime.now() + timedelta(days=1)).isoformat(),
            },
        ]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(tokens, f)
            path = f.name
        try:
            mgr = TokenManager()
            mgr.load_file(path)
            assert mgr.validate("tok_exp") is None  # expired
            assert mgr.validate("tok_val") is not None  # valid
        finally:
            os.unlink(path)

    def test_load_file_skip_empty_token(self):
        tokens = [{"id": "empty", "token": "", "role": "admin"}]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(tokens, f)
            path = f.name
        try:
            mgr = TokenManager()
            mgr.load_file(path)
            assert mgr.count == 0  # skipped empty token
        finally:
            os.unlink(path)

    def test_load_file_not_array(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({"token": "val"}, f)
            path = f.name
        try:
            mgr = TokenManager()
            with pytest.raises(ValueError, match="must be a JSON array"):
                mgr.load_file(path)
        finally:
            os.unlink(path)

    def test_enabled_property(self):
        mgr = TokenManager()
        assert not mgr.enabled
        mgr2 = TokenManager("tok")
        assert mgr2.enabled


# ──────────────────────────────────────────────
#  IPAllowList
# ──────────────────────────────────────────────

class TestIPAllowList:
    def test_disabled_by_default(self):
        al = IPAllowList()
        assert not al.enabled
        assert al.allow("1.2.3.4")

    def test_single_ip(self):
        al = IPAllowList(["1.2.3.4/32"])
        assert al.enabled
        assert al.allow("1.2.3.4")
        assert not al.allow("1.2.3.5")

    def test_cidr(self):
        al = IPAllowList(["10.0.0.0/8"])
        assert al.allow("10.1.2.3")
        assert al.allow("10.255.255.255")
        assert not al.allow("11.0.0.1")

    def test_multiple_networks(self):
        al = IPAllowList(["192.168.1.0/24", "10.0.0.0/8"])
        assert al.allow("192.168.1.100")
        assert al.allow("10.0.0.1")
        assert not al.allow("1.2.3.4")

    def test_invalid_network(self):
        al = IPAllowList(["not-an-ip"])
        assert not al.enabled  # invalid entries are ignored


# ──────────────────────────────────────────────
#  BruteForceGuard
# ──────────────────────────────────────────────

class TestBruteForceGuard:
    def test_no_lock_by_default(self):
        bf = BruteForceGuard(max_attempts=3, lockout_sec=1)
        assert not bf.is_locked("1.2.3.4")

    def test_lock_after_max_attempts(self):
        bf = BruteForceGuard(max_attempts=3, lockout_sec=5)
        bf.record_failure("1.2.3.4")
        bf.record_failure("1.2.3.4")
        assert not bf.is_locked("1.2.3.4")
        bf.record_failure("1.2.3.4")
        assert bf.is_locked("1.2.3.4")

    def test_success_clears_failures(self):
        bf = BruteForceGuard(max_attempts=2, lockout_sec=5)
        bf.record_failure("1.2.3.4")
        bf.record_success("1.2.3.4")
        bf.record_failure("1.2.3.4")
        assert not bf.is_locked("1.2.3.4")  # only 1 failure

    def test_lockout_expires(self):
        bf = BruteForceGuard(max_attempts=1, lockout_sec=0.1)
        bf.record_failure("1.2.3.4")
        assert bf.is_locked("1.2.3.4")
        time.sleep(0.15)
        assert not bf.is_locked("1.2.3.4")


# ──────────────────────────────────────────────
#  DenyList
# ──────────────────────────────────────────────

class TestDenyList:
    def test_disabled(self):
        dl = DenyList()
        assert not dl.enabled
        assert not dl.is_denied("rm -rf /")

    def test_deny_command(self):
        dl = DenyList(["rm", "shutdown"])
        assert dl.is_denied("rm -rf /")
        assert dl.is_denied("shutdown now")
        assert not dl.is_denied("ls -la")

    def test_empty_command(self):
        dl = DenyList(["rm"])
        assert not dl.is_denied("")


# ──────────────────────────────────────────────
#  require_role
# ──────────────────────────────────────────────

class TestRequireRole:
    def test_admin_can_do_admin(self):
        info = TokenInfo(id="t1", token="x", role=Role.ADMIN)
        require_role(info, Role.ADMIN)  # should not raise

    def test_file_cannot_do_admin(self):
        info = TokenInfo(id="t1", token="x", role=Role.FILE)
        with pytest.raises(PermissionError_):
            require_role(info, Role.ADMIN)

    def test_readonly_cannot_do_file(self):
        info = TokenInfo(id="t1", token="x", role=Role.READONLY)
        with pytest.raises(PermissionError_):
            require_role(info, Role.FILE)

    def test_none_is_readonly(self):
        with pytest.raises(PermissionError_):
            require_role(None, Role.FILE)


# ──────────────────────────────────────────────
#  AuditLogger (smoke test — just check it doesn't crash)
# ──────────────────────────────────────────────

class TestAuditLogger:
    def test_all_events(self):
        al = AuditLogger()
        al.connect("c1", "1.2.3.4", Role.ADMIN, "t1")
        al.disconnect("c1", "1.2.3.4", 100.0)
        al.command("c1", "ls -la", Role.ADMIN)
        al.file_upload("c1", "/tmp/x.txt", 1024, Role.FILE)
        al.file_download("c1", "/tmp/x.txt", Role.FILE)
        al.auth_failed("1.2.3.4", "abc123")
        al.permission_denied("c1", "1.2.3.4", "exec", Role.READONLY)
