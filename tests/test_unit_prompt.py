"""测试: Step 3 结构化 prompt + 逐 unit 字数。

提供可单独测试的纯函数:
- compute_target_char_range(dur_sec) → (lo, hi)
- build_unit_translation_lines(batch) → ['[N] (X-Y字) <英文>', ...]
- strip_char_count_prefix(text) → 译文 (剥离 LLM 输出中的字数自报)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── compute_target_char_range ──

def test_target_range_5s_normal():
    """5 秒英语用 use_jieba=False 走 CPS 区间 [3.5*5, 5.5*5] = [18, 28]"""
    from text_utils import compute_target_char_range
    lo, hi = compute_target_char_range(5.0, use_jieba=False)
    assert 15 <= lo <= 19
    assert 26 <= hi <= 30
    assert lo < hi


def test_target_range_short_segment():
    """超短段 (<1s) 至少给最低区间"""
    from text_utils import compute_target_char_range
    lo, hi = compute_target_char_range(0.5)
    assert lo >= 1
    assert hi > lo


def test_target_range_long_segment():
    """12s use_jieba=False → CPS 区间 [42, 66]"""
    from text_utils import compute_target_char_range
    lo, hi = compute_target_char_range(12.0, use_jieba=False)
    assert 38 <= lo <= 45
    assert 60 <= hi <= 70


# ── build_unit_translation_lines ──

def test_build_lines_inline_range():
    """每行格式: [N] (X-Y字) <英文>"""
    from pipeline import build_unit_translation_lines
    batch = [
        {"start": 0.0, "end": 5.0, "text": "Hello world."},
        {"start": 5.0, "end": 10.0, "text": "Second sentence here."},
    ]
    lines = build_unit_translation_lines(batch)
    assert len(lines) == 2
    assert lines[0].startswith("[1] (")
    assert "字)" in lines[0]
    assert lines[0].endswith("Hello world.")
    assert lines[1].startswith("[2] (")
    assert "Second sentence here." in lines[1]


def test_build_lines_continuation_marker():
    """碎片续段保留 {续} 标记 (与原行为兼容)"""
    from pipeline import build_unit_translation_lines
    batch = [
        {"start": 0.0, "end": 2.0, "text": "Part one"},
        {"start": 2.0, "end": 4.0, "text": "part two."},
    ]
    seg_to_group = {1: ("g1", 1)}  # idx 1 是组内第二段
    lines = build_unit_translation_lines(batch, seg_to_group=seg_to_group)
    # 第二行带 {续} 标记
    assert "{续}" in lines[1]


# ── strip_char_count_prefix ──

def test_strip_single_char_count():
    from text_utils import strip_char_count_prefix
    assert strip_char_count_prefix("(35) 译文内容") == "译文内容"
    assert strip_char_count_prefix("(35字) 译文内容") == "译文内容"


def test_strip_range_char_count():
    from text_utils import strip_char_count_prefix
    assert strip_char_count_prefix("(32-46字) 译文内容") == "译文内容"
    assert strip_char_count_prefix("(32-46) 译文内容") == "译文内容"


def test_strip_no_prefix_unchanged():
    from text_utils import strip_char_count_prefix
    assert strip_char_count_prefix("译文内容") == "译文内容"
    assert strip_char_count_prefix("Just plain text") == "Just plain text"


# ── 解析器集成: [N] (字数) 译文 ──

def test_parser_handles_char_count_format():
    """LLM 输出 [N] (字数) 译文 时解析器能正确剥离"""
    from pipeline import _parse_numbered_translations
    raw = "[1] (35) 第一段译文内容\n[2] (28) 第二段译文内容"
    result = _parse_numbered_translations(raw, expected_count=2)
    assert result[0] == "第一段译文内容", f"got: {result[0]}"
    assert result[1] == "第二段译文内容"


def test_parser_handles_range_format():
    from pipeline import _parse_numbered_translations
    raw = "[1] (32-46字) 第一段\n[2] (28-40字) 第二段"
    result = _parse_numbered_translations(raw, expected_count=2)
    assert result[0] == "第一段"
    assert result[1] == "第二段"


def test_parser_legacy_no_char_count():
    """旧格式 [N] 译文 仍能解析 (向后兼容)"""
    from pipeline import _parse_numbered_translations
    raw = "[1] 第一段\n[2] 第二段"
    result = _parse_numbered_translations(raw, expected_count=2)
    assert result[0] == "第一段"
    assert result[1] == "第二段"
