#!/usr/bin/env python3
"""翻译质量评估工具 — 对比 pipeline 翻译与 YouTube 社区中文字幕 (ground truth)

用法:
  python3 test_translate_only.py --compare-only --all          # 评估所有视频现有翻译
  python3 test_translate_only.py --compare-only --video zjMuIxRvygQ  # 评估单个视频
  python3 test_translate_only.py --all                         # 重新翻译并评估所有视频
  python3 test_translate_only.py --video zjMuIxRvygQ           # 重新翻译并评估单个视频
"""

import argparse
import json
import os
import re
import sys
import statistics
from pathlib import Path

# 导入 jieba 时长估算器
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import jieba as _jieba_test  # noqa: F401
    from pipeline import _estimate_duration_jieba
    HAS_JIEBA_ESTIMATOR = True
except (ImportError, ModuleNotFoundError):
    HAS_JIEBA_ESTIMATOR = False

# ── 视频目录 → GT 文件映射 ──
VIDEO_GT_MAP = {
    "d4EgbgTm0Bg": "d4EgbgTm0Bg.zh.srt",
    "kCc8FmEb1nY": None,  # 无 GT
    "zjMuIxRvygQ": "zjMuIxRvygQ.zh.srt",
    "Calculus/WUvTyaaNkzM": "WUvTyaaNkzM.zh.srt",
    "Computer Science/03_But_how_does_bitcoin_actually_work_": "bBC-nXj3Ng4.zh.srt",
    "Computer Science/05_Simulating_an_epidemic": "gxAaO2rsdIs.zh.srt",
    "Differential Equations/01_Differential_equations,_studying_the_unsolvable": "p_di4Zn4wz4.zh.srt",
    "Neural Networks/aircAruvnKk": "aircAruvnKk.zh.srt",
    "Analysis/02_But_what_is_the_Fourier_Transform__A_visual_introduction.": "spUNpyF58BY.zh.srt",
    "Probability/06_But_what_is_the_Central_Limit_Theorem_": "zeJD6dqJ5lo.zh.srt",
}

ALL_VIDEOS = list(VIDEO_GT_MAP.keys())

PROJECT_DIR = Path(__file__).resolve().parent
GT_DIR = PROJECT_DIR / "ground_truth"
OUTPUT_DIR = PROJECT_DIR / "output"


def parse_srt(path: str) -> list:
    """解析 SRT 字幕文件 → [(start_sec, end_sec, text), ...]"""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    blocks = re.split(r"\n\s*\n", content.strip())
    entries = []
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        ts = re.match(
            r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)",
            lines[1],
        )
        if not ts:
            continue
        g = ts.groups()
        start = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3]) / 1000
        end = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7]) / 1000
        text = " ".join(lines[2:]).strip()
        if text:
            entries.append((start, end, text))
    return entries


def align_seg_to_gt(seg_start, seg_end, gt_entries):
    """根据时间戳重叠找到对应 GT 文本"""
    matches = []
    for gs, ge, gt_text in gt_entries:
        overlap = min(seg_end, ge) - max(seg_start, gs)
        if overlap > 0.1:
            matches.append(gt_text)
    return " ".join(matches) if matches else None


def zh_char_count(text: str) -> int:
    return sum(1 for c in text if "\u4e00" <= c <= "\u9fff")


def evaluate_video(video_id: str, segments: list, gt_entries: list = None) -> dict:
    """评估单个视频的翻译质量 (双目标: 信息完整度 + TTS时长拟合)"""
    total = len(segments)
    cps_list = []
    jieba_ratios = []
    ellipsis_count = 0
    telegram_count = 0
    too_short_count = 0
    dup_neighbor_count = 0
    info_ratios = []
    window_info_ratios = []
    worst_segments = []

    for i, s in enumerate(segments):
        zh = s.get("text_zh", "")
        en = s.get("text_en", s.get("text", ""))
        dur = s.get("end", 0) - s.get("start", 0)
        target_ms = int(dur * 1000)
        zc = zh_char_count(zh)
        cps = zc / dur if dur > 0 else 0
        cps_list.append(cps)

        # jieba 时长拟合
        if HAS_JIEBA_ESTIMATOR and target_ms > 500 and zc >= 2:
            jb_ms = _estimate_duration_jieba(zh)
            jb_ratio = jb_ms / target_ms
            jieba_ratios.append(jb_ratio)

        if zh.strip().endswith("……") or zh.strip().endswith("..."):
            ellipsis_count += 1
        if zh.strip().endswith("，") and len(zh.strip()) < 12:
            telegram_count += 1
        en_words = len(en.split())
        if en_words > 8 and zc < 5:
            too_short_count += 1

        # 邻段重复
        if i > 0:
            prev_zh = segments[i - 1].get("text_zh", "")
            for ln in range(min(len(zh), len(prev_zh)), 6, -1):
                found = False
                for st in range(len(prev_zh) - ln + 1):
                    sub = prev_zh[st : st + ln]
                    if sub in zh:
                        dup_neighbor_count += 1
                        found = True
                        break
                if found:
                    break

        # GT 对比: 逐段
        if gt_entries:
            gt_text = align_seg_to_gt(s.get("start", 0), s.get("end", 0), gt_entries)
            if gt_text:
                gt_chars = zh_char_count(gt_text)
                our_chars = zc
                if gt_chars > 0:
                    ratio = our_chars / gt_chars
                    info_ratios.append(ratio)
                    if ratio < 0.5:
                        worst_segments.append(
                            {
                                "idx": i,
                                "ratio": ratio,
                                "en": en[:100],
                                "ours": zh,
                                "gt": gt_text[:150],
                            }
                        )

            # GT 对比: 3段窗口 (区分错位 vs 真丢)
            lo = max(0, i - 1)
            hi = min(total - 1, i + 1)
            win_zh = "".join(segments[j].get("text_zh", "") for j in range(lo, hi + 1))
            win_gt = align_seg_to_gt(segments[lo].get("start", 0), segments[hi].get("end", 0), gt_entries)
            if win_gt:
                win_gt_chars = zh_char_count(win_gt)
                if win_gt_chars > 0:
                    window_info_ratios.append(zh_char_count(win_zh) / win_gt_chars)

    # 排序 worst 按 ratio 升序
    worst_segments.sort(key=lambda x: x["ratio"])

    metrics = {
        "total_segments": total,
        "cps_mean": statistics.mean(cps_list) if cps_list else 0,
        "cps_median": statistics.median(cps_list) if cps_list else 0,
        "cps_stdev": statistics.stdev(cps_list) if len(cps_list) > 1 else 0,
        "cps_gt6": sum(1 for c in cps_list if c > 6),
        "cps_lt2": sum(1 for c in cps_list if c < 2),
        "ellipsis_count": ellipsis_count,
        "telegram_count": telegram_count,
        "too_short_count": too_short_count,
        "dup_neighbor_count": dup_neighbor_count,
    }

    # jieba 时长拟合指标
    if jieba_ratios:
        metrics["jieba_ratio_mean"] = statistics.mean(jieba_ratios)
        metrics["jieba_ratio_median"] = statistics.median(jieba_ratios)
        metrics["jieba_gt150"] = sum(1 for r in jieba_ratios if r > 1.5)
        metrics["jieba_085_115"] = sum(1 for r in jieba_ratios if 0.85 <= r <= 1.15)
        metrics["jieba_count"] = len(jieba_ratios)

    if info_ratios:
        metrics["info_ratio_mean"] = statistics.mean(info_ratios)
        metrics["info_ratio_median"] = statistics.median(info_ratios)
        metrics["info_ratio_lt50"] = sum(1 for r in info_ratios if r < 0.5)
        metrics["info_ratio_lt70"] = sum(1 for r in info_ratios if r < 0.7)
        metrics["gt_aligned_count"] = len(info_ratios)

    if window_info_ratios:
        metrics["win_info_mean"] = statistics.mean(window_info_ratios)
        metrics["win_info_lt50"] = sum(1 for r in window_info_ratios if r < 0.5)

    metrics["worst_segments"] = worst_segments[:10]
    return metrics


def load_segments(video_id: str) -> list:
    """加载视频翻译结果 (segments_cache.json 或 .old)"""
    cache = OUTPUT_DIR / video_id / "segments_cache.json"
    old_cache = OUTPUT_DIR / video_id / "segments_cache.json.old"
    path = cache if cache.exists() else old_cache if old_cache.exists() else None
    if not path:
        return None
    with open(path) as f:
        return json.load(f)


def run_translation(video_id: str, config: dict) -> list:
    """运行翻译流程 (仅翻译，不跑 TTS/合成)"""
    sys.path.insert(0, str(PROJECT_DIR))
    from pipeline import translate_segments

    # 加载 transcribe_cache
    tc_path = OUTPUT_DIR / video_id / "transcribe_cache.json"
    if not tc_path.exists():
        print(f"  [SKIP] {video_id}: 无 transcribe_cache.json")
        return None

    with open(tc_path) as f:
        segments = json.load(f)

    print(f"  翻译 {video_id} ({len(segments)} 段)...")
    result = translate_segments(segments, config)

    # 保存到 segments_cache.json
    cache_path = OUTPUT_DIR / video_id / "segments_cache.json"
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"  已保存: {cache_path}")
    return result


def print_report(video_id: str, metrics: dict, show_worst: int = 5):
    """打印单个视频的评估报告"""
    has_gt = "info_ratio_mean" in metrics
    gt_tag = "" if has_gt else " (无GT)"

    print(f"\n{'='*60}")
    print(f"  {video_id}{gt_tag} ({metrics['total_segments']} 段)")
    print(f"{'='*60}")
    print(f"  CPS: mean={metrics['cps_mean']:.2f} med={metrics['cps_median']:.2f} std={metrics['cps_stdev']:.2f}")
    print(f"  >6 CPS: {metrics['cps_gt6']}  <2 CPS: {metrics['cps_lt2']}")
    print(f"  省略号结尾: {metrics['ellipsis_count']}  电报体: {metrics['telegram_count']}  过短: {metrics['too_short_count']}")
    print(f"  邻段重复: {metrics['dup_neighbor_count']}")

    # jieba 时长拟合
    if "jieba_ratio_mean" in metrics:
        jc = metrics["jieba_count"]
        jm = metrics["jieba_ratio_mean"]
        jmed = metrics["jieba_ratio_median"]
        j_ok = metrics["jieba_085_115"]
        j_over = metrics["jieba_gt150"]
        pct_ok = j_ok / jc * 100 if jc else 0
        print(f"\n  jieba时长拟合 ({jc} 段):")
        print(f"    ratio mean={jm:.2f} med={jmed:.2f}")
        print(f"    [0.85-1.15]: {j_ok} ({pct_ok:.0f}%)  >1.50: {j_over}")

    if has_gt:
        print(f"\n  GT 对比 (对齐 {metrics['gt_aligned_count']} 段):")
        print(f"    信息比 mean={metrics['info_ratio_mean']:.2f} med={metrics['info_ratio_median']:.2f}")
        print(f"    <50%: {metrics['info_ratio_lt50']}  <70%: {metrics['info_ratio_lt70']}")
        if "win_info_mean" in metrics:
            print(f"    3段窗口 mean={metrics['win_info_mean']:.2f}  窗口<50%: {metrics['win_info_lt50']}")

        worst = metrics.get("worst_segments", [])
        if worst and show_worst > 0:
            print(f"\n  信息比最低的 {min(show_worst, len(worst))} 段:")
            for w in worst[:show_worst]:
                print(f"    [{w['idx']}] ratio={w['ratio']:.2f}")
                print(f"      EN:   {w['en']}")
                print(f"      OURS: {w['ours']}")
                print(f"      GT:   {w['gt']}")


def print_summary(all_metrics: dict):
    """打印汇总表"""
    print(f"\n{'='*110}")
    print("  汇总")
    print(f"{'='*110}")
    hdr = (f"  {'视频':<45} {'段数':>5} {'CPS':>5} {'省略':>4} {'电报':>4} {'重复':>4} "
           f"{'信息比':>6} {'窗口比':>6} {'jieba':>6} {'适配%':>5}")
    print(hdr)
    sep = f"  {'-'*45} {'-'*5} {'-'*5} {'-'*4} {'-'*4} {'-'*4} {'-'*6} {'-'*6} {'-'*6} {'-'*5}"
    print(sep)

    total_ellipsis = 0
    total_telegram = 0
    total_dup = 0
    total_segs = 0
    info_ratios_all = []
    win_info_all = []
    jieba_ratios_all = []
    jieba_ok_total = 0
    jieba_count_total = 0

    for vid, m in all_metrics.items():
        ir = f"{m['info_ratio_mean']:.2f}" if "info_ratio_mean" in m else " N/A"
        wi = f"{m['win_info_mean']:.2f}" if "win_info_mean" in m else " N/A"
        jr = f"{m['jieba_ratio_mean']:.2f}" if "jieba_ratio_mean" in m else " N/A"
        if "jieba_count" in m and m["jieba_count"] > 0:
            jp = f"{m['jieba_085_115'] / m['jieba_count'] * 100:.0f}%"
        else:
            jp = " N/A"
        short_vid = vid if len(vid) <= 45 else "..." + vid[-42:]
        print(
            f"  {short_vid:<45} {m['total_segments']:>5} "
            f"{m['cps_mean']:>5.2f} {m['ellipsis_count']:>4} "
            f"{m['telegram_count']:>4} {m['dup_neighbor_count']:>4} "
            f"{ir:>6} {wi:>6} {jr:>6} {jp:>5}"
        )
        total_ellipsis += m["ellipsis_count"]
        total_telegram += m["telegram_count"]
        total_dup += m["dup_neighbor_count"]
        total_segs += m["total_segments"]
        if "info_ratio_mean" in m:
            info_ratios_all.append(m["info_ratio_mean"])
        if "win_info_mean" in m:
            win_info_all.append(m["win_info_mean"])
        if "jieba_ratio_mean" in m:
            jieba_ratios_all.append(m["jieba_ratio_mean"])
        if "jieba_count" in m:
            jieba_ok_total += m.get("jieba_085_115", 0)
            jieba_count_total += m["jieba_count"]

    print(sep)
    avg_ir = f"{statistics.mean(info_ratios_all):.2f}" if info_ratios_all else " N/A"
    avg_wi = f"{statistics.mean(win_info_all):.2f}" if win_info_all else " N/A"
    avg_jr = f"{statistics.mean(jieba_ratios_all):.2f}" if jieba_ratios_all else " N/A"
    avg_jp = f"{jieba_ok_total / jieba_count_total * 100:.0f}%" if jieba_count_total else " N/A"
    print(
        f"  {'TOTAL':<45} {total_segs:>5} {'':>5} "
        f"{total_ellipsis:>4} {total_telegram:>4} {total_dup:>4} "
        f"{avg_ir:>6} {avg_wi:>6} {avg_jr:>6} {avg_jp:>5}"
    )


def main():
    parser = argparse.ArgumentParser(description="翻译质量评估 (对比 GT)")
    parser.add_argument("--video", type=str, help="单个视频 ID")
    parser.add_argument("--all", action="store_true", help="测试全部 10 个视频")
    parser.add_argument("--compare-only", action="store_true", help="仅评估现有翻译，不重新翻译")
    parser.add_argument("--worst", type=int, default=5, help="每个视频展示最差 N 段")
    args = parser.parse_args()

    if not args.video and not args.all:
        parser.print_help()
        sys.exit(1)

    videos = ALL_VIDEOS if args.all else [args.video]

    # 加载 config (用于翻译模式)
    config = None
    if not args.compare_only:
        config_path = PROJECT_DIR / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                config = json.load(f)
            # 确保 two_pass 开启
            if "llm" not in config:
                config["llm"] = {}
            config["llm"]["two_pass"] = True
        else:
            print("ERROR: config.json not found")
            sys.exit(1)

    all_metrics = {}

    for vid in videos:
        # 加载或运行翻译
        if args.compare_only:
            segments = load_segments(vid)
            if segments is None:
                print(f"  [SKIP] {vid}: 无 segments_cache")
                continue
        else:
            segments = run_translation(vid, config)
            if segments is None:
                continue

        # 加载 GT
        gt_file = VIDEO_GT_MAP.get(vid)
        gt_entries = None
        if gt_file:
            gt_path = GT_DIR / gt_file
            if gt_path.exists():
                gt_entries = parse_srt(str(gt_path))

        # 评估
        metrics = evaluate_video(vid, segments, gt_entries)
        all_metrics[vid] = metrics
        print_report(vid, metrics, show_worst=args.worst)

    if len(all_metrics) > 1:
        print_summary(all_metrics)


if __name__ == "__main__":
    main()
