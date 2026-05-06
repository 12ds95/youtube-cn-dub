"""测试: Pass 1 批次 prompt 内联本批涉及的 proper_nouns 提示。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_filter_relevant_proper_nouns_in_batch():
    """build_batch_proper_noun_hint: 仅返回本批次实际出现的 proper_nouns"""
    from pipeline import build_batch_proper_noun_hint
    batch = [
        {"text": "It was done with Ben Eater, who is awesome."},
        {"text": "Some other topic about quaternions."},
    ]
    proper_nouns = ["Ben Eater", "Andy Matuszczak", "Apple (苹果)"]
    hint = build_batch_proper_noun_hint(batch, proper_nouns)
    assert "Ben Eater" in hint
    # Andy/Apple 未在本批出现, 不应包含
    assert "Andy Matuszczak" not in hint
    assert "Apple" not in hint


def test_no_match_returns_empty():
    """本批次无 proper_noun 出现 → 空提示"""
    from pipeline import build_batch_proper_noun_hint
    batch = [{"text": "Some plain content here."}]
    proper_nouns = ["Ben Eater", "Apple"]
    hint = build_batch_proper_noun_hint(batch, proper_nouns)
    assert hint == ""


def test_case_insensitive_match():
    """大小写不敏感 (英文标点边界)"""
    from pipeline import build_batch_proper_noun_hint
    batch = [{"text": "ben eater runs the channel"}]
    hint = build_batch_proper_noun_hint(batch, ["Ben Eater"])
    assert "Ben Eater" in hint


def test_empty_proper_nouns_returns_empty():
    from pipeline import build_batch_proper_noun_hint
    batch = [{"text": "anything"}]
    assert build_batch_proper_noun_hint(batch, []) == ""
    assert build_batch_proper_noun_hint(batch, None) == ""


def test_hint_format_includes_instruction():
    """提示应含'保留'指令文字"""
    from pipeline import build_batch_proper_noun_hint
    batch = [{"text": "Apple is great."}]
    hint = build_batch_proper_noun_hint(batch, ["Apple (苹果)"])
    assert "保留" in hint or "原文" in hint
    assert "Apple" in hint
