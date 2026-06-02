"""tests/test_protocol.py — 协议解析函数的单元测试"""

import pytest

from wsstunnel.relay import _parse_backend_auth, _is_frontend_auth


# ──────────────────────────────────────────────
#  _parse_backend_auth 测试
# ──────────────────────────────────────────────

class TestParseBackendAuth:
    """后端认证消息解析测试。"""

    # ── 带 token 的完整格式 ──

    def test_full_format_pty(self):
        result = _parse_backend_auth("IAM_BACKEND:secret:mybox:pty", "secret")
        assert result == ("mybox", "pty")

    def test_full_format_pipe(self):
        result = _parse_backend_auth("IAM_BACKEND:secret:mybox:pipe", "secret")
        assert result == ("mybox", "pipe")

    def test_token_and_name_no_mode(self):
        """旧客户端：有 token 和 name 但没有 mode 标记。"""
        result = _parse_backend_auth("IAM_BACKEND:secret:mybox", "secret")
        assert result == ("mybox", "pipe")

    def test_token_only(self):
        """仅有 token，自动命名。"""
        result = _parse_backend_auth("IAM_BACKEND:secret", "secret")
        assert result == (None, "pipe")

    # ── 无 token 的格式 ──

    def test_no_token_name_and_mode(self):
        result = _parse_backend_auth("IAM_BACKEND:mybox:pty", None)
        assert result == ("mybox", "pty")

    def test_no_token_mode_only(self):
        """仅有 mode 标记，自动命名。"""
        result = _parse_backend_auth("IAM_BACKEND:pty", None)
        assert result == (None, "pty")

    def test_no_token_bare(self):
        """最旧兼容格式。"""
        result = _parse_backend_auth("IAM_BACKEND", None)
        assert result == (None, "pipe")

    # ── 边界情况 ──

    def test_binary_message_rejected(self):
        result = _parse_backend_auth(b"IAM_BACKEND:secret:mybox:pty", "secret")
        assert result is None

    def test_wrong_prefix(self):
        result = _parse_backend_auth("AUTH:secret", "secret")
        assert result is None

    def test_wrong_token(self):
        result = _parse_backend_auth("IAM_BACKEND:wrong:mybox:pty", "secret")
        assert result is None

    def test_whitespace_stripped(self):
        result = _parse_backend_auth("  IAM_BACKEND:secret:mybox:pty  ", "secret")
        assert result == ("mybox", "pty")

    def test_empty_string(self):
        result = _parse_backend_auth("", "secret")
        assert result is None

    def test_partial_prefix(self):
        """消息仅包含 'IAM_BACKEND' 但设了 token → 不应匹配。"""
        result = _parse_backend_auth("IAM_BACKEND", "secret")
        assert result is None


# ──────────────────────────────────────────────
#  _is_frontend_auth 测试
# ──────────────────────────────────────────────

class TestIsFrontendAuth:
    """前端认证消息验证测试。"""

    def test_correct_token(self):
        assert _is_frontend_auth("AUTH:secret", "secret") is True

    def test_wrong_token(self):
        assert _is_frontend_auth("AUTH:wrong", "secret") is False

    def test_no_token_any_message(self):
        """无 token 时任何文本消息都被接受为前端。"""
        assert _is_frontend_auth("hello", None) is True

    def test_no_token_binary_rejected(self):
        """无 token 时二进制消息仍被拒绝。"""
        assert _is_frontend_auth(b"hello", None) is False

    def test_binary_message_rejected(self):
        assert _is_frontend_auth(b"AUTH:secret", "secret") is False

    def test_whitespace(self):
        assert _is_frontend_auth("  AUTH:secret  ", "secret") is True

    def test_partial_match(self):
        """不以 'AUTH:' 开头的不匹配。"""
        assert _is_frontend_auth("AUTHsecret", "secret") is False
