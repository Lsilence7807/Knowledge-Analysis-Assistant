# 文件：tests/test_p5_style.py
# 作用：P5.3 视觉风格契约的可运行自检：旋钮值、插画自包含性、组件默认描边、页面引用的插画真的存在
# 阶段：P5.3 视觉风格 v2
# 依赖：标准库（re、xml.etree）
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"
ILLUSTRATIONS = ROOT / "frontend" / "public" / "illustrations"

# P5.1 / P5.3 旋钮的契约值（设计文档 §4.16）：改风格要连这里一起改，免得两边悄悄跑偏
KNOBS = (
    "--paper: #FFF7E8",
    "--ink: #17130E",
    "--violet: #6B4BF6",
    "--yellow: #FFD34D",
    "--coral: #FF6A54",
    "--mint: #2F9E5F",
    "--stroke: 3px",
    "--shadow: 6px 6px 0 var(--ink)",
    "--shadow-sm: 3px 3px 0 var(--ink)",
    "--font-stack:",
)


def test_css_knobs_are_still_the_single_source():
    css = (SRC / "index.css").read_text(encoding="utf-8")
    for knob in KNOBS:
        assert knob in css, knob
    # 不许出现第二套配色：每个旋钮只定义一次
    assert css.count("--violet:") == 1
    assert css.count("--font-stack:") == 1
    # shadcn 语义 token 仍指回旋钮（组件拿不到第二套颜色）
    assert "--background: var(--paper)" in css
    assert "--primary: var(--violet)" in css
    assert "--shadow-hard-sm: var(--shadow-sm)" in css
    assert "--font-sans: var(--font-stack)" in css


def test_illustrations_are_self_contained_svg():
    files = sorted(ILLUSTRATIONS.glob("*.svg"))
    assert len(files) >= 3, files
    for path in files:
        text = path.read_text(encoding="utf-8")
        root = ET.fromstring(text)  # 坏掉的 XML 直接抛
        assert root.tag.endswith("svg"), path.name
        assert root.get("viewBox"), path.name
        assert "<script" not in text, path.name
        assert "<text" not in text, path.name  # 不含文字，不依赖字体
        assert text.count("http") == 1, path.name  # 只允许 xmlns 那一个 http
        assert 'xmlns="http://www.w3.org/2000/svg"' in text, path.name
        assert "#17130E" in text, path.name  # 描边取旋钮的 ink


def test_component_defaults_carry_stroke_and_hard_shadow():
    card = (SRC / "components" / "ui" / "card.tsx").read_text(encoding="utf-8")
    assert "border-[3px] border-ink shadow-hard" in card
    button = (SRC / "components" / "ui" / "button.tsx").read_text(encoding="utf-8")
    assert "border-[3px] border-transparent" in button
    assert button.count("shadow-hard-sm") >= 4
    for name in ("input.tsx", "textarea.tsx", "select.tsx"):
        source = (SRC / "components" / "ui" / name).read_text(encoding="utf-8")
        assert "border-[3px] border-ink" in source, name


def test_pages_use_the_decor_layer_and_assets_exist():
    used = set()
    for name in ("home.tsx", "login.tsx"):
        source = (SRC / "routes" / name).read_text(encoding="utf-8")
        assert "@/components/decor" in source, name
        used.update(re.findall(r'<Illustration\s+name="([a-z0-9-]+)"', source))
    assert len(used) >= 3, used
    for asset in sorted(used):
        # 插画名写错在页面上只是裂图、不报错，所以这里钉住每个引用都真的存在
        assert (ILLUSTRATIONS / (asset + ".svg")).is_file(), asset


def test_orientation_variants_spell_out_the_radix_attribute():
    # K-057：Radix 给的是 data-orientation="horizontal"，Tailwind 的 `data-horizontal:` 只匹配「属性存在」，
    # 于是页签条一类没匹配上、被撑成 332px 竖排。变体一律写全，禁止简写。
    offenders = []
    for path in sorted(SRC.rglob("*.ts*")):
        text = path.read_text(encoding="utf-8")
        for bad in ("data-horizontal:", "data-vertical:"):
            if bad in text:
                offenders.append(path.relative_to(SRC).as_posix() + ": " + bad)
    assert not offenders, offenders
    tabs = (SRC / "components" / "ui" / "tabs.tsx").read_text(encoding="utf-8")
    assert "group-data-[orientation=horizontal]/tabs:h-8" in tabs
