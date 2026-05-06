#!/usr/bin/env python3
"""
测试统一质量守卫：_validate_text_adjustment 扩展 + _validate_translation_retry + merge CPS guard
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import (
    _validate_text_adjustment,
    _validate_translation_retry,
    _log_zh_change,
    merge_short_segments,
)

SAMPLE_SEGMENTS = [
    {"start": 0.0, "end": 3.0, "text_en": "Hello world", "text_zh": "你好世界欢迎来到"},
    {"start": 3.0, "end": 6.0, "text_en": "This is a test", "text_zh": "这是一个测试内容"},
    {"start": 6.0, "end": 9.0, "text_en": "Another segment here", "text_zh": "另一个片段在这里"},
]


# ── _validate_text_adjustment mode="refine" ──────────────────────────

def test_refine_mode_passes_normal():
    """refine 模式: 正常改写通过"""
    old = "这是一段比较长的中文翻译内容"
    new = "这是一段中文翻译内容"  # 略短，在范围内
    valid, reason = _validate_text_adjustment(new, old, 1, SAMPLE_SEGMENTS, mode="refine",
                                              compression_floor=0.60, expansion_ceiling=1.50)
    assert valid, f"Expected pass, got rejected: {reason}"
    print("  ✅ test_refine_mode_passes_normal")


def test_refine_mode_rejects_over_compressed():
    """refine 模式: 压缩过度拒绝"""
    old = "这是一段比较长的中文翻译内容需要多一些字"  # 18 chars
    new = "这是比较长"  # 5 chars, < 18*0.6=10.8 → over_compressed
    valid, reason = _validate_text_adjustment(new, old, 1, SAMPLE_SEGMENTS, mode="refine",
                                              compression_floor=0.60, expansion_ceiling=1.50)
    assert not valid and reason == "over_compressed", f"Expected over_compressed, got {reason}"
    print("  ✅ test_refine_mode_rejects_over_compressed")


def test_refine_mode_rejects_over_expanded():
    """refine 模式: 扩展过度拒绝"""
    old = "短文本内容"
    new = "这是一段非常非常非常长的文本内容它远远超过了原文长度的一点五倍以上因此应该被拒绝掉"
    valid, reason = _validate_text_adjustment(new, old, 1, SAMPLE_SEGMENTS, mode="refine",
                                              compression_floor=0.60, expansion_ceiling=1.50)
    assert not valid and reason == "over_expanded", f"Expected over_expanded, got {reason}"
    print("  ✅ test_refine_mode_rejects_over_expanded")


def test_refine_mode_shrink_only_no_expand_check():
    """shrink 模式不检查 expansion_ceiling"""
    old = "短文本"
    new = "这是一段比原文长很多的文本内容在这里放着"
    valid, reason = _validate_text_adjustment(new, old, 1, SAMPLE_SEGMENTS, mode="shrink",
                                              compression_floor=0.60, expansion_ceiling=1.50)
    # shrink mode does NOT check expansion ceiling
    # But it would fail length filter in _select_best_candidate before reaching here
    # For the gate itself: shrink only checks compression_floor
    assert valid or reason != "over_expanded", f"shrink mode should not check expansion"
    print("  ✅ test_refine_mode_shrink_only_no_expand_check")


def test_check_repetition_flag():
    """check_repetition=True 检测段内重复"""
    old = "这是原始翻译内容"
    # Create repetitive text
    new = "这是重复这是重复这是重复这是重复这是重复"
    valid, reason = _validate_text_adjustment(new, old, 1, SAMPLE_SEGMENTS, mode="refine",
                                              compression_floor=0.30, expansion_ceiling=3.0,
                                              fidelity_threshold=0.10,
                                              check_repetition=True)
    assert not valid and reason == "repetition", f"Expected repetition, got {reason}"
    print("  ✅ test_check_repetition_flag")


def test_check_repetition_false_no_check():
    """check_repetition=False 不检测段内重复"""
    old = "这是原始翻译内容"
    new = "这是重复这是重复这是重复这是重复这是重复"
    valid, reason = _validate_text_adjustment(new, old, 1, SAMPLE_SEGMENTS, mode="refine",
                                              compression_floor=0.30, expansion_ceiling=3.0,
                                              fidelity_threshold=0.10,
                                              check_repetition=False)
    # Without repetition check, it passes (other checks may still reject)
    # If it passes, great; if it fails, it should NOT be for "repetition"
    if not valid:
        assert reason != "repetition", f"Should not check repetition when flag is False"
    print("  ✅ test_check_repetition_false_no_check")


# ── _validate_translation_retry ──────────────────────────────────────

def test_retry_valid_translation():
    """正常翻译通过轻量验证"""
    valid, reason = _validate_translation_retry("这是正确的翻译结果", "This is a correct translation", 1, SAMPLE_SEGMENTS)
    assert valid, f"Expected pass, got rejected: {reason}"
    print("  ✅ test_retry_valid_translation")


def test_retry_too_short():
    """过短翻译被拒"""
    valid, reason = _validate_translation_retry("好", "Hello world", 1, SAMPLE_SEGMENTS)
    assert not valid and reason == "too_short", f"Expected too_short, got {reason}"
    print("  ✅ test_retry_too_short")


def test_retry_untranslated_english():
    """含未翻译英文的重试结果被拒"""
    # "coolest" is not in the English source
    valid, reason = _validate_translation_retry(
        "这是一段 coolest 的翻译",
        "This is a translation",
        1, SAMPLE_SEGMENTS
    )
    assert not valid and reason == "untranslated_english", f"Expected untranslated_english, got {reason}"
    print("  ✅ test_retry_untranslated_english")


def test_retry_english_in_source_ok():
    """英文原文中存在的词允许出现在翻译中"""
    valid, reason = _validate_translation_retry(
        "这是关于 quaternions 的翻译",
        "This is about quaternions",
        1, SAMPLE_SEGMENTS
    )
    assert valid, f"Expected pass (quaternions is in source), got rejected: {reason}"
    print("  ✅ test_retry_english_in_source_ok")


def test_retry_repetition():
    """段内重复的重试结果被拒"""
    valid, reason = _validate_translation_retry(
        "重复重复重复重复重复重复重复重复重复重复",
        "Some English text here",
        1, SAMPLE_SEGMENTS
    )
    assert not valid and reason == "repetition", f"Expected repetition, got {reason}"
    print("  ✅ test_retry_repetition")


def test_retry_duplicate_of_neighbor():
    """与邻段重复的重试结果被拒"""
    # Use the same text as SAMPLE_SEGMENTS[0]
    valid, reason = _validate_translation_retry(
        "你好世界欢迎来到",  # exact copy of segment 0
        "Something different in English",
        1, SAMPLE_SEGMENTS  # idx=1, neighbor is idx=0
    )
    assert not valid and reason == "duplicate", f"Expected duplicate, got {reason}"
    print("  ✅ test_retry_duplicate_of_neighbor")


# ── merge_short_segments CPS guard ───────────────────────────────────

def test_merge_cps_guard_allows_normal():
    """正常合并不被 CPS 守卫拦截"""
    segments = [
        {"start": 0.0, "end": 5.0, "text_zh": "前面的正常翻译内容", "text_en": "Normal prev"},
        {"start": 5.0, "end": 6.0, "text_zh": "好", "text_en": "OK"},  # short, will merge
        {"start": 6.0, "end": 9.0, "text_zh": "后面的内容", "text_en": "After"},
    ]
    result = merge_short_segments(segments, min_chars=3)
    # "好" should merge into prev since CPS is fine
    assert len(result) < len(segments), f"Expected merge, got {len(result)} segments"
    print("  ✅ test_merge_cps_guard_allows_normal")


def test_merge_cps_guard_blocks_high_cps():
    """CPS > 12 时拒绝合并"""
    # Create a scenario: prev segment is 1 second with lots of Chinese text
    # Adding more text would push CPS > 12
    segments = [
        {"start": 0.0, "end": 1.0, "text_zh": "已有十个中文字符号内容", "text_en": "Short"},  # 10 zh_chars / 1s = 10 CPS
        {"start": 1.0, "end": 1.5, "text_zh": "加", "text_en": "Add"},  # short, would merge to prev
        {"start": 1.5, "end": 5.0, "text_zh": "后面的正常内容", "text_en": "After"},
    ]
    result = merge_short_segments(segments, min_chars=3)
    # After merge: "已有十个中文字符号内容加" = 11 zh_chars in 1.5s = 7.3 CPS - still OK
    # Let's make it more extreme
    segments2 = [
        {"start": 0.0, "end": 0.5, "text_zh": "十二个中文字在这里面", "text_en": "Short"},  # 9 zh / 0.5s = 18 CPS already!
        {"start": 0.5, "end": 0.8, "text_zh": "加", "text_en": "Add"},  # merge would make 10 zh / 0.8s = 12.5 > 12
        {"start": 0.8, "end": 5.0, "text_zh": "后面正常内容", "text_en": "After"},
    ]
    result2 = merge_short_segments(segments2, min_chars=3)
    # "加" should NOT merge to prev because CPS would exceed 12
    assert len(result2) == 3, f"Expected CPS guard to block merge, got {len(result2)} segments"
    print("  ✅ test_merge_cps_guard_blocks_high_cps")


# ── _log_zh_change ───────────────────────────────────────────────────

def test_log_zh_change_format():
    """日志记录格式正确"""
    log = []
    _log_zh_change(log, 5, "旧翻译", "新翻译", "test_reason", extra={"ratio": 1.2})
    assert len(log) == 1
    entry = log[0]
    assert entry["idx"] == 5
    assert entry["old_zh"] == "旧翻译"
    assert entry["new_zh"] == "新翻译"
    assert entry["reason"] == "test_reason"
    assert entry["ratio"] == 1.2
    print("  ✅ test_log_zh_change_format")


def test_log_zh_change_no_extra():
    """无额外字段时正常工作"""
    log = []
    _log_zh_change(log, 0, "old", "new", "reason")
    assert "ratio" not in log[0]
    assert log[0]["reason"] == "reason"
    print("  ✅ test_log_zh_change_no_extra")


if __name__ == "__main__":
    print("统一质量守卫测试:")
    # _validate_text_adjustment refine mode
    test_refine_mode_passes_normal()
    test_refine_mode_rejects_over_compressed()
    test_refine_mode_rejects_over_expanded()
    test_refine_mode_shrink_only_no_expand_check()
    test_check_repetition_flag()
    test_check_repetition_false_no_check()
    # _validate_translation_retry
    test_retry_valid_translation()
    test_retry_too_short()
    test_retry_untranslated_english()
    test_retry_english_in_source_ok()
    test_retry_repetition()
    test_retry_duplicate_of_neighbor()
    # merge CPS guard
    test_merge_cps_guard_allows_normal()
    test_merge_cps_guard_blocks_high_cps()
    # _log_zh_change
    test_log_zh_change_format()
    test_log_zh_change_no_extra()
    print("  全部通过 ✅")
