#!/usr/bin/env python3
"""Phase 2: 全文翻译质量优化

Phase 1（pipeline.py）优化目标是 TTS 时长适配，给出每段中文字数的 ground truth。
Phase 2 优化目标是翻译质量：全文连贯翻译 → 多候选 → DP 按 budget 切分 → 选最优。

用法:
    python phase2_translate.py output/zjMuIxRvygQ
    python phase2_translate.py output/zjMuIxRvygQ --candidates 5 --config config.json
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx
import jieba

# ── 共享工具函数 ──
sys.path.insert(0, str(Path(__file__).resolve().parent))
from text_utils import normalize_llm_output, text_for_duration
from duration_estimator import estimate_duration as _estimate_duration_jieba


# ─────────────────────────────────────────────────────────
# Budget 提取
# ─────────────────────────────────────────────────────────

def count_hanzi(text: str) -> int:
    """统计文本中的汉字数量"""
    return sum(1 for c in text if '\u4e00' <= c <= '\u9fff')


def extract_budgets(segments: list) -> list[int]:
    """从 Phase 1 的 text_zh 提取每段汉字数作为 budget"""
    return [count_hanzi(text_for_duration(seg["text_zh"])) for seg in segments]


def extract_budgets_jieba(segments: list) -> list[int]:
    """用 jieba 时长估算器计算 budget（时长校准版）。

    原理：Phase 1 text_zh 的 jieba 估算时长反映真实 TTS 节奏，
    将时长按全局汉字/ms 比率换算回汉字数，作为 DP 的 budget。
    相同字数但含英文/数字/URL 的段会获得更大 budget（因为 TTS 读得慢）。
    """
    durations = [_estimate_duration_jieba(text_for_duration(seg["text_zh"])) for seg in segments]
    hanzi_counts = [count_hanzi(text_for_duration(seg["text_zh"])) for seg in segments]
    total_hanzi = sum(hanzi_counts)
    total_dur = sum(durations)
    if total_dur <= 0 or total_hanzi <= 0:
        return hanzi_counts  # fallback
    # 全局 ms/hanzi 比率
    ms_per_hanzi = total_dur / total_hanzi
    return [max(1, round(dur / ms_per_hanzi)) for dur in durations]


# ─────────────────────────────────────────────────────────
# LLM API 调用
# ─────────────────────────────────────────────────────────

def call_llm(
    endpoint: str, headers: dict, model: str,
    system_prompt: str, user_prompt: str,
    temperature: float = 0.3, max_tokens: int = 8192,
) -> str:
    """调用 OpenAI 兼容 API，返回助手回复文本"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    with httpx.Client(timeout=180.0) as client:
        resp = client.post(endpoint, json=payload, headers=headers)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return normalize_llm_output(content)


CANDIDATE_CONFIGS = [
    {"temperature": 0.3, "style": ""},
    {"temperature": 0.5, "style": ""},
    {"temperature": 0.7, "style": ""},
    {"temperature": 0.3, "style": "请使用口语化配音风格，短句为主。"},
    {"temperature": 0.5, "style": "请用精炼简洁的表达，信息密度高。"},
]

BASE_SYSTEM_PROMPT = (
    "你是专业的英中视频翻译专家。请将以下英文视频内容完整翻译为自然流畅的中文。\n"
    "要求：\n"
    "1) 完整保留原文全部信息，不遗漏、不添加\n"
    "2) 译文通顺自然，符合中文表达习惯，适合配音朗读\n"
    "3) 保持原文的语气和情感\n"
    "4) 计算机/数学术语保留通用英文缩写\n"
    "5) 只输出翻译结果，不要解释"
)


def generate_candidates(
    full_english: str, endpoint: str, headers: dict, model: str,
    n_candidates: int = 5, max_tokens: int = 8192,
    total_budget: int = 0, style_guide: str = "",
) -> list[str]:
    """生成多个全文翻译候选"""
    candidates = []
    configs = CANDIDATE_CONFIGS[:n_candidates]

    budget_hint = f"\n（译文总字数请控制在 {total_budget} 字左右）" if total_budget > 0 else ""

    for i, cfg in enumerate(configs):
        sys_prompt = BASE_SYSTEM_PROMPT
        if style_guide:
            sys_prompt += f"\n{style_guide}"
        if cfg["style"]:
            sys_prompt += f"\n\n附加风格要求：{cfg['style']}"

        print(f"  候选 {i+1}/{len(configs)} (T={cfg['temperature']}"
              f"{', ' + cfg['style'][:10] + '...' if cfg['style'] else ''})...",
              end="", flush=True)
        t0 = time.time()
        try:
            text = call_llm(
                endpoint, headers, model,
                sys_prompt, f"请翻译以下英文视频内容：{budget_hint}\n\n{full_english}",
                temperature=cfg["temperature"],
                max_tokens=max_tokens,
            )
            elapsed = time.time() - t0
            hanzi = count_hanzi(text)
            print(f" {hanzi}字, {elapsed:.1f}s")
            candidates.append(text)
        except Exception as e:
            elapsed = time.time() - t0
            print(f" 失败 ({e}), {elapsed:.1f}s")

    return candidates


# ─────────────────────────────────────────────────────────
# DP 切分算法
# ─────────────────────────────────────────────────────────

# 切分点优先级
PRIORITY_SENTENCE_END = 1   # 。！？
PRIORITY_CLAUSE = 2         # ，；：、
PRIORITY_WORD = 3           # 普通词边界
PRIORITY_BAD_START = 4      # 下一词是虚词（的/地/了等），不宜作句首

SPLIT_PENALTY = {
    PRIORITY_SENTENCE_END: 0,
    PRIORITY_CLAUSE: 1,
    PRIORITY_WORD: 4,
    PRIORITY_BAD_START: 20,
}

# 不宜出现在句首的虚词/助词/连词
_BAD_START_WORDS = frozenset("的地了着过得是和与而且也都就又")


def find_split_points(text_zh: str) -> list[tuple[int, int]]:
    """识别中文文本中的候选切分点。

    返回 [(char_position, priority), ...] 按位置排序。
    char_position 是切分点右侧的字符索引（即该位置之前为前一段）。
    """
    words = jieba.lcut(text_zh)
    points = []
    pos = 0

    for i, word in enumerate(words):
        pos += len(word)
        # 不在开头或末尾切分
        if pos <= 0 or pos >= len(text_zh):
            continue

        # 根据词尾字符判断优先级
        last_char = word[-1] if word else ""
        if last_char in "。！？":
            priority = PRIORITY_SENTENCE_END
        elif last_char in "，；：、":
            priority = PRIORITY_CLAUSE
        else:
            priority = PRIORITY_WORD

        # 前瞻：如果下一词首字是虚词，不宜在此切分
        if i + 1 < len(words):
            next_first = words[i + 1][0] if words[i + 1] else ""
            if next_first in _BAD_START_WORDS and priority == PRIORITY_WORD:
                priority = PRIORITY_BAD_START

        points.append((pos, priority))

    return points


def split_text_by_budgets(text_zh: str, budgets: list[int]) -> list[str]:
    """DP 算法：将连续中文文本按 budget 数组最优切分。

    Args:
        text_zh: 连续的中文翻译全文
        budgets: 每段的目标汉字数数组，长度 N

    Returns:
        N 段文本列表
    """
    n_segs = len(budgets)
    text_len = len(text_zh)

    if n_segs == 0:
        return []
    if n_segs == 1:
        return [text_zh]

    # 预计算：每个位置之前的累计汉字数
    cum_hanzi = [0] * (text_len + 1)
    for i, c in enumerate(text_zh):
        cum_hanzi[i + 1] = cum_hanzi[i] + (1 if '\u4e00' <= c <= '\u9fff' else 0)

    def hanzi_in_range(start: int, end: int) -> int:
        return cum_hanzi[end] - cum_hanzi[start]

    # 预计算：累计 budget，用于估算每段的目标位置
    cum_budget = [0] * (n_segs + 1)
    for i in range(n_segs):
        cum_budget[i + 1] = cum_budget[i] + budgets[i]
    total_budget = cum_budget[n_segs]
    total_hanzi = cum_hanzi[text_len]

    # 文本/budget 比率，用于将 budget 映射到字符位置
    ratio = text_len / total_hanzi if total_hanzi > 0 else 1.0

    # 获取所有切分点
    split_points = find_split_points(text_zh)
    sp_positions = [0] + [p for p, _ in split_points] + [text_len]
    sp_priority = {p: pri for p, pri in split_points}
    sp_positions = sorted(set(sp_positions))

    # DP: dp[i] = {pos: (min_cost, parent_pos)}
    # i = 已分配的段数，pos = 已使用的字符位置
    INF = float('inf')

    # 初始化：0 段使用 0 个字符
    prev_layer = {0: (0.0, -1)}

    for seg_idx in range(n_segs):
        curr_layer = {}
        target_hanzi = budgets[seg_idx]

        # 估算这段结束的目标字符位置
        target_cum_hanzi = cum_budget[seg_idx + 1]
        target_pos = int(target_cum_hanzi * ratio)

        # 搜索窗口
        window = max(target_hanzi * 3, 80)
        min_end = max(0, target_pos - window)
        max_end = min(text_len, target_pos + window)

        # 最后一段必须到达文本末尾
        if seg_idx == n_segs - 1:
            min_end = text_len
            max_end = text_len

        for start_pos, (start_cost, _) in prev_layer.items():
            # 找 start_pos 之后、在窗口内的切分点
            for end_pos in sp_positions:
                if end_pos <= start_pos:
                    continue
                if end_pos < min_end:
                    continue
                if end_pos > max_end:
                    break

                seg_hanzi = hanzi_in_range(start_pos, end_pos)
                deviation = seg_hanzi - target_hanzi
                cost = deviation * deviation

                # 切分点惩罚（最后一段到末尾无惩罚）
                if end_pos < text_len:
                    priority = sp_priority.get(end_pos, PRIORITY_WORD)
                    cost += SPLIT_PENALTY.get(priority, 4)

                # 空段大惩罚
                if seg_hanzi == 0:
                    cost += 1000

                total_cost = start_cost + cost
                if end_pos not in curr_layer or total_cost < curr_layer[end_pos][0]:
                    curr_layer[end_pos] = (total_cost, start_pos)

        if not curr_layer:
            # 回退：如果窗口太窄找不到解，放宽到全范围
            for start_pos, (start_cost, _) in prev_layer.items():
                end_pos = text_len if seg_idx == n_segs - 1 else min(
                    text_len, start_pos + int(target_hanzi * ratio) + 50)
                # 找最近的切分点
                best_sp = end_pos
                best_dist = INF
                for sp in sp_positions:
                    if sp <= start_pos:
                        continue
                    dist = abs(sp - end_pos)
                    if dist < best_dist:
                        best_dist = dist
                        best_sp = sp
                end_pos = best_sp

                seg_hanzi = hanzi_in_range(start_pos, end_pos)
                cost = (seg_hanzi - target_hanzi) ** 2
                total_cost = start_cost + cost
                if end_pos not in curr_layer or total_cost < curr_layer[end_pos][0]:
                    curr_layer[end_pos] = (total_cost, start_pos)

        prev_layer = curr_layer

    # 回溯：从 text_len 开始
    if text_len not in prev_layer:
        # 找最接近末尾的有效状态
        best_pos = max(prev_layer.keys()) if prev_layer else 0
        # 强制用这个状态 + 把剩余文本放最后一段
        segments_result = []
        positions = [best_pos]
        pos = best_pos
        for _ in range(n_segs - 1):
            parent = prev_layer.get(pos, (0, 0))[1]
            positions.append(parent)
            pos = parent
        positions.reverse()
        for k in range(len(positions) - 1):
            segments_result.append(text_zh[positions[k]:positions[k+1]])
        # 追加剩余
        segments_result.append(text_zh[best_pos:])
        while len(segments_result) < n_segs:
            segments_result.append("")
        return segments_result[:n_segs]

    # 正常回溯
    positions = [text_len]
    pos = text_len
    for _ in range(n_segs):
        parent = prev_layer.get(pos, (0, 0))[1]
        if parent == -1:
            parent = 0
        positions.append(parent)
        # 切换到上一层
        pos = parent
        # 需要逆向遍历层，这里用简化方式

    # 重新正向 DP 回溯（更可靠）
    # 重跑 DP 保存完整路径
    layers = [{0: (0.0, -1)}]
    for seg_idx in range(n_segs):
        curr_layer = {}
        target_hanzi = budgets[seg_idx]
        target_cum_hanzi = cum_budget[seg_idx + 1]
        target_pos = int(target_cum_hanzi * ratio)
        window = max(target_hanzi * 3, 80)
        min_end = max(0, target_pos - window)
        max_end = min(text_len, target_pos + window)
        if seg_idx == n_segs - 1:
            min_end = text_len
            max_end = text_len

        for start_pos, (start_cost, _) in layers[-1].items():
            for end_pos in sp_positions:
                if end_pos <= start_pos:
                    continue
                if end_pos < min_end:
                    continue
                if end_pos > max_end:
                    break
                seg_hanzi = hanzi_in_range(start_pos, end_pos)
                deviation = seg_hanzi - target_hanzi
                cost = deviation * deviation
                if end_pos < text_len:
                    priority = sp_priority.get(end_pos, PRIORITY_WORD)
                    cost += SPLIT_PENALTY.get(priority, 4)
                if seg_hanzi == 0:
                    cost += 1000
                total_cost = start_cost + cost
                if end_pos not in curr_layer or total_cost < curr_layer[end_pos][0]:
                    curr_layer[end_pos] = (total_cost, start_pos)

        if not curr_layer:
            # 回退
            for start_pos, (start_cost, _) in layers[-1].items():
                end_pos = text_len if seg_idx == n_segs - 1 else min(
                    text_len, start_pos + max(20, int(target_hanzi * ratio)))
                best_sp = end_pos
                for sp in sp_positions:
                    if sp > start_pos and abs(sp - end_pos) < abs(best_sp - end_pos):
                        best_sp = sp
                seg_hanzi = hanzi_in_range(start_pos, best_sp)
                cost = (seg_hanzi - target_hanzi) ** 2
                if best_sp not in curr_layer or start_cost + cost < curr_layer[best_sp][0]:
                    curr_layer[best_sp] = (start_cost + cost, start_pos)

        layers.append(curr_layer)

    # 从末尾回溯
    result_positions = [text_len]
    pos = text_len
    for layer_idx in range(n_segs, 0, -1):
        layer = layers[layer_idx]
        if pos in layer:
            parent = layer[pos][1]
        else:
            # 找最近的
            parent = 0
        result_positions.append(parent)
        pos = parent

    result_positions.reverse()

    # 生成段文本
    segments_result = []
    for k in range(len(result_positions) - 1):
        seg_text = text_zh[result_positions[k]:result_positions[k+1]]
        segments_result.append(seg_text)

    while len(segments_result) < n_segs:
        segments_result.append("")

    return segments_result[:n_segs]


# ─────────────────────────────────────────────────────────
# 候选评分
# ─────────────────────────────────────────────────────────

def score_candidate(segments_zh: list[str], budgets: list[int]) -> dict:
    """评价一个切分候选的质量"""
    deviations = []
    empty = 0
    for seg_text, budget in zip(segments_zh, budgets):
        actual = count_hanzi(seg_text)
        dev = actual - budget
        deviations.append(dev)
        if actual < 2:
            empty += 1

    abs_devs = [abs(d) for d in deviations]
    mse = sum(d * d for d in deviations) / len(deviations) if deviations else 0
    mae = sum(abs_devs) / len(abs_devs) if abs_devs else 0
    max_dev = max(abs_devs) if abs_devs else 0
    total = mse + 10 * empty + 0.5 * max_dev * max_dev
    return {
        "mse": round(mse, 2),
        "mae": round(mae, 2),
        "max_dev": max_dev,
        "empty": empty,
        "total": round(total, 2),
        "deviations": deviations,
    }


# ─────────────────────────────────────────────────────────
# Sentence Embedding (lazy)
# ─────────────────────────────────────────────────────────

_embedding_model = None
_embedding_available = None


def _load_embedding_model():
    """懒加载 sentence embedding 模型 (paraphrase-multilingual-MiniLM-L12-v2)"""
    global _embedding_model, _embedding_available
    if _embedding_available is not None:
        return _embedding_available
    try:
        import os
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer(
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        )
        _embedding_available = True
        print("  ✅ sentence embedding 模型已加载")
    except Exception as e:
        print(f"  ⚠️  sentence embedding 不可用 ({e})")
        _embedding_available = False
    return _embedding_available


def compute_alignment_score(
    en_segments: list[str], zh_segments: list[str],
) -> tuple[float, list[float]]:
    """计算英中逐段 cosine similarity。

    返回 (平均 sim, 每段 sim)。embedding 不可用时返回 (0.5, [])。
    """
    if not _load_embedding_model():
        return 0.5, []
    import numpy as np
    en_embs = _embedding_model.encode(en_segments, normalize_embeddings=True)
    zh_embs = _embedding_model.encode(zh_segments, normalize_embeddings=True)
    sims = [float(np.dot(e, z)) for e, z in zip(en_embs, zh_embs)]
    avg = sum(sims) / len(sims) if sims else 0.0
    return avg, sims


# ─────────────────────────────────────────────────────────
# 量化质量指标
# ─────────────────────────────────────────────────────────

def compute_repetition_score(zh_text: str) -> float:
    """字符级 n-gram 重复率 + 句子级近重复检测 (0.0 = 无重复, 1.0 = 全重复)。

    层级 1: 3-gram 和 4-gram 检测, 出现 >=3 次视为重复 (捕捉幻觉式循环)。
    层级 2: 句子级近重复 — 按中文句末标点分句, 检测 Jaccard > 0.7 的近重复对 (捕捉整句重复)。
    返回两者中更高的重复率。
    """
    from collections import Counter
    chars = [c for c in zh_text if '\u4e00' <= c <= '\u9fff' or c.isalnum()]
    if len(chars) < 10:
        return 0.0
    worst = 0.0

    # 层级 1: character n-gram
    for n in (3, 4):
        if len(chars) < n:
            continue
        ngrams = [tuple(chars[i:i+n]) for i in range(len(chars) - n + 1)]
        total = len(ngrams)
        counts = Counter(ngrams)
        repeated = sum(cnt for cnt in counts.values() if cnt >= 3)
        ratio = repeated / total if total > 0 else 0.0
        worst = max(worst, ratio)

    # 层级 2: sentence-level near-duplicate
    sentences = re.split(r'[。！？；\n]+', zh_text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 10]
    if len(sentences) >= 2:
        dup_chars = 0
        total_chars = sum(len(s) for s in sentences)
        seen = []
        for s in sentences:
            s_set = set(s)
            for prev, prev_set in seen:
                if len(s) < 10:
                    continue
                intersection = len(s_set & prev_set)
                union = len(s_set | prev_set)
                if union > 0 and intersection / union > 0.6:
                    dup_chars += len(s)
                    break
            seen.append((s, s_set))
        if total_chars > 0:
            sent_ratio = dup_chars / total_chars
            worst = max(worst, sent_ratio)

    return worst


def compute_source_coverage(
    en_text: str, zh_text: str,
    term_dict: dict[str, str] | None = None,
) -> tuple[float, int]:
    """检查英文中的关键标识符是否保留在中文译文中。

    提取: 数字 (>=2字符) + 大写缩写 + 含数字的术语 (3D/3x3) + URL片段。
    若提供 term_dict (英→中), 额外检查双语术语覆盖:
      匹配英文原词 OR 中文翻译均视为覆盖。
    返回 (覆盖率 0.0-1.0, token数)。无 token 时返回 (1.0, 0)。
    """
    tokens = set()
    # 数字 (>=2 字符)
    for m in re.findall(r'\d+(?:\.\d+)?%?', en_text):
        if len(m) >= 2:
            tokens.add(m)
    # 大写缩写 (>=2 字母)
    for m in re.findall(r'\b[A-Z]{2,}\b', en_text):
        tokens.add(m)
    # 含数字的术语: 3D, 3x3
    for m in re.findall(r'\b\d+[A-Za-z]+\b', en_text):
        tokens.add(m)
    for m in re.findall(r'\b\d+x\d+\b', en_text):
        tokens.add(m)
    # URL 片段: xxx.yyy/zzz
    for m in re.findall(r'[a-z]+\.[a-z]+(?:/[a-z]+)*', en_text, re.IGNORECASE):
        tokens.add(m.lower())

    # 双语术语覆盖 (从 LLM guide 的 term_rules 构建)
    bilingual_pairs = []  # [(en_term, zh_term)]
    if term_dict:
        en_lower = en_text.lower()
        for en_term, zh_term in term_dict.items():
            # 只检查在原文中实际出现的术语
            if en_term.lower() in en_lower:
                bilingual_pairs.append((en_term, zh_term))

    # 去重: 如果基础 token 已被双语术语覆盖 (如 "3D" 在 "3D orientation → 三维朝向" 中)
    bilingual_en_lower = {en.lower() for en, _ in bilingual_pairs}
    tokens = {t for t in tokens if t.lower() not in bilingual_en_lower}

    if not tokens and not bilingual_pairs:
        return 1.0, 0

    zh_lower = zh_text.lower()
    found = 0

    # 基础 token 匹配 (增强: NxN→N×N, 序数词→数字部分, 数字+字母→数字部分)
    for t in tokens:
        if t in zh_text or t.lower() in zh_lower:
            found += 1
        elif re.fullmatch(r'\d+x\d+', t):
            # 3x3 → 3×3 (全角乘号)
            alt = t.replace('x', '×')
            if alt in zh_text:
                found += 1
        elif re.fullmatch(r'\d+(?:st|nd|rd|th)', t):
            # 19th → 查找数字部分 "19"
            num_part = re.match(r'\d+', t).group()
            if num_part in zh_text:
                found += 1
        elif re.fullmatch(r'\d+[A-Za-z]+', t):
            # 3D/2D → 数字部分 "3"/"2" 出现在 zh 中即可
            num_part = re.match(r'\d+', t).group()
            if len(num_part) >= 1 and num_part in zh_text:
                found += 1

    # 双语术语匹配: EN 原词 OR ZH 翻译出现即算覆盖
    for en_term, zh_term in bilingual_pairs:
        if (en_term.lower() in zh_lower) or (zh_term in zh_text):
            found += 1

    total = len(tokens) + len(bilingual_pairs)
    return found / total, total


def compute_combined_score(
    alignment_sim: float,
    budget_score: dict,
    repetition_ratio: float,
    coverage: float | tuple[float, int],
) -> float:
    """综合质量评分 (0-100)。

    权重: Alignment 50% + Budget 25% + Repetition 15% + Coverage 10%
    当 coverage 无 anchor token 时，将 10% 权重转给 alignment (60/25/15/0)。
    """
    if isinstance(coverage, tuple):
        cov_ratio, n_tokens = coverage
    else:
        cov_ratio, n_tokens = coverage, 1  # 兼容旧调用

    mae = budget_score.get("mae", 5)
    budget_pts = max(0, 1.0 - mae / 10) * 25
    rep_pts = (1.0 - min(repetition_ratio, 1.0)) * 15

    if n_tokens > 0:
        align_pts = alignment_sim * 50
        cov_pts = cov_ratio * 10
    else:
        # 无 anchor token: coverage 无信号, 权重全归 alignment
        align_pts = alignment_sim * 60
        cov_pts = 0

    return align_pts + budget_pts + rep_pts + cov_pts


# ─────────────────────────────────────────────────────────
# 对比报告
# ─────────────────────────────────────────────────────────

def print_comparison(
    segments_orig: list[dict], segments_phase2: list[str],
    budgets: list[int], score: dict,
):
    """打印 Phase 1 vs Phase 2 对比"""
    print("\n" + "=" * 60)
    print("Phase 1 vs Phase 2 对比")
    print("=" * 60)

    print(f"\n  Budget 偏差统计:")
    print(f"    MAE:  {score['mae']:.1f} 字/段")
    print(f"    RMSE: {score['mse'] ** 0.5:.1f} 字/段")
    print(f"    最大偏差: {score['max_dev']} 字")
    within2 = sum(1 for d in score["deviations"] if abs(d) <= 2)
    print(f"    ±2字内: {within2}/{len(budgets)} ({100*within2/len(budgets):.0f}%)")
    within5 = sum(1 for d in score["deviations"] if abs(d) <= 5)
    print(f"    ±5字内: {within5}/{len(budgets)} ({100*within5/len(budgets):.0f}%)")

    # 抽样对比
    print(f"\n  抽样对比 (偏差最大的 5 段):")
    indices_by_dev = sorted(range(len(budgets)),
                            key=lambda i: abs(score["deviations"][i]),
                            reverse=True)
    for idx in indices_by_dev[:5]:
        dev = score["deviations"][idx]
        p1 = segments_orig[idx]["text_zh"]
        p2 = segments_phase2[idx]
        print(f"    [{idx}] budget={budgets[idx]}, dev={dev:+d}")
        print(f"      P1: {p1[:60]}{'...' if len(p1) > 60 else ''}")
        print(f"      P2: {p2[:60]}{'...' if len(p2) > 60 else ''}")

    # 再抽样几个随机段
    print(f"\n  随机抽样 (3 段):")
    import random
    sample_indices = random.sample(range(len(budgets)), min(3, len(budgets)))
    for idx in sorted(sample_indices):
        dev = score["deviations"][idx]
        p1 = segments_orig[idx]["text_zh"]
        p2 = segments_phase2[idx]
        print(f"    [{idx}] budget={budgets[idx]}, dev={dev:+d}")
        print(f"      P1: {p1[:60]}{'...' if len(p1) > 60 else ''}")
        print(f"      P2: {p2[:60]}{'...' if len(p2) > 60 else ''}")


# ─────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Phase 2: 全文翻译质量优化")
    parser.add_argument("output_dir", help="视频输出目录 (如 output/zjMuIxRvygQ)")
    parser.add_argument("--candidates", type=int, default=5, help="翻译候选数 (默认 5)")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    cache_path = output_dir / "segments_cache.json"
    if not cache_path.exists():
        print(f"❌ 找不到 {cache_path}")
        sys.exit(1)

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"❌ 找不到配置文件 {config_path}")
        sys.exit(1)

    # 加载配置
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)
    llm = config.get("llm", {})
    api_url = llm.get("api_url", "").rstrip("/")
    api_key = llm.get("api_key", "")
    model = llm.get("model", "")
    if not api_url or not api_key or not model:
        print("❌ config.json 中 llm.api_url/api_key/model 不完整")
        sys.exit(1)

    endpoint = api_url if "/chat/completions" in api_url else f"{api_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # 加载 Phase 1 结果
    with open(cache_path, encoding="utf-8") as f:
        segments = json.load(f)

    budgets_hanzi = extract_budgets(segments)
    budgets_jieba = extract_budgets_jieba(segments)
    total_hanzi = sum(budgets_hanzi)
    total_jieba = sum(budgets_jieba)
    full_english = " ".join(seg["text_en"] for seg in segments)
    en_segments = [seg["text_en"] for seg in segments]

    _load_embedding_model()

    # ── 翻译风格识别 (LLM guide) ──
    from translation_style import (
        detect_translation_style, load_cached_style, parse_term_rules,
    )
    style_guide, term_rules = load_cached_style(output_dir)
    if not style_guide:
        style_guide, term_rules = detect_translation_style(
            segments, "", endpoint, headers, model,
            output_dir=output_dir, text_key="text_en",
        )
    term_dict = parse_term_rules(term_rules) if term_rules else {}

    print(f"Phase 2 全文翻译质量优化")
    print(f"  目录: {output_dir}")
    print(f"  段数: {len(segments)}")
    print(f"  英文: {len(full_english)} 字符 (~{len(full_english)//4} tokens)")
    print(f"  Budget (hanzi): 总{total_hanzi}, 范围 {min(budgets_hanzi)}-{max(budgets_hanzi)} (avg={total_hanzi/len(budgets_hanzi):.1f})")
    print(f"  Budget (jieba): 总{total_jieba}, 范围 {min(budgets_jieba)}-{max(budgets_jieba)} (avg={total_jieba/len(budgets_jieba):.1f})")
    if term_dict:
        print(f"  术语字典: {len(term_dict)} 条")

    # 打印两种 budget 差异较大的段
    diffs = [(i, budgets_hanzi[i], budgets_jieba[i]) for i in range(len(segments))
             if abs(budgets_hanzi[i] - budgets_jieba[i]) >= 3]
    if diffs:
        print(f"  Budget 差异 ≥3 的段 ({len(diffs)}段):")
        for i, bh, bj in diffs[:5]:
            print(f"    [{i}] hanzi={bh} jieba={bj} ({bj-bh:+d}) | {segments[i]['text_zh'][:40]}...")
    print()

    # Token 估算检查
    est_input_tokens = len(full_english) // 4
    est_output_tokens = total_hanzi * 2
    est_total = est_input_tokens + est_output_tokens
    if est_total > 28000:
        print(f"  ⚠️  预估 token 数 {est_total} 较大，可能需要分批（当前仍一次性发送）")

    # 生成候选 (注入 style guide)
    print(f"🔄 生成 {args.candidates} 个全文翻译候选...")
    candidates = generate_candidates(
        full_english, endpoint, headers, model,
        n_candidates=args.candidates,
        max_tokens=max(8192, total_hanzi * 3),
        total_budget=total_hanzi,
        style_guide=style_guide,
    )
    if not candidates:
        print("❌ 所有候选生成失败")
        sys.exit(1)

    print(f"\n  成功: {len(candidates)}/{args.candidates} 个候选")

    # 预过滤：总字数偏差 > 40% 的跳过
    valid_candidates = []
    for i, cand in enumerate(candidates):
        cand_hanzi = count_hanzi(cand)
        deviation_pct = abs(cand_hanzi - total_hanzi) / total_hanzi * 100
        if deviation_pct > 40:
            print(f"  候选 {i+1}: {cand_hanzi}字 (偏差 {deviation_pct:.0f}%) — 跳过")
        else:
            valid_candidates.append((i, cand))
            print(f"  候选 {i+1}: {cand_hanzi}字 (偏差 {deviation_pct:.0f}%)")

    if not valid_candidates:
        print("❌ 所有候选字数偏差过大")
        sys.exit(1)

    # DP 切分 + 评分（两种 budget 各跑一次）
    budget_modes = [
        ("hanzi", budgets_hanzi),
        ("jieba", budgets_jieba),
    ]
    best_results = {}  # mode -> (score, segments, idx)

    for mode_name, mode_budgets in budget_modes:
        print(f"\n🔪 DP 切分 + 评分 [{mode_name} budget]...")
        best_combined = -1
        best_score = None
        best_segments = None
        best_idx = -1

        for orig_idx, cand in valid_candidates:
            cand_clean = normalize_llm_output(cand)
            cand_clean = text_for_duration(cand_clean)
            split_result = split_text_by_budgets(cand_clean, mode_budgets)
            sc = score_candidate(split_result, mode_budgets)

            align_sim, _ = compute_alignment_score(en_segments, split_result)
            repetition = compute_repetition_score(cand_clean)
            cov_ratio, cov_n = compute_source_coverage(full_english, cand_clean, term_dict)
            combined = compute_combined_score(align_sim, sc, repetition, (cov_ratio, cov_n))

            print(f"  候选 {orig_idx+1}: combined={combined:.1f} "
                  f"(align={align_sim:.3f}, MAE={sc['mae']:.1f}, "
                  f"rep={repetition:.3f}, cov={cov_ratio:.2f}/{cov_n})")

            if combined > best_combined:
                best_combined = combined
                best_score = sc
                best_segments = split_result
                best_idx = orig_idx

        print(f"  ✅ [{mode_name}] 选中候选 {best_idx+1} (combined={best_combined:.1f})")
        best_results[mode_name] = (best_score, best_segments, best_idx)

    # 构建两种 Phase 2 输出
    for mode_name, mode_budgets in budget_modes:
        best_score, best_segments, best_idx = best_results[mode_name]

        phase2_segments = []
        for seg, new_zh in zip(segments, best_segments):
            phase2_segments.append({
                "start": seg["start"],
                "end": seg["end"],
                "text_en": seg["text_en"],
                "text_zh": normalize_llm_output(new_zh) if new_zh else seg["text_zh"],
            })

        # 对比报告
        print_comparison(segments, best_segments, mode_budgets, best_score)

        # 写文件
        if args.dry_run:
            print(f"\n  (dry-run 模式，未写文件)")
        else:
            suffix = "" if mode_name == "hanzi" else f"_{mode_name}"
            out_path = output_dir / f"segments_cache_phase2{suffix}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(phase2_segments, f, ensure_ascii=False, indent=2)
            print(f"\n  ✅ [{mode_name}] 已写入: {out_path}")


if __name__ == "__main__":
    main()
