#!/usr/bin/env python3
"""Phase 2 渐进分段翻译实验

核心思路: 从全文翻译（1组）渐进到细粒度分段（2^k 组），
搜索「翻译流畅度 vs 对齐质量」的最优分段粒度。

Split level 0: 1 组 (全文)        → 高流畅 + 低对齐
Split level 1: 2 组 (≈36段/组)    → ...
Split level 2: 4 组 (≈18段/组)    → ...
Split level 3: 8 组 (≈9段/组)     → ...
...
Split level k: 2^k 组             → 低流畅 + 高对齐

每组翻译时提供前后邻组的英文摘要作为上下文，保证衔接。
组内自由翻译，组间拼接后做 DP 切分。

用法:
    python phase2_iterative_split.py output/zjMuIxRvygQ
    python phase2_iterative_split.py output/zjMuIxRvygQ --max-level 7 --config config.json
"""

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

import httpx
import jieba

sys.path.insert(0, str(Path(__file__).resolve().parent))
from text_utils import normalize_llm_output, text_for_duration
from duration_estimator import estimate_duration as _estimate_duration_jieba
from phase2_translate import (
    count_hanzi, extract_budgets, call_llm,
    split_text_by_budgets, score_candidate, BASE_SYSTEM_PROMPT,
    _load_embedding_model, compute_alignment_score,
    compute_repetition_score, compute_source_coverage, compute_combined_score,
)
from translation_style import (
    detect_translation_style, load_cached_style, parse_term_rules,
)

# ─────────────────────────────────────────────────────────
# 分组翻译核心
# ─────────────────────────────────────────────────────────

TRANSLATE_SYSTEM_PROMPT = (
    "你是专业的英中视频翻译专家。请将以下英文视频片段完整翻译为自然流畅的中文。\n"
    "要求：\n"
    "1) 完整保留原文全部信息，不遗漏、不添加\n"
    "2) 译文通顺自然，符合中文表达习惯，适合配音朗读\n"
    "3) 保持原文的语气和情感\n"
    "4) 计算机/数学术语保留通用英文缩写\n"
    "5) 不要添加原文没有的信息（如人名、公司名、数据等）\n"
    "6) 只输出翻译结果，不要解释"
)


def split_into_groups(segments: list, n_groups: int) -> list[list[int]]:
    """将段索引列表均匀分为 n_groups 组，返回每组的索引列表"""
    n = len(segments)
    if n_groups >= n:
        return [[i] for i in range(n)]
    groups = []
    base_size = n // n_groups
    remainder = n % n_groups
    idx = 0
    for g in range(n_groups):
        size = base_size + (1 if g < remainder else 0)
        groups.append(list(range(idx, idx + size)))
        idx += size
    return groups


def translate_groups(
    en_segments: list[str], groups: list[list[int]], budgets: list[int],
    endpoint: str, headers: dict, model: str,
    temperature: float = 0.3, max_tokens: int = 8192,
    context_sentences: int = 1, style_guide: str = "",
) -> str:
    """逐组翻译并拼接。每组提供前后上下文。"""
    sys_prompt = TRANSLATE_SYSTEM_PROMPT
    if style_guide:
        sys_prompt += f"\n{style_guide}"

    all_translations = []

    for gi, group_indices in enumerate(groups):
        group_en = " ".join(en_segments[idx] for idx in group_indices)
        group_budget = sum(budgets[idx] for idx in group_indices)

        # 上下文量根据组大小调整
        ctx_n = 2 if len(group_indices) <= 10 else context_sentences

        # 构建上下文
        context_parts = []
        if gi > 0:
            prev_indices = groups[gi - 1]
            prev_ctx = " ".join(en_segments[idx] for idx in prev_indices[-ctx_n:])
            context_parts.append(f"[前文英文] {prev_ctx}")
            if all_translations:
                prev_zh = all_translations[-1]
                zh_tail = prev_zh[-100:] if len(prev_zh) > 100 else prev_zh
                context_parts.append(f"[前文中文结尾] ...{zh_tail}")

        if gi < len(groups) - 1:
            next_indices = groups[gi + 1]
            next_ctx = " ".join(en_segments[idx] for idx in next_indices[:ctx_n])
            context_parts.append(f"[后文英文] {next_ctx}")

        context_str = "\n".join(context_parts)

        # 更严格的字数范围
        lo = int(group_budget * 0.85)
        hi = int(group_budget * 1.15)
        budget_hint = f"（译文字数严格控制在 {lo}~{hi} 字之间，目标 {group_budget} 字）"

        # 组内 ||| 标记: 大组 (>5段) 每 5 段插入标记，帮助保持语序
        if len(group_indices) > 5:
            sub_parts = []
            for si in range(0, len(group_indices), 5):
                sub = " ".join(en_segments[idx] for idx in group_indices[si:si+5])
                sub_parts.append(sub)
            group_en_marked = " ||| ".join(sub_parts)
            marker_hint = "\n原文中的 ||| 是分段标记，请在译文对应位置也保留 |||，保持段间顺序。"
        else:
            group_en_marked = group_en
            marker_hint = ""
        if context_str:
            user_prompt = (
                f"上下文参考（仅供理解衔接，不要翻译上下文内容）:\n{context_str}\n\n"
                f"请仅翻译以下英文片段（不要重复翻译上下文内容）：{budget_hint}{marker_hint}\n\n{group_en_marked}"
            )
        else:
            user_prompt = f"请翻译以下英文视频内容：{budget_hint}{marker_hint}\n\n{group_en_marked}"

        try:
            text = call_llm(
                endpoint, headers, model,
                sys_prompt, user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            text = normalize_llm_output(text)
            text = text_for_duration(text)
            # 移除组内 ||| 标记
            text = re.sub(r'\s*\|\|\|\s*', '', text)

            # 组边界去重: 检查当前组开头是否与上一组结尾重叠
            if all_translations and text:
                prev_tail = all_translations[-1][-80:]  # 上一组末尾 80 字符
                # 查找最长公共子串 (从 prev_tail 末尾向前扫描)
                best_overlap = 0
                for overlap_len in range(min(len(prev_tail), len(text), 60), 5, -1):
                    if prev_tail.endswith(text[:overlap_len]):
                        best_overlap = overlap_len
                        break
                if best_overlap >= 6:
                    text = text[best_overlap:]

            all_translations.append(text)
        except Exception as e:
            print(f"    组{gi} 翻译失败: {e}")
            all_translations.append("")

    return "".join(all_translations)


# ─────────────────────────────────────────────────────────
# 评估一个分段级别
# ─────────────────────────────────────────────────────────

def evaluate_split_level(
    level: int, n_groups: int,
    en_segments: list[str], budgets: list[int], full_english: str,
    endpoint: str, headers: dict, model: str,
    n_candidates: int = 2,
    style_guide: str = "", term_dict: dict | None = None,
) -> dict:
    """评估给定分组数量的翻译质量。生成多个候选取最优。"""
    groups = split_into_groups(en_segments, n_groups)
    segs_per_group = [len(g) for g in groups]

    print(f"\n  Level {level}: {n_groups} 组 (段/组: "
          f"min={min(segs_per_group)}, max={max(segs_per_group)}, "
          f"avg={sum(segs_per_group)/len(segs_per_group):.1f})")

    best_result = None
    best_combined = -1

    temps = [0.3, 0.5, 0.7][:n_candidates]

    for ci, temp in enumerate(temps):
        print(f"    候选 {ci+1} (T={temp})...", end="", flush=True)
        t0 = time.time()

        # 逐组翻译
        zh_full = translate_groups(
            en_segments, groups, budgets,
            endpoint, headers, model,
            temperature=temp,
            max_tokens=max(4096, sum(budgets) * 3),
            style_guide=style_guide,
        )
        elapsed = time.time() - t0
        hanzi = count_hanzi(zh_full)
        print(f" {hanzi}字, {elapsed:.1f}s ({len(groups)}组)")

        # DP 切分
        zh_segments = split_text_by_budgets(zh_full, budgets)
        b_score = score_candidate(zh_segments, budgets)

        # Embedding 对齐
        align_sim, per_seg_sims = compute_alignment_score(en_segments, zh_segments)

        # 量化指标
        repetition = compute_repetition_score(zh_full)
        cov_ratio, cov_n = compute_source_coverage(full_english, zh_full, term_dict)
        combined = compute_combined_score(align_sim, b_score, repetition, (cov_ratio, cov_n))

        print(f"      combined={combined:.1f} (align={align_sim:.3f}, "
              f"MAE={b_score['mae']:.1f}, rep={repetition:.3f}, cov={cov_ratio:.2f}/{cov_n})")

        result = {
            "level": level,
            "n_groups": n_groups,
            "candidate": ci + 1,
            "temperature": temp,
            "hanzi": hanzi,
            "combined": round(combined, 2),
            "alignment_sim": round(align_sim, 4),
            "budget_mae": b_score["mae"],
            "budget_max_dev": b_score["max_dev"],
            "repetition": round(repetition, 4),
            "coverage": round(cov_ratio, 4),
            "coverage_tokens": cov_n,
            "elapsed": round(elapsed, 1),
            "zh_full": zh_full,
            "zh_segments": zh_segments,
            "per_seg_sims": [round(s, 3) for s in per_seg_sims] if per_seg_sims else [],
        }

        if combined > best_combined:
            best_combined = combined
            best_result = result

    return best_result


# ─────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────

def run_progressive_split(
    output_dir: Path, config_path: Path,
    max_level: int = 7, n_candidates: int = 2,
    dry_run: bool = False, no_embedding: bool = False,
):
    cache_path = output_dir / "segments_cache.json"
    if not cache_path.exists():
        print(f"❌ 找不到 {cache_path}")
        sys.exit(1)

    # ── 归档上次运行产物 ──
    from datetime import datetime
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    audit_dir = output_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)

    old_files = [
        output_dir / "segments_cache_phase2_split.json",
        output_dir / "phase2_split_best_translation.txt",
        audit_dir / "phase2_split_log.json",
    ]
    archived = 0
    for fp in old_files:
        if fp.exists():
            ts_name = f"{fp.stem}_{fp.stat().st_mtime:.0f}{fp.suffix}"
            dest = audit_dir / ts_name
            fp.rename(dest)
            archived += 1
    if archived:
        print(f"  📦 已归档 {archived} 个旧产物到 {audit_dir}/")

    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    llm = config.get("llm", {})
    api_url = llm.get("api_url", "").rstrip("/")
    api_key = llm.get("api_key", "")
    model_name = llm.get("model", "")
    endpoint = api_url if "/chat/completions" in api_url else f"{api_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    with open(cache_path, encoding="utf-8") as f:
        segments = json.load(f)

    budgets = extract_budgets(segments)
    total_budget = sum(budgets)
    full_english = " ".join(seg["text_en"] for seg in segments)
    en_segments = [seg["text_en"] for seg in segments]
    n_segs = len(segments)

    # ── 翻译风格识别 (LLM guide) ──
    style_guide, term_rules = load_cached_style(output_dir)
    if not style_guide:
        style_guide, term_rules = detect_translation_style(
            segments, "", endpoint, headers, model_name,
            output_dir=output_dir, text_key="text_en",
        )
    term_dict = parse_term_rules(term_rules) if term_rules else {}

    print("=" * 60)
    print("Phase 2 渐进分段翻译实验")
    print("=" * 60)
    print(f"  目录: {output_dir}")
    print(f"  段数: {n_segs}")
    print(f"  英文: {len(full_english)} 字符")
    print(f"  Budget: 总{total_budget}字, avg={total_budget/n_segs:.1f}")
    print(f"  最大级别: {max_level} (最大组数 2^{max_level}={2**max_level})")
    if term_dict:
        print(f"  术语字典: {len(term_dict)} 条")

    if not no_embedding:
        _load_embedding_model()
    print()

    # 实验记录
    experiment_log = {
        "video_dir": str(output_dir),
        "model": model_name,
        "n_segments": n_segs,
        "total_budget": total_budget,
        "levels": [],
    }

    # ── 渐进分段: 聚焦最优区间 ──
    # 前3轮实验表明最优在 2-4 组，细搜 1-6 + 边界 8/12
    group_schedule = [1, 2, 3, 4, 5, 6, 8, 12, 16, 24]
    group_schedule = [g for g in group_schedule if g <= n_segs]
    group_schedule = group_schedule[:max_level + 1]

    best_overall = None
    best_overall_combined = -1
    consecutive_worse = 0

    for level, n_groups in enumerate(group_schedule):
        if n_groups > n_segs:
            n_groups = n_segs

        print(f"\n{'='*60}")
        print(f"🔄 Split Level {level}: {n_groups} 组")
        print(f"{'='*60}")

        result = evaluate_split_level(
            level, n_groups,
            en_segments, budgets, full_english,
            endpoint, headers, model_name,
            n_candidates=n_candidates,
            style_guide=style_guide, term_dict=term_dict,
        )

        # 记录
        log_entry = {k: v for k, v in result.items()
                     if k not in ("zh_full", "zh_segments", "per_seg_sims")}
        log_entry["per_seg_sims_summary"] = {
            "mean": round(sum(result["per_seg_sims"]) / len(result["per_seg_sims"]), 3) if result["per_seg_sims"] else 0,
            "min": round(min(result["per_seg_sims"]), 3) if result["per_seg_sims"] else 0,
            "gt_0.5": sum(1 for s in result["per_seg_sims"] if s > 0.5),
        }
        experiment_log["levels"].append(log_entry)

        # 判断是否全局最优
        if result["combined"] > best_overall_combined:
            improvement = result["combined"] - best_overall_combined if best_overall else 0
            print(f"\n  ★ 新全局最优! combined={result['combined']:.1f}"
                  f"{f' (+{improvement:.1f})' if best_overall else ''}")
            best_overall = result
            best_overall_combined = result["combined"]
        else:
            print(f"\n  ↓ 不如当前最优 (best={best_overall_combined:.1f}, "
                  f"this={result['combined']:.1f})")

    # ── 最终结果 ──
    print(f"\n{'='*60}")
    print(f"📊 渐进分段实验结果")
    print(f"{'='*60}")

    # 汇总表
    print(f"\n  Level | 组数 | 段/组 | combined | align  | MAE  |  rep  |  cov  | 耗时")
    print(f"  {'─'*75}")
    for entry in experiment_log["levels"]:
        segs_per = n_segs / entry["n_groups"]
        marker = " ★" if entry["combined"] == best_overall_combined else ""
        print(f"  {entry['level']:5d} | {entry['n_groups']:4d} | "
              f"{segs_per:5.1f} | {entry['combined']:8.1f} | "
              f"{entry['alignment_sim']:6.3f} | {entry['budget_mae']:4.1f} | "
              f"{entry.get('repetition', 0):5.3f} | {entry.get('coverage', 1):5.2f} | "
              f"{entry['elapsed']:5.1f}s{marker}")

    best = best_overall
    print(f"\n  最优: Level {best['level']} ({best['n_groups']} 组)")
    print(f"    combined={best['combined']:.1f}, "
          f"align={best['alignment_sim']:.3f}, "
          f"MAE={best['budget_mae']:.1f}, "
          f"rep={best.get('repetition', 0):.3f}, "
          f"cov={best.get('coverage', 1):.2f}")

    # Budget 分析
    b_score = score_candidate(best["zh_segments"], budgets)
    within2 = sum(1 for d in b_score["deviations"] if abs(d) <= 2)
    print(f"    Budget ±2字内: {within2}/{n_segs}")

    # 写出结果
    if not dry_run:
        phase2_segments = []
        for seg, new_zh in zip(segments, best["zh_segments"]):
            phase2_segments.append({
                "start": seg["start"],
                "end": seg["end"],
                "text_en": seg["text_en"],
                "text_zh": normalize_llm_output(new_zh) if new_zh else seg["text_zh"],
            })

        # 写固定名 (最新结果)
        out_path = output_dir / "segments_cache_phase2_split.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(phase2_segments, f, ensure_ascii=False, indent=2)
        print(f"\n  ✅ 切分结果: {out_path}")

        experiment_log["best"] = {
            "level": best["level"],
            "n_groups": best["n_groups"],
            "combined": best["combined"],
            "alignment_sim": best["alignment_sim"],
            "budget_mae": best["budget_mae"],
            "repetition": best.get("repetition", 0),
            "coverage": best.get("coverage", 1),
        }
        experiment_log["run_ts"] = run_ts

        log_path = audit_dir / "phase2_split_log.json"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(experiment_log, f, ensure_ascii=False, indent=2)
        print(f"  ✅ 实验日志: {log_path}")

        best_text_path = output_dir / "phase2_split_best_translation.txt"
        with open(best_text_path, "w", encoding="utf-8") as f:
            f.write(best["zh_full"])
        print(f"  ✅ 最优译文: {best_text_path}")

        # 写带时间戳副本 (保留历史)
        ts_log = audit_dir / f"phase2_split_log_{run_ts}.json"
        with open(ts_log, "w", encoding="utf-8") as f:
            json.dump(experiment_log, f, ensure_ascii=False, indent=2)
        ts_txt = audit_dir / f"phase2_split_best_{run_ts}.txt"
        with open(ts_txt, "w", encoding="utf-8") as f:
            f.write(best["zh_full"])
        print(f"  ✅ 历史归档: {ts_log.name}, {ts_txt.name}")

    # 抽样对比
    print(f"\n  抽样对比 (前 5 段):")
    for idx in range(min(5, n_segs)):
        p1 = segments[idx]["text_zh"]
        p2 = best["zh_segments"][idx] if idx < len(best["zh_segments"]) else ""
        dev = b_score["deviations"][idx] if idx < len(b_score["deviations"]) else 0
        print(f"    [{idx}] budget={budgets[idx]}, dev={dev:+d}")
        print(f"      P1: {p1[:60]}{'...' if len(p1) > 60 else ''}")
        print(f"      P2: {p2[:60]}{'...' if len(p2) > 60 else ''}")

    return experiment_log


def main():
    parser = argparse.ArgumentParser(description="Phase 2 渐进分段翻译实验")
    parser.add_argument("output_dir", help="视频输出目录")
    parser.add_argument("--max-level", type=int, default=7, help="最大分段级别 (默认 7, 即 128 组)")
    parser.add_argument("--candidates", type=int, default=2, help="每级候选数")
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    parser.add_argument("--no-embedding", action="store_true", help="跳过 embedding")
    args = parser.parse_args()

    run_progressive_split(
        output_dir=Path(args.output_dir),
        config_path=Path(args.config),
        max_level=args.max_level,
        n_candidates=args.candidates,
        dry_run=args.dry_run,
        no_embedding=args.no_embedding,
    )


if __name__ == "__main__":
    main()
