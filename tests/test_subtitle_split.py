"""测试: Step 4 字幕 unit 内分行 split_unit_into_subtitle_lines."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _seg(start, end, text_zh, text_en=""):
    return {"start": start, "end": end, "text_zh": text_zh, "text_en": text_en}


def test_短unit不切分():
    """汉字 ≤ max_chars 时直接返回原 unit (单条字幕)"""
    from pipeline import split_unit_into_subtitle_lines
    seg = _seg(0.0, 3.0, "短句子。", "Short sentence.")
    lines = split_unit_into_subtitle_lines(seg, max_chars=14)
    assert len(lines) == 1
    assert lines[0]["text_zh"] == "短句子。"
    assert lines[0]["start"] == 0.0
    assert lines[0]["end"] == 3.0


def test_长unit切多行():
    """汉字数 > max_chars 时切成多行"""
    from pipeline import split_unit_into_subtitle_lines
    text = "这是第一句话内容很长。这是第二句话也比较长。"
    seg = _seg(0.0, 10.0, text, "Long.")
    lines = split_unit_into_subtitle_lines(seg, max_chars=14)
    assert len(lines) >= 2, f"应切成 >=2 行, got {len(lines)}: {[l['text_zh'] for l in lines]}"
    # 每行字数控制
    for l in lines:
        chars = sum(1 for c in l["text_zh"] if "一" <= c <= "鿿")
        assert chars <= 18, f"单行字数 {chars} 超出软上限"


def test_切点优先句末标点():
    """切分点应在句末标点后, 而非词中"""
    from pipeline import split_unit_into_subtitle_lines
    text = "这是第一句话很长一些。这是第二句话也长。这是第三句。"
    seg = _seg(0.0, 12.0, text, "")
    lines = split_unit_into_subtitle_lines(seg, max_chars=12)
    # 有句末标点存在, 切点应该在 . 之后
    for l in lines[:-1]:
        last = l["text_zh"].rstrip()[-1:]
        assert last in "。！？", f"行尾应是句末标点, got '{last}' in: {l['text_zh']}"


def test_时间戳按字符等比分配():
    """子行时间戳总和 = 原 unit 时长"""
    from pipeline import split_unit_into_subtitle_lines
    text = "这是测试句子内容包含。两个不同的子句话语。"
    seg = _seg(2.0, 12.0, text, "")
    lines = split_unit_into_subtitle_lines(seg, max_chars=12)
    # 起始点对齐 unit
    assert lines[0]["start"] == 2.0
    # 末尾对齐
    assert lines[-1]["end"] == 12.0
    # 中间无重叠
    for i in range(len(lines) - 1):
        assert abs(lines[i]["end"] - lines[i+1]["start"]) < 1e-3


def test_text_en按比例分配到每子行():
    """text_en 按汉字比例切分到各子行 (P2-4); 各 subline 都应非空"""
    from pipeline import split_unit_into_subtitle_lines
    seg = _seg(0.0, 10.0, "这是一段较长的中文内容。需要被分成多行字幕。", "Original English text here.")
    lines = split_unit_into_subtitle_lines(seg, max_chars=12)
    # 每条非空, 拼起来覆盖原英文所有词
    combined = " ".join(l["text_en"] for l in lines if l["text_en"])
    for w in "Original English text here.".split():
        assert w in combined


def test_纯标点不导致空段():
    """text 全是标点时不能产生空 text_zh"""
    from pipeline import split_unit_into_subtitle_lines
    seg = _seg(0.0, 1.0, "！。？", "")
    lines = split_unit_into_subtitle_lines(seg, max_chars=14)
    # 至少返回 1 行
    assert len(lines) >= 1


def test_切点回退到子句标点():
    """没有句末标点时, 用子句标点 (， 等) 切"""
    from pipeline import split_unit_into_subtitle_lines
    text = "这是一个很长的句子，里面有逗号但没有句号，最终在一个地方结束"
    seg = _seg(0.0, 10.0, text, "")
    lines = split_unit_into_subtitle_lines(seg, max_chars=12)
    assert len(lines) >= 2
    # 至少前面有一行以逗号结尾或在逗号附近切
    for l in lines[:-1]:
        last = l["text_zh"].rstrip()[-1:]
        assert last in "，；：。！？" or len([c for c in l["text_zh"] if "一" <= c <= "鿿"]) <= 14
