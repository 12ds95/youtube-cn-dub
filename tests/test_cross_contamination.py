#!/usr/bin/env python3
"""
TDD: 跨段内容污染检测与修复

问题场景：
  batch翻译时 next_preview 暴露后续英文，LLM 将未来段的内容"翻译"到当前段。
  例: seg22 EN="...other ways to think about computing"
      seg22 ZH="熟悉线性代数者可知，3×3旋转矩阵..."  ← 实为 seg24 的内容
      seg24 EN="...linear algebra...3x3 matrices..."
      seg24 ZH="熟稔线性代数者皆知：3×3矩阵..."      ← 正确

  现有 window dedup 只重译后者(seg24)，但真正被污染的是前者(seg22)。
  
修复目标：
  当检测到近重复对(j,k)时，两段都加入重译列表，逐条重译消除污染。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import _char_overlap_ratio


# ── 测试数据：模拟真实污染场景 ──────────────────────────────────────

CONTAMINATED_SEGMENTS = [
    # seg 20
    {"text_en": "millions of devices that use quaternions to track the phone's model for how it's oriented in",
     "text_zh": "手机用四元数追踪朝向；你几乎肯定在用——"},
    # seg 21
    {"text_en": "space. That's right, your phone almost certainly has software running somewhere inside of it",
     "text_zh": "但旋转算法不止四元数，有些更易理解。例如——"},
    # seg 22 ← CONTAMINATED: zh talks about linear algebra (seg 24's content)
    {"text_en": "that relies on quaternions. The thing is, there are other ways to think about computing",
     "text_zh": "熟悉线性代数者可知，3×3旋转矩阵可描述三维旋转。"},
    # seg 23
    {"text_en": "rotations, many of which are way simpler to think about than quaternions. For example,",
     "text_zh": "常见做法是绕三轴依次旋转，每个角度即对应欧拉角——"},
    # seg 24 ← CORRECT: zh matches its own en
    {"text_en": "any of you familiar with linear algebra will know that 3x3 matrices can really nicely",
     "text_zh": "熟稔线性代数者皆知：3×3矩阵可优雅表征三维变换"},
    # seg 25
    {"text_en": "describe 3D transformations, and a common way that many programmers think about constructing",
     "text_zh": "而许多程序员在构建旋转矩阵时，常采用的一种常见思路是："},
]


def test_char_overlap_detects_contamination():
    """字符重叠率能检测出 seg22 和 seg24 的近重复"""
    zh_22 = CONTAMINATED_SEGMENTS[2]["text_zh"]  # seg 22
    zh_24 = CONTAMINATED_SEGMENTS[4]["text_zh"]  # seg 24
    ratio = _char_overlap_ratio(zh_22, zh_24)
    assert ratio > 0.6, f"Expected overlap > 0.6, got {ratio:.3f}"
    print(f"  ✅ test_char_overlap_detects_contamination (ratio={ratio:.3f})")


def test_window_dedup_finds_both_segments():
    """
    全局窗口去重应将近重复对的【两段都】加入重译列表。
    现有逻辑只加 j (后者)，修复后应加 j 和 k (两者都加)。
    """
    from pipeline import _detect_cross_contamination
    
    segments = CONTAMINATED_SEGMENTS
    retry_indices = _detect_cross_contamination(segments, window=3, threshold=0.6)
    
    # seg 22 (index 2) 和 seg 24 (index 4) 都应在重译列表中
    assert 2 in retry_indices, f"Contaminated seg 22 (idx=2) should be in retry list, got {retry_indices}"
    assert 4 in retry_indices, f"Duplicate seg 24 (idx=4) should be in retry list, got {retry_indices}"
    print(f"  ✅ test_window_dedup_finds_both_segments (retry={sorted(retry_indices)})")


def test_no_false_positives_on_clean_segments():
    """正常翻译不应触发跨段污染检测"""
    clean_segments = [
        {"text_en": "Hello world", "text_zh": "你好世界"},
        {"text_en": "This is a test", "text_zh": "这是一个测试"},
        {"text_en": "Machine learning is great", "text_zh": "机器学习很棒"},
        {"text_en": "Deep neural networks", "text_zh": "深度神经网络"},
    ]
    from pipeline import _detect_cross_contamination
    retry_indices = _detect_cross_contamination(clean_segments, window=3, threshold=0.6)
    assert len(retry_indices) == 0, f"Expected no retries for clean data, got {retry_indices}"
    print("  ✅ test_no_false_positives_on_clean_segments")


def test_short_segments_skipped():
    """短段(<8字符)不应触发检测"""
    short_segments = [
        {"text_en": "Hi", "text_zh": "嗨"},
        {"text_en": "OK", "text_zh": "好的"},
        {"text_en": "Yes", "text_zh": "是"},
    ]
    from pipeline import _detect_cross_contamination
    retry_indices = _detect_cross_contamination(short_segments, window=3, threshold=0.6)
    assert len(retry_indices) == 0, f"Short segments should be skipped, got {retry_indices}"
    print("  ✅ test_short_segments_skipped")


def test_substring_containment_triggers():
    """子串包含也应触发检测（两段都加入重译列表）"""
    segments = [
        {"text_en": "The quick brown fox", "text_zh": "敏捷的棕色狐狸跳过了那只懒惰的狗"},
        {"text_en": "jumps over the lazy dog", "text_zh": "敏捷的棕色狐狸跳过了那只懒惰的狗在草地上"},  # contains above
        {"text_en": "Something else entirely", "text_zh": "完全不同的东西和内容放在这里"},
    ]
    from pipeline import _detect_cross_contamination
    retry_indices = _detect_cross_contamination(segments, window=3, threshold=0.6)
    assert 0 in retry_indices and 1 in retry_indices, \
        f"Substring pair should both be in retry list, got {retry_indices}"
    print(f"  ✅ test_substring_containment_triggers (retry={sorted(retry_indices)})")


if __name__ == "__main__":
    print("跨段内容污染检测测试:")
    test_char_overlap_detects_contamination()
    test_window_dedup_finds_both_segments()
    test_no_false_positives_on_clean_segments()
    test_short_segments_skipped()
    test_substring_containment_triggers()
    print("  全部通过 ✅")
