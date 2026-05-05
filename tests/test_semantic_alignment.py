#!/usr/bin/env python3
"""
翻译语义对齐验证测试 — 通用方案，无领域专属词典

检测目的:
  1. 跨段内容错位 — 译文 N 实际对应英文 N+1 的内容（LLM 合并句子导致前移）
  2. 批次边界断裂 — 批次末尾/开头出现内容丢失或重复
  3. 关键术语保持 — 英文中的数字/缩写/专有名词应出现在对应译文中

检测方法 (3层信号融合):
  1. 正向锚点: EN 数字/缩写/专有名词 → 检查出现在哪段 ZH
  2. 反向锚点: ZH 中保留的英文单词 → 反向追溯到哪段 EN
  3. 长度比异常: EN-ZH 字符比率偏离局部中位数

设计参考:
  - VideoLingo: segment count matching + JSON key validation
  - Helsinki-NLP/subalign: cognate anchoring
  - Gale-Church: length-ratio alignment
"""
import sys
import os
import re
import json
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 通用句首词排除集 ──
_COMMON_CAPS = {
    'The', 'This', 'That', 'These', 'Those', 'What', 'When', 'Where',
    'Which', 'How', 'And', 'But', 'For', 'Not', 'You', 'They', 'His',
    'Her', 'Its', 'Our', 'Are', 'Can', 'Will', 'Has', 'Have', 'Had',
    'Was', 'Were', 'May', 'Let', 'Now', 'Here', 'There', 'Also', 'Just',
    'Some', 'All', 'Any', 'Each', 'Every', 'Much', 'Many', 'More', 'Most',
    'Such', 'Very', 'Well', 'Then', 'Than', 'Once', 'Only', 'Even',
    'Still', 'About', 'After', 'Before', 'Over', 'Under', 'So', 'If',
}

_TRIVIAL_EN = {'the', 'a', 'an', 'of', 'in', 'to', 'and', 'or', 'is', 'it',
               'at', 'on', 'by', 'as', 'so', 'if', 'no', 'do', 'be', 'we', 'he'}


# ── 工具函数 (通用，无领域词典) ──

def extract_anchor_terms(en_text: str) -> set:
    """从英文段提取通用锚定术语: 数字、大写缩写、专有名词"""
    anchors = set()
    # 数字 (含小数、百分比)
    for m in re.findall(r'\d+(?:\.\d+)?%?', en_text):
        if len(m) >= 2:
            anchors.add(('num', m))
    # 全大写缩写 (>=2字符)
    for m in re.findall(r'\b[A-Z]{2,}\b', en_text):
        anchors.add(('acr', m))
    # 非句首大写词 = 专有名词
    words = en_text.split()
    for k, w in enumerate(words):
        clean = re.sub(r'[^a-zA-Z]', '', w)
        if clean and clean[0].isupper() and k > 0 and len(clean) > 2:
            if clean not in _COMMON_CAPS:
                anchors.add(('name', clean))
    return anchors


def term_present_in_zh(term: tuple, zh_text: str) -> bool:
    """检查锚点是否出现在中文译文中 (通用匹配)"""
    kind, val = term
    if kind == 'num':
        return val in zh_text
    # 缩写和专有名词: 中文常保留英文原词
    return val.lower() in zh_text.lower()


def _zh_char_count(text: str) -> int:
    """统计 CJK 汉字数"""
    return sum(1 for c in text if '\u4e00' <= c <= '\u9fff')


def check_segment_alignment(segments: list) -> list:
    """
    检测跨段错位 — 通用 3 层信号融合。

    返回: [{idx, direction, ...}, ...] direction: +1=前移, -1=后移
    """
    if len(segments) < 3:
        return []

    en_texts = [s.get("text_en", "") for s in segments]
    zh_texts = [s.get("text_zh", "") for s in segments]
    scores = [0.0] * len(segments)

    # ── 信号 1: 正向锚点 ──
    for i in range(len(segments)):
        anchors = extract_anchor_terms(en_texts[i])
        if len(anchors) < 2:
            continue
        hits_self = sum(1 for a in anchors if term_present_in_zh(a, zh_texts[i]))
        for delta in [-1, 1]:
            j = i + delta
            if j < 0 or j >= len(segments):
                continue
            hits_nb = sum(1 for a in anchors if term_present_in_zh(a, zh_texts[j]))
            if hits_self == 0 and hits_nb >= 2:
                scores[i] += 2.0
            elif hits_nb >= hits_self + 2 and hits_nb >= 3:
                scores[i] += 1.5

    # ── 信号 2: 反向锚点 (ZH 中保留的英文) ──
    for i, zh in enumerate(zh_texts):
        preserved = {m.lower() for m in re.findall(r'[A-Za-z]{2,}', zh)
                     if m.lower() not in _TRIVIAL_EN}
        if not preserved:
            continue
        en_lower = en_texts[i].lower()
        hits_self = sum(1 for w in preserved if w in en_lower)
        for delta in [-1, 1]:
            j = i + delta
            if 0 <= j < len(en_texts):
                hits_nb = sum(1 for w in preserved if w in en_texts[j].lower())
                if hits_nb > hits_self and hits_nb >= 2:
                    scores[i] += 1.5

    # ── 信号 3: 长度比异常 ──
    ratios = [_zh_char_count(zh) / max(len(en), 1)
              for en, zh in zip(en_texts, zh_texts)]
    for i in range(len(ratios)):
        window = sorted(ratios[max(0, i - 2):min(len(ratios), i + 3)])
        if len(window) >= 3:
            median = window[len(window) // 2]
            if median > 0.05:
                if ratios[i] > median * 3.0 or ratios[i] < median * 0.2:
                    scores[i] += 1.0

    # 收集详细报告
    misaligned = []
    for i, sc in enumerate(scores):
        if sc >= 1.5:
            # 找到主要方向
            for delta in [-1, 1]:
                j = i + delta
                if 0 <= j < len(segments):
                    misaligned.append({
                        "idx": i,
                        "direction": delta,
                        "score": sc,
                        "hits_self": 0,
                        "hits_neighbor": 0,
                        "en_text": en_texts[i][:60],
                        "zh_self": zh_texts[i][:40],
                        "zh_neighbor": zh_texts[j][:40] if 0 <= j < len(zh_texts) else "",
                    })
                    break
    return misaligned


# ── 测试用例 ──

def test_no_cross_segment_misalignment():
    """segments_cache.json 中不应有跨段内容错位"""
    cache_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "output", "zjMuIxRvygQ", "segments_cache.json"
    )
    if not os.path.exists(cache_path):
        print("  ⏭  跳过: segments_cache.json 不存在")
        return

    with open(cache_path, encoding="utf-8") as f:
        segments = json.load(f)

    misaligned = check_segment_alignment(segments)
    # 容许少量误报（Whisper 句中切分导致锚定词在邻段自然出现）
    MAX_TOLERABLE = 5
    if misaligned:
        print(f"  ⚠️  发现 {len(misaligned)} 段疑似错位 (容许 ≤{MAX_TOLERABLE}):")
        for m in misaligned:
            dir_str = "前移→" if m["direction"] == 1 else "←后移"
            print(f"     #{m['idx']} {dir_str} (score={m['score']:.1f}): "
                  f"{m['en_text'][:50]}")
            print(f"       ZH(自): {m['zh_self']}")
            print(f"       ZH(邻): {m['zh_neighbor']}")
    else:
        print("  ✅ 无跨段错位")
    assert len(misaligned) <= MAX_TOLERABLE, \
        f"发现 {len(misaligned)} 段跨段错位 (超过容许阈值 {MAX_TOLERABLE})"
    if misaligned:
        print(f"  ✅ 错位数 {len(misaligned)} ≤ {MAX_TOLERABLE}，在容许范围内")


def test_segment_count_preserved():
    """翻译后段数应与转录段数一致"""
    base = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "output", "zjMuIxRvygQ"
    )
    cache = os.path.join(base, "segments_cache.json")
    tc = os.path.join(base, "transcribe_cache.json")
    if not os.path.exists(cache) or not os.path.exists(tc):
        print("  ⏭  跳过: 缓存文件不存在")
        return
    with open(cache) as f:
        segs = json.load(f)
    with open(tc) as f:
        trans = json.load(f)
    print(f"  转录段数: {len(trans)}, 翻译段数: {len(segs)}")
    ratio = len(segs) / len(trans) if len(trans) > 0 else 0
    # Sentence-Unit 流水线: 多个 Whisper 碎片合并到 1 unit, 比率允许 0.30-1.05
    assert 0.30 <= ratio <= 1.05, f"段数比 {ratio:.2f} 异常 (期望 0.30~1.05)"
    print("  ✅ 段数比合理")


def test_no_hallucination():
    """同一译文不应出现 ≥3 次"""
    cache_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "output", "zjMuIxRvygQ", "segments_cache.json"
    )
    if not os.path.exists(cache_path):
        print("  ⏭  跳过")
        return
    with open(cache_path) as f:
        segs = json.load(f)
    texts = [s.get("text_zh", "") for s in segs if s.get("text_zh", "").strip()]
    counts = Counter(texts)
    hall = {t: c for t, c in counts.items() if c >= 3}
    if hall:
        print(f"  ❌ 发现幻觉:")
        for t, c in hall.items():
            print(f"    '{t[:40]}' × {c}")
    else:
        print("  ✅ 无幻觉")
    assert not hall, f"发现 {len(hall)} 种幻觉重复"


def test_coverage():
    """翻译覆盖率应 ≥ 98%"""
    cache_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "output", "zjMuIxRvygQ", "segments_cache.json"
    )
    if not os.path.exists(cache_path):
        print("  ⏭  跳过")
        return
    with open(cache_path) as f:
        segs = json.load(f)
    covered = sum(1 for s in segs if len(s.get("text_zh", "").strip()) >= 2)
    rate = covered / len(segs)
    print(f"  覆盖率: {covered}/{len(segs)} = {rate:.1%}")
    assert rate >= 0.98, f"覆盖率 {rate:.1%} < 98%"
    print("  ✅ 覆盖率达标")


def test_parse_numbered_alignment():
    """_parse_numbered_translations 应按编号对齐，而非仅按行序"""
    from pipeline import _parse_numbered_translations

    content = "[1] 你好\n[2] 世界\n[3] 测试"
    result = _parse_numbered_translations(content, 3)
    assert result == ["你好", "世界", "测试"], f"正常解析失败: {result}"

    # 编号跳跃: slot 0 = "你好", slot 1 = "" (空), slot 2 = "测试"
    content_skip = "[1] 你好\n[3] 测试"
    result_skip = _parse_numbered_translations(content_skip, 3)
    assert result_skip[0] == "你好", f"slot 0 应为 '你好': {result_skip}"
    assert result_skip[1] == "", f"slot 1 应为空 (跳号): {result_skip}"
    assert result_skip[2] == "测试", f"slot 2 应为 '测试': {result_skip}"
    print(f"  编号跳跃修复后: {result_skip}")
    print("  ✅ test_parse_numbered_alignment (编号验证通过)")


def test_anchor_extraction():
    """验证通用锚定术语提取 — 不依赖领域词典"""
    # 数学/3D 场景 — 只有单数字 "3", 无缩写、无专有名词 → 无强锚点 (正确)
    a1 = extract_anchor_terms(
        "For example, any of you familiar with linear algebra will know that 3x3 matrices"
    )
    # 单个数字太模糊，通用方案不提取 (这不是 bug，是设计决策)
    assert len(a1) == 0, f"单数字不应作为锚点: {a1}"

    # 有多位数字的数学场景
    a1b = extract_anchor_terms(
        "The 3x3 rotation matrix requires 9 values, but a quaternion only needs 4 real numbers plus 30 degrees"
    )
    assert any(k == 'num' for k, v in a1b), f"多位数字应提取: {a1b}"

    # 科技新闻场景
    a2 = extract_anchor_terms(
        "The company Google announced that their new GPU chip can handle 100 billion parameters"
    )
    assert ('name', 'Google') in a2, f"应提取 Google: {a2}"
    assert ('acr', 'GPU') in a2, f"应提取 GPU: {a2}"
    assert ('num', '100') in a2, f"应提取 100: {a2}"

    # 烹饪场景
    a3 = extract_anchor_terms(
        "Preheat the oven to 350 degrees Fahrenheit as Gordon Ramsay demonstrates"
    )
    assert ('num', '350') in a3, f"应提取 350: {a3}"
    assert ('name', 'Gordon') in a3 or ('name', 'Ramsay') in a3, f"应提取人名: {a3}"

    # 无锚点场景 (纯一般性描述)
    a4 = extract_anchor_terms("and this is why it matters so much for all of us")
    assert len(a4) == 0, f"一般性描述不应有锚点: {a4}"

    print(f"  数学: {a1}")
    print(f"  科技: {a2}")
    print(f"  烹饪: {a3}")
    print(f"  无锚: {a4}")
    print("  ✅ test_anchor_extraction (通用提取，无领域词典)")


def test_known_misalignment_detection():
    """用构造的错位样本验证检测逻辑 — 通用场景"""
    # 场景 1: 科技视频，段内容前移
    segments_tech = [
        {"text_en": "Google has invested over 500 million dollars into this GPU research project.",
         "text_zh": "下面来看看新款芯片在自然语言处理上的表现"},  # 错: 这是下一段的内容
        {"text_en": "The new chip shows remarkable performance in NLP benchmarks.",
         "text_zh": "Google已在这个GPU研发项目上投入超过5亿美元"},  # 错: 这是上一段的内容
        {"text_en": "Researchers at Stanford confirmed the results independently.",
         "text_zh": "Stanford的研究人员独立确认了这些结果"},  # 正确
    ]
    m1 = check_segment_alignment(segments_tech)
    assert len(m1) >= 1, f"科技场景应检测到错位: {m1}"
    print(f"  科技场景: 检测到 {len(m1)} 段错位 ✅")

    # 场景 2: 正确对齐不应误报
    segments_ok = [
        {"text_en": "NASA launched the Artemis 3 mission in 2025.",
         "text_zh": "NASA在2025年发射了Artemis 3号任务。"},
        {"text_en": "The crew consisted of 4 astronauts from different countries.",
         "text_zh": "机组由来自不同国家的4名宇航员组成。"},
        {"text_en": "They spent 14 days on the lunar surface.",
         "text_zh": "他们在月球表面停留了14天。"},
    ]
    m2 = check_segment_alignment(segments_ok)
    assert len(m2) == 0, f"正确对齐不应误报: {m2}"
    print(f"  正确对齐: 无误报 ✅")

    print("  ✅ test_known_misalignment_detection (通用场景)")


def test_correct_alignment_passes():
    """多种主题的正确翻译不应误报"""
    # 混合主题: 烹饪
    segments = [
        {"text_en": "Chef Ramsay adds 200 grams of flour to the bowl.",
         "text_zh": "Ramsay主厨往碗里加了200克面粉。"},
        {"text_en": "Then he mixes in 3 eggs and 50 milliliters of milk.",
         "text_zh": "然后他加入3个鸡蛋和50毫升牛奶搅拌。"},
        {"text_en": "The mixture should rest for about 30 minutes.",
         "text_zh": "面糊需要静置大约30分钟。"},
    ]
    misaligned = check_segment_alignment(segments)
    assert len(misaligned) == 0, f"正确对齐误报为错位: {misaligned}"
    print("  ✅ test_correct_alignment_passes (多主题无误报)")


if __name__ == "__main__":
    print("=" * 50)
    print("翻译语义对齐验证测试 (通用方案)")
    print("=" * 50)

    print("\n[1] 锚点提取 (通用):")
    test_anchor_extraction()

    print("\n[2] 已知错位检测 (通用场景):")
    test_known_misalignment_detection()

    print("\n[3] 正确对齐无误报:")
    test_correct_alignment_passes()

    print("\n[4] 编号解析对齐:")
    test_parse_numbered_alignment()

    # 集成测试（依赖 segments_cache.json）
    print("\n[5] 幻觉检测:")
    test_no_hallucination()

    print("\n[6] 覆盖率:")
    test_coverage()

    print("\n[7] 段数一致性:")
    test_segment_count_preserved()

    print("\n[8] 跨段错位检测:")
    test_no_cross_segment_misalignment()

    print("\n" + "=" * 50)
    print("全部通过")
