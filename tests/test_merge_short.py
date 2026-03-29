#!/usr/bin/env python3
"""
测试：merge_short_segments 短段合并逻辑。
来源：f09d1957a98 运行中 #659 "例"、#773 "即" 等极短片段导致 TTS 失败。
注意：不调用 edge-tts 等外部服务，仅测试纯逻辑。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import merge_short_segments


def test_merge_to_prev():
    """短段（<3字）应合并到前一段"""
    segments = [
        {"start": 0, "end": 2, "text_en": "For example,", "text_zh": "例如，"},
        {"start": 2, "end": 2.36, "text_en": "For example,", "text_zh": "例"},
        {"start": 2.36, "end": 5, "text_en": "we can see that", "text_zh": "我们可以看到"},
    ]
    merged = merge_short_segments(segments, min_chars=3)
    assert len(merged) == 2, f"应合并为 2 段, got {len(merged)}"
    # "例" 合并到前一段
    assert "例" in merged[0]["text_zh"]
    assert merged[0]["end"] == 2.36


def test_merge_to_next_when_no_prev():
    """第一个段就是短段，应合并到后一段"""
    segments = [
        {"start": 0, "end": 0.28, "text_en": "I mean,", "text_zh": "即"},
        {"start": 0.28, "end": 3, "text_en": "that's the point", "text_zh": "这就是关键"},
    ]
    merged = merge_short_segments(segments, min_chars=3)
    assert len(merged) == 1, f"应合并为 1 段, got {len(merged)}"
    assert merged[0]["text_zh"].startswith("即")
    assert merged[0]["start"] == 0


def test_no_merge_for_normal_segments():
    """正常长度的段不应被合并"""
    segments = [
        {"start": 0, "end": 2, "text_en": "Hello world", "text_zh": "你好世界"},
        {"start": 2, "end": 4, "text_en": "Goodbye", "text_zh": "再见朋友"},
    ]
    merged = merge_short_segments(segments, min_chars=3)
    assert len(merged) == 2, "正常段不应合并"


def test_merge_preserves_text_en():
    """合并时英文原文也应拼接"""
    segments = [
        {"start": 0, "end": 2, "text_en": "For example,", "text_zh": "例如，"},
        {"start": 2, "end": 2.5, "text_en": "right?", "text_zh": "对"},
        {"start": 2.5, "end": 5, "text_en": "we can see", "text_zh": "我们可以看到"},
    ]
    merged = merge_short_segments(segments, min_chars=3)
    assert len(merged) == 2
    # "对" 合并到前一段，英文也拼接
    assert "right?" in merged[0]["text_en"]


def test_multiple_short_segments():
    """连续多个短段依次合并"""
    segments = [
        {"start": 0, "end": 2, "text_en": "Well,", "text_zh": "嗯，"},
        {"start": 2, "end": 2.3, "text_en": "I", "text_zh": "我"},
        {"start": 2.3, "end": 2.6, "text_en": "mean", "text_zh": "是"},
        {"start": 2.6, "end": 5, "text_en": "that's important", "text_zh": "这很重要"},
    ]
    merged = merge_short_segments(segments, min_chars=3)
    # "我" 和 "是" 都应合并到前一段
    assert len(merged) == 2, f"应合并为 2 段, got {len(merged)}"
    assert "我" in merged[0]["text_zh"]
    assert "是" in merged[0]["text_zh"]


def test_empty_segments():
    """空列表不应报错"""
    assert merge_short_segments([]) == []


def test_real_case_659():
    """复现 f09d1957a98 #659 "例" 的真实场景"""
    segments = [
        {"start": 100.0, "end": 103.5, "text_en": "Consider the following example.",
         "text_zh": "考虑以下的例子。"},
        {"start": 103.5, "end": 103.86, "text_en": "For example,",
         "text_zh": "例"},
        {"start": 103.86, "end": 107.0, "text_en": "let's see what it looks like for j",
         "text_zh": "以j为例来看看效果"},
    ]
    merged = merge_short_segments(segments, min_chars=3)
    assert len(merged) == 2, f"应合并为 2 段, got {len(merged)}"
    # "例" 合并到前一段
    assert merged[0]["text_zh"] == "考虑以下的例子。例"
    assert merged[0]["end"] == 103.86


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  \u2705 {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  \u274c {t.__name__}: {e}")
            failed += 1
    icon = '\u2705' if failed == 0 else '\u274c'
    print(f"\n{icon} {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
