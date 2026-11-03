"""tests/test_web_background.py — Web 终端背景图配置回归断言

v1.0.15 新增背景图配置：本地文件 / 网页 URL 两种来源，模糊度、亮度可调，
图片缓存到 IndexedDB（URL 图首次拉取后，刷新/重连都不再联网），跨域图床
由 relay 的 ``/api/bg`` 代理下载并落盘缓存。

这些断言把几个"改错就废"的点固化下来：

1. 背景层必须存在，且 z-index 低于终端；xterm 的 ``.xterm-viewport`` 默认
   是黑底，不覆盖透明的话背景图会被整个挡住。
2. xterm 必须 ``allowTransparency: true``，且背景启用时 ``theme.background``
   为透明色 —— 否则 canvas 画满底色，图透不出来。
3. 图片二进制必须进 IndexedDB：localStorage 只有 ~5MB，且只能存字符串，
   存不下照片；同时配置（模糊/亮度/开关）单独存 localStorage。
4. 同一 URL 命中缓存时必须直接返回，不能再次 fetch —— 这就是"下次不用
   重新拉"的契约，改坏了会静默退化成每次都联网。
5. 必须存在 relay 代理兜底路径 ``/api/bg?url=``，否则跨域图床永远无法缓存。
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
    """去掉 HTML/JS 注释行，只留可执行代码，避免注释里的关键词造成误判。"""
    out = []
    for line in html.splitlines():
        s = line.strip()
        if s.startswith("//") or s.startswith("/*") or s.startswith("*") or s.startswith("<!--"):
            continue
        out.append(line)
    return "\n".join(out)


class TestBackgroundMarkup:
    def test_layer_and_panel_exist(self, html):
        assert 'id="bg-layer"' in html, "缺少背景图层 #bg-layer"
        assert 'id="bg-panel"' in html, "缺少背景设置面板 #bg-panel"
        assert 'id="bg-btn"' in html, "顶栏缺少背景设置入口 #bg-btn"

    def test_controls_present(self, html):
        # 本地文件选择
        assert 'id="bg-file"' in html and 'type="file"' in html
        assert 'accept="image/*"' in html, "本地文件选择器必须限制 image/*"
        # 网页 URL 输入 + 应用按钮
        assert 'id="bg-url"' in html and 'id="bg-url-apply"' in html
        # 模糊 / 亮度滑块
        assert 'id="bg-blur"' in html and 'type="range"' in html
        assert 'id="bg-bright"' in html
        assert 'id="bg-enable"' in html
        # 清除与重新拉取
        assert 'id="bg-clear"' in html
        assert 'id="bg-refresh"' in html

    def test_slider_ranges(self, html):
        blur = re.search(r'<input[^>]*id="bg-blur"[^>]*>', html)
        assert blur, "缺少模糊度滑块"
        tag = blur.group(0)
        assert 'min="0"' in tag and 'max="30"' in tag, "模糊度范围应为 0~30px"
        bright = re.search(r'<input[^>]*id="bg-bright"[^>]*>', html)
        assert bright, "缺少亮度滑块"
        tag = bright.group(0)
        assert 'min="20"' in tag and 'max="150"' in tag, "亮度范围应为 20%~150%"

    def test_panel_hidden_by_default(self, html):
        m = re.search(r'<div id="bg-panel"([^>]*)>', html)
        assert m and "hidden" in m.group(1), "设置面板默认应收起"


class TestBackgroundCSS:
    def test_layer_stacked_below_terminal(self, html):
        css = re.search(r"#bg-layer\s*\{(.*?)\}", html, re.S)
        assert css, "缺少 #bg-layer 样式"
        block = css.group(1)
        assert "position: fixed" in block
        assert "z-index: 0" in block, "背景层必须在终端之下"
        # 负 inset 兜住模糊后的边缘，避免四周露底色
        assert "inset: -" in block
        assert "pointer-events: none" in block, "背景层不能拦截点击"

    def test_terminal_above_layer(self, html):
        css = re.search(r"#terminal\s*\{(.*?)\}", html, re.S)
        assert css
        assert "z-index: 1" in css.group(1), "终端要压在背景层之上"

    def test_xterm_viewport_transparent(self, html):
        """xterm.css 给 .xterm-viewport 画了黑底，不覆盖就看不见背景图。"""
        assert re.search(
            r"#terminal \.xterm-viewport\s*\{[^}]*background-color:\s*transparent",
            html, re.S,
        ), ".xterm-viewport 必须透明，否则背景图被黑底挡住"


class TestBackgroundRuntime:
    def test_terminal_allow_transparency(self, html):
        code = _code_lines(html)
        assert "allowTransparency: true" in code, (
            "xterm 必须开 allowTransparency，否则 canvas 底色会把背景图盖掉"
        )

    def test_transparent_terminal_theme_when_bg_on(self, html):
        code = _code_lines(html)
        assert "'rgba(0,0,0,0)'" in code, "背景启用时终端 theme.background 要切成透明"
        assert re.search(r"background:\s*transparent\s*\?\s*'rgba\(0,0,0,0\)'", code), (
            "主题应随背景开关切换"
        )

    def test_blur_and_brightness_filter(self, html):
        code = _code_lines(html)
        assert re.search(r"blur\(\$\{bgState\.blur\}px\)\s*brightness\(\$\{bgState\.bright\}%\)", code), (
            "模糊/亮度必须通过 CSS filter 实时应用"
        )

    def test_image_cached_in_indexeddb(self, html):
        code = _code_lines(html)
        assert "indexedDB.open" in code, "图片必须缓存到 IndexedDB"
        assert "createObjectStore" in code
        assert "createObjectURL" in code, "Blob 需要通过 objectURL 应用"
        # 换图时 revoke 旧的 objectURL，避免内存泄漏
        assert "revokeObjectURL" in code

    def test_prefs_in_localstorage(self, html):
        code = _code_lines(html)
        assert "wsstunnel_bg_prefs" in code, "模糊/亮度/开关应持久化"
        assert "localStorage.setItem" in code

    def test_url_cache_hit_skips_network(self, html):
        """同一 URL 已缓存 → 直接本地读取，不再联网（'下次不用重新拉'）。"""
        code = _code_lines(html)
        assert "old.kind === 'url' && old.url === url" in code, (
            "缺少 URL 缓存命中判断：会退化成每次刷新都重新下载"
        )
        # 命中分支出现在任何 fetch 之前
        hit = code.index("old.kind === 'url' && old.url === url")
        first_fetch = code.index("await bgFetchDirect(url)")
        assert hit < first_fetch, "缓存命中必须先于网络请求"

    def test_relay_proxy_fallback(self, html):
        code = _code_lines(html)
        assert "/api/bg?url=" in code, "跨域图床必须回退到 relay 代理下载并缓存"
        assert "encodeURIComponent(url)" in code, "URL 必须编码后传给代理"

    def test_refresh_forces_redownload(self, html):
        code = _code_lines(html)
        assert "refresh=1" in code, "重新拉取必须带 refresh=1 绕过服务端缓存"

    def test_local_file_and_url_kinds(self, html):
        code = _code_lines(html)
        assert "kind: 'file'" in code and "kind: 'url'" in code, "两种来源要能在缓存里区分"
