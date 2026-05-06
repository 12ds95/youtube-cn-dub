"""
测试: group_segments_to_units 把 Whisper 碎片合并到 sentence unit。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import group_segments_to_units


def _seg(start, end, text, words=None):
    s = {"start": start, "end": end, "text": text}
    if words is not None:
        s["words"] = words
    return s


def test_句末标点合并_紧邻两段():
    """段尾有句号 → 与前段同一 unit；下一段开句新 unit"""
    segs = [
        _seg(0.0, 2.0, "In a moment I'll point you to a separate website."),
        _seg(2.1, 4.5, "It was done in collaboration with Ben Eater."),
    ]
    units = group_segments_to_units(segs)
    assert len(units) == 2, f"应得 2 个 unit, got {len(units)}"
    assert units[0]["text"].endswith(".")
    assert units[1]["text"].startswith("It was done")


def test_未结尾段合并到下一段():
    """前段无句末标点 → 与后段同一 unit;
    含内部句末标点的段在中间切分: 前半归前 unit, 后半合并下一段."""
    segs = [
        _seg(0.0, 2.0, "with a little bit of surrounding"),
        _seg(2.1, 4.5, "context. So to set the stage,"),
        _seg(4.6, 7.0, "last video introduced quaternions."),
    ]
    units = group_segments_to_units(segs)
    # Unit 1: "with...surrounding context." (跨段修复破句)
    # Unit 2: "So to set the stage, last video introduced quaternions." (新句完整聚合)
    assert len(units) == 2, f"应合并为 2 unit, got {len(units)}: {[u['text'] for u in units]}"
    assert units[0]["text"].endswith("context.")
    assert "surrounding" in units[0]["text"]
    assert units[1]["text"].startswith("So to set")
    assert units[1]["text"].endswith("quaternions.")


def test_短unit合并到邻居():
    """单 unit < 1.5s 应与相邻 unit 合并"""
    segs = [
        _seg(0.0, 4.0, "This is a complete sentence ending."),
        _seg(4.0, 4.8, "Yes."),  # 0.8s 过短
        _seg(4.8, 8.0, "But the next one is normal length here."),
    ]
    units = group_segments_to_units(segs, min_unit_duration=1.5)
    assert len(units) == 2, f"短 unit 应合并, got {len(units)}: {[u['text'] for u in units]}"


def test_默认min_duration_为2秒():
    """默认 min_unit_duration=2.0,边缘 1.6s unit 应被并到邻居"""
    segs = [
        _seg(0.0, 4.0, "First complete sentence one."),
        _seg(4.0, 5.6, "Short tail piece here."),  # 1.6s 边缘
        _seg(5.6, 9.0, "Then comes another full sentence ending."),
    ]
    units = group_segments_to_units(segs)  # 用默认值
    assert len(units) == 2, f"1.6s 边缘 unit 应合并 (默认 min=2.0), got {len(units)}: {[u['text'] for u in units]}"


def test_超长unit按子句切():
    """单 unit > max_unit_duration 时在子句标点切"""
    long_text = (
        "First clause goes here, second clause continues, "
        "third clause adds info, fourth clause finishes things up."
    )
    segs = [_seg(0.0, 14.0, long_text)]
    units = group_segments_to_units(segs, max_unit_duration=8.0)
    assert len(units) >= 2, f"超长单段应切分, got {len(units)}: {[u['text'] for u in units]}"


def test_无words字段不报错():
    """部分 segment 没有 words 字段时仍能正常合并"""
    segs = [
        _seg(0.0, 2.0, "Hello there"),  # 无 words
        _seg(2.0, 4.0, "world."),
    ]
    units = group_segments_to_units(segs)
    assert len(units) == 1
    assert "Hello there" in units[0]["text"] and "world." in units[0]["text"]


def test_words字段拼接():
    """有 words 的段合并后 words 也拼接"""
    segs = [
        _seg(0.0, 2.0, "Hello there", words=[
            {"start": 0.0, "end": 0.6, "word": "Hello"},
            {"start": 0.7, "end": 1.5, "word": "there"},
        ]),
        _seg(2.0, 4.0, "world.", words=[
            {"start": 2.0, "end": 2.8, "word": "world."},
        ]),
    ]
    units = group_segments_to_units(segs)
    assert len(units) == 1
    assert "words" in units[0]
    assert len(units[0]["words"]) == 3


def test_unit_member_indices():
    """合并后保留原始 segment 索引（便于回溯）"""
    segs = [
        _seg(0.0, 2.0, "Part one"),
        _seg(2.0, 4.0, "part two."),
        _seg(4.0, 6.0, "Independent third."),
    ]
    units = group_segments_to_units(segs)
    assert units[0].get("_unit_member_indices") == [0, 1]
    assert units[1].get("_unit_member_indices") == [2]


def test_时间戳取边界():
    """合并后 start = 首段 start, end = 末段 end"""
    segs = [
        _seg(1.0, 2.0, "A"),
        _seg(2.0, 4.5, "b."),
    ]
    units = group_segments_to_units(segs)
    assert units[0]["start"] == 1.0
    assert units[0]["end"] == 4.5


def test_空输入():
    assert group_segments_to_units([]) == []


def test_单段不变():
    segs = [_seg(0.0, 5.0, "Just one segment ending.")]
    units = group_segments_to_units(segs)
    assert len(units) == 1
    assert units[0]["text"] == "Just one segment ending."


def test_长静音切分():
    """段间 gap > 默认阈值时应切，即使前段无标点"""
    segs = [
        _seg(0.0, 2.0, "Some unfinished thought"),  # 无标点
        _seg(5.0, 8.0, "Then a new idea."),  # 3 秒静音
    ]
    units = group_segments_to_units(segs, min_unit_gap_for_split_ms=500)
    # 3 秒间隙超过 500ms 阈值 → 切
    assert len(units) == 2, f"长静音应切, got {len(units)}: {[u['text'] for u in units]}"
