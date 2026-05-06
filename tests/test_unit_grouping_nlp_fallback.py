"""测试: group_segments_to_units 超长 unit 的 NLP 切分回退。

问题背景:
  Whisper 英文转录常缺标点 (尤其口语视频), 导致 _split_long_unit_by_clause
  找不到 ',;:' 类子句标点, 超长 unit 不被切, 平均段长可达 90+ 秒。
  这超出 edge-tts 的安全段长边界 (~30s), 也让 LLM 翻译跨段污染概率飙高。

回退策略:
  1. 优先用现有标点切 (_split_long_unit_by_clause)
  2. 切完后仍存在 dur > max_unit_duration 的段 → 用 spaCy NLP 按句子边界再切
  3. NLP 仍切不动 (单句过长) → 用 word-level 时间戳按时间均匀切

预期: 任意 unit 时长 ≤ max_unit_duration * 1.5 (留 50% 容忍)
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import group_segments_to_units


def _seg(start, end, text, words=None):
    s = {"start": start, "end": end, "text": text}
    if words is not None:
        s["words"] = words
    return s


def _make_words(start, end, text):
    """根据 text 均匀分配 word timestamps (模拟 Whisper 输出)。"""
    toks = text.split()
    if not toks:
        return []
    dur = end - start
    per = dur / len(toks)
    return [
        {"word": t, "start": start + i * per, "end": start + (i + 1) * per}
        for i, t in enumerate(toks)
    ]


# ───────────────────────────────────────────────────────────────────
# Case A: 单段超长无标点 → 必须切到 ≤ max
# ───────────────────────────────────────────────────────────────────
def test_long_unit_no_punctuation_split_via_nlp():
    """100s 单段无任何标点, 但语义上有多句 → NLP 应切 ≥ 2 段"""
    text = (
        "so in this section I want to walk through the basic ideas "
        "of how a transformer architecture actually processes tokens "
        "first we need to understand the role of attention in the model "
        "then we'll look at how the residual connections help training "
        "and finally we'll talk about why layer normalization matters here"
    )
    words = _make_words(0.0, 100.0, text)
    segs = [_seg(0.0, 100.0, text, words=words)]
    units = group_segments_to_units(segs, max_unit_duration=12.0)

    # 切分必须发生
    assert len(units) >= 2, (
        f"100s 无标点 unit 应被切分, got {len(units)}: "
        f"{[(round(u['end']-u['start'], 1), u['text'][:30]) for u in units]}"
    )
    # 任意 unit 时长不应超 max * 1.5
    for u in units:
        dur = u["end"] - u["start"]
        assert dur <= 12.0 * 1.5, f"unit 仍超长: {dur:.1f}s, text={u['text'][:50]}"


# ───────────────────────────────────────────────────────────────────
# Case B: 标点切优先, NLP 不抢戏
# ───────────────────────────────────────────────────────────────────
def test_long_unit_with_clause_punctuation_uses_clause_split():
    """有 ',' 标点的超长 unit → 仍用现有标点切, 不调用 NLP"""
    text = (
        "first we'll cover attention, then we'll cover residual connections, "
        "then we'll cover layer norm, then we'll cover the training loop, "
        "and finally we'll wrap up with the inference path"
    )
    words = _make_words(0.0, 30.0, text)
    segs = [_seg(0.0, 30.0, text, words=words)]
    units = group_segments_to_units(segs, max_unit_duration=12.0)

    assert len(units) >= 2
    for u in units:
        dur = u["end"] - u["start"]
        assert dur <= 12.0 * 1.5, f"unit 仍超长: {dur:.1f}s"


# ───────────────────────────────────────────────────────────────────
# Case C: NLP 切完仍存在单句超长 → word-level 时间均切兜底
# ───────────────────────────────────────────────────────────────────
def test_extremely_long_single_sentence_falls_back_to_time_split():
    """50s 单句无标点 (NLP 也只识别一句) → 按 word 时间均切"""
    text = (
        "and so what we end up doing is we just take the input and we run it "
        "through the network and we get the output and then we compare it to "
        "the target and we compute the loss and we backprop through everything"
    )
    words = _make_words(0.0, 50.0, text)
    segs = [_seg(0.0, 50.0, text, words=words)]
    units = group_segments_to_units(segs, max_unit_duration=12.0)

    assert len(units) >= 3, (
        f"50s 单句应被切到 ≥3 段, got {len(units)}: "
        f"{[(round(u['end']-u['start'], 1)) for u in units]}"
    )
    for u in units:
        dur = u["end"] - u["start"]
        assert dur <= 12.0 * 1.5, f"unit 仍超长: {dur:.1f}s"


# ───────────────────────────────────────────────────────────────────
# Case D: 短 unit 不被误切
# ───────────────────────────────────────────────────────────────────
def test_short_unit_not_split():
    """正常长度 unit (<= max) 不应被切"""
    segs = [
        _seg(0.0, 5.0, "This is a short sentence.",
             words=_make_words(0.0, 5.0, "This is a short sentence."))
    ]
    units = group_segments_to_units(segs, max_unit_duration=12.0)
    assert len(units) == 1
    assert units[0]["text"] == "This is a short sentence."


# ───────────────────────────────────────────────────────────────────
# Case E: 缺 word timestamps 时优雅降级 (不崩溃)
# ───────────────────────────────────────────────────────────────────
def test_long_unit_no_words_does_not_crash():
    """无 word timestamps 的超长 unit → 至少不崩溃, 返回原 unit 或按字符均切"""
    text = "first some background then attention then residual then layernorm then training"
    segs = [_seg(0.0, 50.0, text)]  # no words
    units = group_segments_to_units(segs, max_unit_duration=12.0)
    # 不强制必须切 (无 words 无法精确分配时间), 但不应崩
    assert len(units) >= 1
    assert all(u["text"].strip() for u in units)


# ───────────────────────────────────────────────────────────────────
# Case F: 配置开关: nlp_segmentation_fallback=False 时禁用 NLP 兜底
# ───────────────────────────────────────────────────────────────────
def test_nlp_fallback_can_be_disabled():
    """config 显式禁用 NLP 兜底时, 行为退回旧逻辑 (无标点不切)"""
    text = (
        "so in this section I want to walk through the basic ideas "
        "of how a transformer architecture actually processes tokens "
        "first we need to understand the role of attention in the model"
    )
    words = _make_words(0.0, 60.0, text)
    segs = [_seg(0.0, 60.0, text, words=words)]
    cfg = {"unit_grouping": {"max_duration": 12.0,
                             "nlp_segmentation_fallback": False}}
    units = group_segments_to_units(segs, config=cfg)
    # 禁用兜底 → 老行为, 60s 无标点 unit 不被切
    assert len(units) == 1, f"禁用 fallback 时应保留单段, got {len(units)}"
