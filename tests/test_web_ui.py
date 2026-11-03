"""tests/test_web_ui.py — Web 终端页面结构与回归断言

v1.0.8 ~ v1.0.12 期间"翻页键在真机点击无反应"被反复修复了 4 次，
每次都是靠肉眼在桌面浏览器里模拟，缺少可回归的断言。这里把当时的根因
固化成测试，避免下次改动把坑再踩回来：

1. ⌨ / ▲ / ▼ 必须在同一个 #fab-group 容器里（旧版三者各自 fixed 定位，
   键盘弹出后 ⌨ 与 ▲ 重叠，点击被 ⌨ 吃掉）。
2. 不许再把 ⌨ 用 visualViewport 顶到"可视区垂直中央"。
3. 翻页键必须是 <button type="button">（X5 上 fixed 的 <div>+click 不可靠）。
4. xterm CDN 必须锁版本：未锁定时一旦拿到 4.x，term.buffer.active 会抛
   TypeError，事件处理器静默失败 —— 又变成"点了没反应"。
5. 页面版本号必须与 pyproject 一致，方便真机确认加载的是新页面。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = ROOT / "wsstunnel" / "web" / "index.html"
PYPROJECT = ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _fab_group(html: str) -> str:
    m = re.search(r'<div id="fab-group"[^>]*>(.*?)</div>', html, re.S)
    assert m, "缺少 #fab-group 容器"
    return m.group(1)


def _code_lines(html: str) -> list[str]:
    """去掉 HTML/JS 注释行，只留可执行代码，避免注释里的关键词造成误判。"""
    out = []
    for line in html.splitlines():
        s = line.strip()
        if s.startswith("//") or s.startswith("/*") or s.startswith("*") or s.startswith("<!--"):
            continue
        out.append(line)
    return out


class TestFloatingButtons:
    def test_group_contains_all_three_buttons(self, html):
        group = _fab_group(html)
        for btn_id in ("page-up", "fab", "page-down"):
            assert f'id="{btn_id}"' in group, f"#{btn_id} 必须在 #fab-group 内"
        # 顺序：▲ 在上、⌨ 居中、▼ 在下
        assert group.index('id="page-up"') < group.index('id="fab"') < group.index('id="page-down"')

    def test_page_buttons_are_buttons(self, html):
        for btn_id in ("page-up", "page-down"):
            m = re.search(rf'<(button|div)[^>]*id="{btn_id}"', html)
            assert m and m.group(1) == "button", f"#{btn_id} 必须是 <button>（X5 上 div 不可靠）"
        # 显式 type=button，避免被当成 submit
        assert 'type="button" id="page-up"' in html
        assert 'type="button" id="page-down"' in html

    def test_fab_no_longer_centered_by_visual_viewport(self, html):
        """旧实现 fab.style.top = midY - 24 —— 键盘一弹就压住 ▲。"""
        code = "\n".join(_code_lines(html))
        assert "fab.style.top" not in code
        assert "midY" not in code


class TestScrollingLogic:
    def test_buffer_api_compat_layer(self, html):
        """xterm 4.x 没有 term.buffer.active，必须走兼容层。"""
        assert "function activeBuffer()" in html
        assert "function bufferType()" in html
        # 不允许再直接访问（兼容层内部除外）
        direct = [
            line for line in _code_lines(html)
            if "term.buffer.active" in line and "term.buffer.active || term.buffer" not in line
        ]
        assert direct == [], f"存在不兼容 xterm 4.x 的写法: {direct}"

    def test_per_key_throttle_instead_of_global(self, html):
        """旧版全局 300ms 节流会吞掉快速连点。"""
        assert "_pageFireAt" not in html
        assert "_fireAt" in html

    def test_document_level_touch_fallback(self, html):
        assert "document.addEventListener('touchstart'" in html
        # 按最近距离判定，避免两键热区互相误判
        assert "bestD" in html

    def test_paging_moved_to_panel_as_fallback(self, html):
        assert 'data-key="page-up"' in html
        assert 'data-key="page-down"' in html

    def test_edge_hint_present(self, html):
        assert 'id="page-hint"' in html
        assert "function pageHint(" in html


class TestAssetPinning:
    def test_xterm_version_pinned(self, html):
        srcs = re.findall(r'https://cdn\.jsdelivr\.net/npm/xterm[^"\']*', html)
        assert srcs, "未找到 xterm CDN 引用"
        for src in srcs:
            assert re.search(r"xterm@\d+\.\d+\.\d+", src), f"xterm 必须锁定版本: {src}"
            assert "xterm@5." in src, f"页面依赖 5.x 的 buffer API: {src}"


def test_page_version_matches_project_version(html):
    m = re.search(r'<span id="version"[^>]*>v(\d+\.\d+\.\d+)</span>', html)
    assert m, "页面缺少 #version 版本号"
    page_version = m.group(1)

    pyproject = PYPROJECT.read_text(encoding="utf-8")
    m2 = re.search(r'^version\s*=\s*"(\d+\.\d+\.\d+)"', pyproject, re.M)
    assert m2, "pyproject 缺少 version"
    assert page_version == m2.group(1), (
        f"页面版本 v{page_version} 与 pyproject {m2.group(1)} 不一致 —— "
        "真机上是上不了新页面的，务必同步 bump"
    )
