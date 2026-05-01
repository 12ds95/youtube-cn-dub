#!/usr/bin/env python3
"""
测试批内幻觉检测。

来源: pyvideotrans — 重复输出检测模式
       open-dubbing — no_dubbing_phrases 过滤理念
       本项目实际 Bug — kCc8FmEb1nY "我不想在这里多作赘述" ×7
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import _detect_batch_hallucination


def test_no_hallucination_clean_batch():
    """正常翻译无幻觉"""
    translations = ["你好", "世界", "测试", "翻译", "完成"]
    result = _detect_batch_hallucination(translations)
    assert len(result) == 0


def test_hallucination_exact_repeat():
    """同一翻译重复 3+ 次 → 标记为幻觉"""
    translations = ["我不想在这里多作赘述"] * 5 + ["正常翻译", "另一个"]
    result = _detect_batch_hallucination(translations)
    assert len(result) >= 3, f"应至少标记 3 个幻觉索引, got {len(result)}"


def test_hallucination_threshold_25_percent():
    """batch_size=20 时 threshold = max(3, 20*0.25) = 5"""
    translations = ["幻觉文本"] * 4 + [f"正常{i}" for i in range(16)]
    result = _detect_batch_hallucination(translations)
    # 4 < 5, 不应触发
    assert len(result) == 0, "4次重复不应触发 batch_size=20 的阈值"


def test_hallucination_threshold_small_batch():
    """batch_size=8 时 threshold = max(3, 8*0.25=2) = 3"""
    translations = ["重复文本"] * 3 + [f"正常{i}" for i in range(5)]
    result = _detect_batch_hallucination(translations)
    assert len(result) >= 3


def test_hallucination_with_context_poisoning():
    """与上下文窗口重复: 同一短语在本批出现 2+ 次且存在于 prev_context"""
    prev_context = ["这个在上一批出现过"]
    translations = ["这个在上一批出现过", "这个在上一批出现过", "正常翻译"]
    result = _detect_batch_hallucination(translations, prev_context)
    assert len(result) >= 2, "上下文重复 + 批内 2 次应触发"


def test_no_context_poisoning_single_occurrence():
    """上下文重复但批内只出现 1 次 → 不应触发"""
    prev_context = ["这个在上一批出现过"]
    translations = ["这个在上一批出现过", "正常翻译1", "正常翻译2"]
    result = _detect_batch_hallucination(translations, prev_context)
    assert len(result) == 0


def test_empty_translations_no_crash():
    """空翻译列表不崩溃"""
    assert len(_detect_batch_hallucination([])) == 0
    assert len(_detect_batch_hallucination(["", "", ""])) == 0


def test_too_few_non_empty():
    """非空翻译 < 3 时直接返回空 (避免误报)"""
    result = _detect_batch_hallucination(["你好", "世界", "", ""])
    assert len(result) == 0


def test_returns_indices_not_texts():
    """返回的是索引集合而非文本"""
    translations = ["幻觉"] * 4 + ["正常"]
    result = _detect_batch_hallucination(translations)
    assert all(isinstance(i, int) for i in result)
    assert all(0 <= i < len(translations) for i in result)


def test_real_regression_case():
    """回归: 实际 kCc8FmEb1nY 幻觉 — "我不想在这里多作赘述" 在 batch_size=15 中出现 7 次"""
    translations = (
        ["我不想在这里多作赘述"] * 7
        + [f"正常翻译第{i}段" for i in range(8)]
    )
    result = _detect_batch_hallucination(translations)
    # threshold = max(3, 15*0.25=3.75→3) = 3, 7 >= 3 → 触发
    assert len(result) >= 7, f"7 次重复应全部标记, got {len(result)}"


if __name__ == "__main__":
    print("幻觉检测测试:")
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✅ {name}")
    print("  全部通过")
