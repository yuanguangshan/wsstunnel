"""tests/test_web_menu.py — 顶栏 ☰ 菜单回归断言

v1.0.15 把「📁 上传」「📊 集群」「🖼 背景设置」和页面底部固定的「清除」
统一收进顶栏 ☰ 菜单：这些都是低频操作，常驻按钮会挤占手机上的终端显示区。

固化的约束：
1. 顶栏（#top-bar）里只允许状态、版本号、☰ 菜单按钮；上传/集群/背景/
   清除都必须在 #menu-panel 内。
2. 底部那个 fixed 的 #clear-btn 必须不存在（否则会和菜单里的清除重复）。
3. 每个菜单项的点击处理都要先 menuClose()，否则点完菜单还挂在屏幕上，
   挡住终端。
4. 菜单要支持「点外部收起」和 Esc 收起。
5. 背景面板（z-index 35）必须低于菜单（45），否则从菜单打开背景面板时
   会被遮挡。
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


def _top_bar(html: str) -> str:
    m = re.search(r'<div id="top-bar">(.*?)</div>', html, re.S)
    assert m, "缺少 #top-bar"
    return m.group(1)


def _menu_panel(html: str) -> str:
    m = re.search(r'<div id="menu-panel"[^>]*>(.*?)</div>\s*<!--', html, re.S)
    if not m:
        m = re.search(r'<div id="menu-panel"[^>]*>(.*?)</div>', html, re.S)
    assert m, "缺少 #menu-panel"
    return m.group(1)


class TestTopBar:
    def test_only_status_cluster_and_menu(self, html):
        """顶栏只留高频入口：连接状态、集群、☰ 菜单。"""
        bar = _top_bar(html)
        assert 'id="status"' in bar
        assert 'id="cluster-btn"' in bar, "集群常用，应常驻顶栏"
        assert 'id="menu-btn"' in bar
        # 低频入口与版本信息不能再常驻顶栏
        for gone in ("file-btn", "bg-btn", "clear-btn", "version"):
            assert f'id="{gone}"' not in bar, f"#{gone} 不应留在顶栏"

    def test_menu_button_accessible(self, html):
        m = re.search(r'<button id="menu-btn"([^>]*)>', html)
        assert m, "缺少菜单按钮"
        attrs = m.group(1)
        assert 'aria-haspopup="true"' in attrs
        assert 'aria-expanded="false"' in attrs


class TestMenuPanel:
    def test_all_low_frequency_entries_in_menu(self, html):
        panel = _menu_panel(html)
        for item in ("file-btn", "bg-btn", "clear-btn"):
            assert f'id="{item}"' in panel, f"#{item} 必须在 #menu-panel 内"
        assert 'id="cluster-btn"' not in panel, "集群是高频入口，应留在顶栏"

    def test_version_in_menu(self, html):
        """版本号是低频信息，收进菜单底部即可（具体版本号由 test_web_ui 校验）。"""
        panel = _menu_panel(html)
        assert 'id="version"' in panel, "版本号应显示在菜单里"
        assert re.search(r'<span id="version"[^>]*>v\d+\.\d+\.\d+</span>', panel)

    def test_menu_hidden_by_default(self, html):
        m = re.search(r'<div id="menu-panel"([^>]*)>', html)
        assert m and "hidden" in m.group(1), "菜单默认应收起"

    def test_no_fixed_clear_button_at_bottom(self, html):
        assert not re.search(r"#clear-btn\s*\{[^}]*position:\s*fixed", html, re.S), (
            "底部的 fixed 清除按钮应已删除，清除收进菜单"
        )
        assert '<button id="clear-btn">清除</button>' not in html

    def test_danger_style_for_clear(self, html):
        m = re.search(r'<button id="clear-btn"([^>]*)>', html)
        assert m and "danger" in m.group(1), "清除配置属于破坏性操作，应有 danger 样式"

    def test_menu_stacks_above_panels(self, html):
        menu_css = re.search(r"#menu-panel\s*\{(.*?)\}", html, re.S).group(1)
        bg_css = re.search(r"#bg-panel\s*\{(.*?)\}", html, re.S).group(1)
        z_menu = int(re.search(r"z-index:\s*(\d+)", menu_css).group(1))
        z_bg = int(re.search(r"z-index:\s*(\d+)", bg_css).group(1))
        assert z_menu > z_bg, "菜单层级要高于背景抽屉，否则会被盖住"


class TestMenuBehavior:
    def test_menu_open_close_api(self, html):
        code = _code_lines(html)
        assert "function menuOpen(" in code
        assert "function menuClose(" in code
        assert "menuOpen()" in code and "menuClose()" in code

    def test_outside_click_and_escape_close(self, html):
        code = _code_lines(html)
        assert "panel.contains(e.target)" in code, "缺少点外部收起"
        assert "e.key === 'Escape'" in code, "缺少 Esc 收起"

    def test_entries_close_menu_before_action(self, html):
        code = _code_lines(html)
        # 菜单内的常规入口都应在动作前调用 menuClose()；
        # 清除配置例外 —— 它先展开二次确认，确认后才收菜单
        assert code.count("menuClose();") >= 3, "上传/背景/确认清除应各自先收起菜单"
        for anchor in (
            "document.getElementById('file-btn').addEventListener",
            "document.getElementById('clear-ok')",
        ):
            i = code.index(anchor)
            seg = code[i:i + 400]
            assert "menuClose();" in seg, f"{anchor} 的处理函数应先 menuClose()"

    def test_bg_entry_closes_menu(self, html):
        code = _code_lines(html)
        i = code.index("bgTogglePanel(true)")
        assert "menuClose();" in code[max(0, i - 200):i], "背景菜单项应先收起菜单"
        assert "bgTogglePanel(bgEl('bg-panel').classList.contains('hidden'))" not in code, (
            "菜单项语义是打开面板，不能 toggle（面板已开时会被关掉）"
        )

    def test_upload_entry_still_opens_file_input(self, html):
        code = _code_lines(html)
        i = code.index("document.getElementById('file-btn').addEventListener")
        assert "file-input').click()" in code[i:i + 300], "上传入口仍要触发文件选择"


class TestClearConfirm:
    """清除连接配置是破坏性操作，必须二次确认（v1.0.17）。"""

    def test_confirm_block_exists_and_hidden(self, html):
        m = re.search(r'<div id="clear-confirm"([^>]*)>', html)
        assert m, "缺少清除确认块 #clear-confirm"
        assert "hidden" in m.group(1), "确认块默认应收起"
        assert 'id="clear-cancel"' in html, "缺少取消按钮"
        assert 'id="clear-ok"' in html, "缺少确认按钮"

    @staticmethod
    def _handler_body(code: str, anchor: str) -> str:
        """取某个 addEventListener 处理函数的函数体（到第一个 ``});``）。"""
        i = code.index(anchor)
        j = code.index("});", i)
        return code[i:j]

    def test_clear_btn_does_not_clear_directly(self, html):
        code = _code_lines(html)
        body = self._handler_body(code, "document.getElementById('clear-btn').addEventListener")
        assert "clearConfig()" not in body, "点「清除连接配置」不能直接执行 clearConfig"
        assert "clearConfirmShow(true)" in body, "应先展开二次确认"
        assert "menuClose();" not in body, "展开确认时不要收起菜单，否则确认框看不到"

    def test_confirm_executes_after_ok(self, html):
        code = _code_lines(html)
        body = self._handler_body(code, "document.getElementById('clear-ok')")
        assert "clearConfig()" in body, "确认后才执行清除"
        assert "menuClose();" in body

    def test_cancel_only_closes_confirm(self, html):
        code = _code_lines(html)
        body = self._handler_body(code, "document.getElementById('clear-cancel')")
        assert "clearConfirmShow(false)" in body
        assert "clearConfig()" not in body, "取消不能触发清除"

    def test_menu_close_resets_confirm(self, html):
        code = _code_lines(html)
        i = code.index("function menuClose()")
        seg = code[i:i + 200]
        assert "clearConfirmShow(false)" in seg, "收起菜单时要重置确认态，避免下次误点"

    def test_confirm_explains_consequence(self, html):
        m = re.search(r'<div id="clear-confirm"[^>]*>(.*?)</div>\s*<div class="menu-sep">', html, re.S)
        assert m and ("断开" in m.group(1) or "重新输入" in m.group(1)), (
            "确认文案要说明后果（断线 / 需重新输入地址和 token）"
        )

    def test_no_reconnect_after_clear(self, html):
        """清除后不应再闪「重连中」：主动清除要直接回表单。"""
        code = _code_lines(html)
        i = code.index("function clearConfig()")
        assert "_userCleared = true" in code[i:i + 400]
        j = code.index("ws.onclose")
        seg = code[j:j + 1200]
        assert "if (_userCleared)" in seg, "onclose 要识别主动清除"
        assert "connect-form" in seg
        assert "_reconnectAttempt++" in seg, "正常的自动重连逻辑要保留"
