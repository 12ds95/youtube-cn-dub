#!/usr/bin/env python3
"""
测试迭代优化的邻段重复检测和去重逻辑。
来源：devlog/2026-03-28-refine-duplicate-translation.md
复现场景：output/32884a7ba3d 迭代优化中 LLM 精简时抄相邻段内容
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import _is_duplicate_of_neighbors, _char_overlap_ratio, deduplicate_segments


# ─── 邻段重复检测测试 ───────────────────────────────────────────

def test_exact_duplicate_neighbor():
    """完全相同的相邻段应被检测为重复"""
    segments = [
        {"text_zh": "这是第一段"},
        {"text_zh": "这是第二段"},
        {"text_zh": "这是第三段"},
    ]
    assert _is_duplicate_of_neighbors("这是第一段", 1, segments) is True   # #1 的邻居 #0 完全相同
    assert _is_duplicate_of_neighbors("完全不同的内容", 1, segments) is False  # 和邻居都不同
    print("  ✅ test_exact_duplicate_neighbor")


def test_substring_duplicate_neighbor():
    """子串包含关系应被检测为重复"""
    segments = [
        {"text_zh": "这次合作对我们双方而言都是新尝试"},
        {"text_zh": "占位"},
        {"text_zh": "占位"},
    ]
    assert _is_duplicate_of_neighbors("合作对我们双方而言都是新尝试", 1, segments) is True
    print("  ✅ test_substring_duplicate_neighbor")


def test_similar_duplicate_from_real_case():
    """
    复现 32884a7ba3d 实际 bug：
    iter_1 把 #6 精简成了和 #7 几乎相同的话
    """
    segments = [
        {"text_zh": "占位 0"},
        {"text_zh": "占位 1"},
        {"text_zh": "占位 2"},
        {"text_zh": "占位 3"},
        {"text_zh": "占位 4"},
        {"text_zh": "占位 5"},
        {"text_zh": "因为它真正令人惊叹之处，唯有亲身体验才能体会。"},  # #6 原始
        {"text_zh": "这无疑是我有幸参与过的最酷炫的项目之一。"},         # #7
    ]
    # LLM 把 #6 精简成了抄 #7 的内容
    bad_refine = "这无疑是我参与过最酷的项目之一。"
    assert _is_duplicate_of_neighbors(bad_refine, 6, segments) is True
    print("  ✅ test_similar_duplicate_from_real_case (#6 抄 #7)")


def test_similar_duplicate_case2():
    """
    复现 32884a7ba3d 实际 bug：
    iter_2 把 #4 改成了和 #3 几乎相同的话
    """
    segments = [
        {"text_zh": "占位 0"},
        {"text_zh": "占位 1"},
        {"text_zh": "占位 2"},
        {"text_zh": "这次合作对我们双方而言，都是一次新尝试。"},  # #3
        {"text_zh": "所有网页开发工作完全归功于本，我就不多介绍了。"},  # #4 原始
    ]
    bad_refine = "这次合作对我们双方都是新尝试。"
    assert _is_duplicate_of_neighbors(bad_refine, 4, segments) is True
    print("  ✅ test_similar_duplicate_case2 (#4 抄 #3)")


def test_legitimate_refine_not_blocked():
    """正常精简不应被误判为重复"""
    segments = [
        {"text_zh": "占位 0"},
        {"text_zh": "接下来带您访问一个专门网站，观看我们称作\u201c可探索视频\u201d的简短序列。"},
        {"text_zh": "视频由本·埃德协助完成——您可能认识他。"},
    ]
    good_refine = "接下来带您访问网站，观看可探索视频。"
    assert _is_duplicate_of_neighbors(good_refine, 1, segments) is False
    print("  ✅ test_legitimate_refine_not_blocked")


def test_partial_overlap_allowed():
    """
    部分开头词重叠但包含新信息的翻译应被放行。
    复现：#14 开头含 #13 的词但有大量新内容。
    """
    segments = [
        {"text_zh": "占位"},
        {"text_zh": "其中一个重要原因，尤其是对程序员而言，"},          # #13
        {"text_zh": "就在于它能为描述三维空间中的朝向提供一种极为优雅且高效的方式。"},  # #14 原始
    ]
    refine_14 = "尤其对程序员而言，它能优雅高效地描述三维朝向。"
    result = _is_duplicate_of_neighbors(refine_14, 2, segments)
    # 0.47 重叠率 < 0.6 阈值，应放行
    assert result is False
    print("  ✅ test_partial_overlap_allowed (#14 含新信息)")


# ─── 字符重叠率测试 ──────────────────────────────────────────────

def test_char_overlap_identical():
    """完全相同的文本重叠率应为 1.0"""
    assert _char_overlap_ratio("你好世界", "你好世界") == 1.0
    print("  ✅ test_char_overlap_identical")


def test_char_overlap_no_common():
    """完全无公共字符重叠率应接近 0"""
    ratio = _char_overlap_ratio("你好", "再见")
    assert ratio < 0.5
    print("  ✅ test_char_overlap_no_common")


def test_char_overlap_near_synonym():
    """近义改写的中文应有较高重叠率"""
    a = "这无疑是我参与过最酷的项目之一。"
    b = "这无疑是我有幸参与过的最酷炫的项目之一。"
    ratio = _char_overlap_ratio(a, b)
    assert ratio > 0.8, f"近义句重叠率应 > 0.8, 实际 {ratio:.2f}"
    print(f"  ✅ test_char_overlap_near_synonym (ratio={ratio:.2f})")


# ─── 去重函数测试 ────────────────────────────────────────────────

def test_dedup_exact_consecutive():
    """连续完全相同的片段应被合并"""
    segs = [
        {"start": 0.0, "end": 1.0, "text": "hello"},
        {"start": 1.0, "end": 2.0, "text": "hello"},
        {"start": 2.0, "end": 3.0, "text": "world"},
    ]
    result = deduplicate_segments(segs)
    assert len(result) == 2
    assert result[0]["end"] == 2.0  # 时间戳合并
    assert result[1]["text"] == "world"
    print("  ✅ test_dedup_exact_consecutive")


def test_dedup_substring_consecutive():
    """连续子串包含关系应被合并（保留较长的）"""
    segs = [
        {"start": 0.0, "end": 1.0, "text": "hello world foo bar"},
        {"start": 1.0, "end": 2.0, "text": "hello world"},
        {"start": 2.0, "end": 3.0, "text": "different text entirely"},
    ]
    result = deduplicate_segments(segs)
    assert len(result) == 2
    assert "hello world foo bar" in result[0]["text"]
    print("  ✅ test_dedup_substring_consecutive")


def test_dedup_no_false_positive():
    """不同的片段不应被错误合并"""
    segs = [
        {"start": 0.0, "end": 1.0, "text": "first segment"},
        {"start": 1.0, "end": 2.0, "text": "second segment"},
        {"start": 2.0, "end": 3.0, "text": "third segment"},
    ]
    result = deduplicate_segments(segs)
    assert len(result) == 3
    print("  ✅ test_dedup_no_false_positive")


if __name__ == "__main__":
    print("邻段重复检测测试:")
    test_exact_duplicate_neighbor()
    test_substring_duplicate_neighbor()
    test_similar_duplicate_from_real_case()
    test_similar_duplicate_case2()
    test_legitimate_refine_not_blocked()
    test_partial_overlap_allowed()
    print()
    print("字符重叠率测试:")
    test_char_overlap_identical()
    test_char_overlap_no_common()
    test_char_overlap_near_synonym()
    print()
    print("去重函数测试:")
    test_dedup_exact_consecutive()
    test_dedup_substring_consecutive()
    test_dedup_no_false_positive()
    print()
    print("  全部通过")
