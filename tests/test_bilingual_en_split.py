"""测试: 双语字幕英文按时间(字符比例)分段
   原行为: 整段 text_en 只在第一条子行显示
   新行为: text_en 按各子行汉字比例分配,与中文同步显示
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_split_proportional_two_lines():
    """中文按 1:1 切两行 → 英文也按 1:1 分两半"""
    from pipeline import split_english_proportional
    en = "Hello world this is a test sentence here"
    parts = split_english_proportional(en, [0.5, 0.5])
    assert len(parts) == 2
    # 每部分应非空
    assert parts[0] and parts[1]
    # 拼接应包含原文所有词
    combined = " ".join(parts)
    for w in en.split():
        assert w in combined


def test_split_proportional_uneven():
    """中文按 2:1 切 → 英文也按 2:1 分"""
    from pipeline import split_english_proportional
    en = "one two three four five six seven eight nine"
    parts = split_english_proportional(en, [2/3, 1/3])
    assert len(parts) == 2
    # 第一部分约 6 词, 第二部分约 3 词
    assert len(parts[0].split()) >= 4
    assert len(parts[1].split()) >= 1


def test_split_single_ratio_returns_single():
    """单 ratio → 不切"""
    from pipeline import split_english_proportional
    en = "single ratio test"
    parts = split_english_proportional(en, [1.0])
    assert parts == [en]


def test_split_empty_text():
    """空 text_en → 空字符串列表"""
    from pipeline import split_english_proportional
    parts = split_english_proportional("", [0.5, 0.5])
    assert parts == ["", ""]


def test_split_three_lines():
    """三行均分"""
    from pipeline import split_english_proportional
    en = "a b c d e f g h i"  # 9 词
    parts = split_english_proportional(en, [1/3, 1/3, 1/3])
    assert len(parts) == 3
    # 拼接还是原文 (词序保留)
    assert " ".join(parts) == en


def test_subtitle_line_has_per_line_en():
    """split_unit_into_subtitle_lines: 每个 subline 的 text_en 应是该行对应的英文片段"""
    from pipeline import split_unit_into_subtitle_lines
    seg = {
        "text_zh": "今天天气很好的样子，我们打算出去玩玩。下面继续吧。",
        "text_en": "It is a nice day today and we are going out. Let us continue.",
        "start": 0.0, "end": 6.0,
    }
    parts = split_unit_into_subtitle_lines(seg, max_chars=14)
    assert len(parts) >= 2
    # 各 subline 都应有非空 text_en
    for p in parts:
        assert p["text_en"], f"subline 缺 text_en: {p}"
    # 各 text_en 累加包含所有原英文词
    combined = " ".join(p["text_en"] for p in parts)
    for w in seg["text_en"].split():
        assert w in combined, f"丢失词: {w}"
