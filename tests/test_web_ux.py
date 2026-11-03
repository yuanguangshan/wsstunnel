"""tests/test_web_ux.py — 移动端体验三件套回归断言（v1.0.18）

1. 字号/行高：滑块范围、实时改 xterm options、改完重新 fit（否则行列错位）。
2. 会话恢复：刷新/切后台后还能看到上次终端内容；只存尾部 500 行；
   关闭开关要删掉已存内容；清除配置时同步删除，避免把旧会话留在本机。
3. 快捷命令：可增删、持久化、点击直接发到 PTY；用户输入必须用
   createElement/textContent 渲染（不能 innerHTML，命令里 <script> 会执行）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = ROOT / "wsstunnel" / "web" / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _code_lines(html: str) -> str:
    out = []
    for line in html.splitlines():
        s = line.strip()
        if s.startswith("//") or s.startswith("/*") or s.startswith("*") or s.startswith("<!--"):
            continue
        out.append(line)
    return "\n".join(out)


class TestFontSize:
    def test_sliders_present(self, html):
        font = re.search(r'<input[^>]*id="bg-font"[^>]*>', html)
        assert font, "缺少字号滑块"
        tag = font.group(0)
        assert 'min="10"' in tag and 'max="24"' in tag and 'type="range"' in tag
        lh = re.search(r'<input[^>]*id="bg-lh"[^>]*>', html)
        assert lh, "缺少行高滑块"
        tag = lh.group(0)
        assert 'min="10"' in tag and 'max="18"' in tag, "行高用 10~18 表示 1.0~1.8"

    def test_applied_to_terminal_and_refit(self, html):
        code = _code_lines(html)
        assert "term.options.fontSize = bgState.fontSize" in code
        assert "term.options.lineHeight = bgState.lineHeight" in code
        assert "_fitTerminal = fitTerminal" in code, "字号变了必须重新 fit，否则终端行列错位"
        assert "if (_fitTerminal) _fitTerminal()" in code

    def test_initial_terminal_uses_prefs(self, html):
        code = _code_lines(html)
        i = code.index("term = new Terminal(")
        seg = code[i:i + 700]
        assert "fontSize: bgState.fontSize" in seg
        assert "lineHeight: bgState.lineHeight" in seg
        assert "fontSize: 14," not in seg, "Terminal 构造里不要再用写死的字号"

    def test_persisted(self, html):
        code = _code_lines(html)
        assert "fontSize: bgState.fontSize" in code
        assert "lineHeight: bgState.lineHeight" in code
        assert "fontSize: 14" in code  # prefs 默认值


class TestSessionRestore:
    def test_toggle_default_on(self, html):
        m = re.search(r'<input[^>]*id="bg-session"[^>]*>', html)
        assert m, "缺少会话恢复开关"
        assert 'type="checkbox"' in m.group(0)
        code = _code_lines(html)
        assert "sessionRestore: true" in code

    def test_saves_tail_500_lines(self, html):
        code = _code_lines(html)
        assert "total - 500" in code, "只保存最近 500 行，避免 localStorage 膨胀"
        assert "translateToString(true)" in code, "只存纯文本"
        assert "wsstunnel_term_session:" in code, "按 host 分区，避免不同 relay 串内容"

    def test_save_triggers(self, html):
        code = _code_lines(html)
        assert "scheduleSessionSave()" in code, "PTY 输出后要调度保存"
        assert "addEventListener('pagehide', saveSessionNow)" in code, (
            "页面被回收前必须同步落盘，2s 防抖来不及"
        )
        assert "visibilityState === 'hidden'" in code

    def test_restore_on_connect(self, html):
        code = _code_lines(html)
        assert "restoreSession();" in code
        i = code.index("term = new Terminal(")
        j = code.index("restoreSession();")
        assert i < j
        assert "上次会话" in code, "恢复时要标明这是上次的内容"

    def test_disabling_deletes_stored(self, html):
        code = _code_lines(html)
        i = code.index("sess.addEventListener('change'")
        seg = code[i:i + 400]
        assert "clearSession()" in seg, "关闭开关要删除已存会话（隐私）"

    def test_clear_config_wipes_session(self, html):
        code = _code_lines(html)
        i = code.index("function clearConfig()")
        assert "clearSession()" in code[i:i + 400]


class TestQuickCommands:
    def test_panel_markup(self, html):
        assert 'id="quick-panel"' in html
        assert 'id="quick-list"' in html
        assert 'id="quick-name"' in html and 'id="quick-cmd"' in html
        assert 'id="quick-add"' in html
        assert 'id="quick-btn"' in html, "菜单里要有快捷命令入口"

    def test_defaults_and_storage(self, html):
        code = _code_lines(html)
        assert "QUICK_DEFAULT" in code
        assert "wsstunnel_quick_cmds" in code
        assert "QUICK_MAX" in code

    def test_default_command_groups(self, html):
        """默认命令覆盖系统状态 + 网络诊断两组（v1.0.19）。"""
        code = _code_lines(html)
        for cmd in (
            "uptime", "df -h", "free -h", "top -bn1 | head -20",
            "ps aux | head -20", "uname -a",
            "ss -tlnp", "ip addr", "api.ip.sb", "ping -c 4 1.1.1.1", "ss -s",
        ):
            assert cmd in code, f"默认快捷命令缺少 {cmd}"

    def test_defaults_version_merge(self, html):
        """默认列表升级后，老用户也要能拿到新命令，且保留自己加的。"""
        code = _code_lines(html)
        assert "QUICK_DEFAULT_VERSION" in code
        assert "QUICK_DEFAULTS_VER_KEY" in code
        assert "QUICK_DEFAULT.concat(custom)" in code, "合并顺序：新默认在前，自定义在后"

    def test_send_uses_pty_channel(self, html):
        code = _code_lines(html)
        i = code.index("function quickSend(")
        seg = code[i:i + 600]
        assert "new TextEncoder().encode(text + '\\r')" in seg, (
            "发送要和键盘输入走同一通道（二进制 + 回车）"
        )
        assert "ws.readyState !== WebSocket.OPEN" in seg, "未连接要拦住"

    def test_rendering_is_xss_safe(self, html):
        code = _code_lines(html)
        assert "function quickRender(" in code
        assert "createElement('button')" in code
        # 命令列表渲染不能用 innerHTML 拼用户输入
        i = code.index("function quickRender(")
        seg = code[i:i + 1400]
        assert "innerHTML =" not in seg and ".innerHTML" not in seg, (
            "快捷命令是用户输入，必须走 textContent"
        )

    def test_panel_exclusive_with_background(self, html):
        code = _code_lines(html)
        assert "quickToggle(false)" in code
        i = code.index("function quickToggle(")
        assert "bgTogglePanel(false)" in code[i:i + 500]
