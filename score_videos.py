#!/usr/bin/env python3
"""
视频配音质量评分工具

对管线产出进行自动化质量评分，量化 TTS 自然度和调速失真。

用法:
    ./venv/bin/python3 score_videos.py                        # 评分全部 3 个测试视频
    ./venv/bin/python3 score_videos.py output/zjMuIxRvygQ     # 评分单个视频
    ./venv/bin/python3 score_videos.py --gate                 # 质量门禁模式（超阈值退出 1）
    ./venv/bin/python3 score_videos.py --save-baseline        # 保存当前评分为基线
    ./venv/bin/python3 score_videos.py --compare              # 对比基线

指标:
    CPS (chars/sec)   — 中文字数 / TTS 实际时长，自然范围 3.5-6.0
    Atempo distortion  — 调速比均值和标准差，来自 speed_report.json
    UTMOS (可选)       — 神经网络 MOS 预测，需 pip install utmos
    Parselmouth (可选)  — 声学分析 (jitter/shimmer)，需 pip install praat-parselmouth
"""
import json
import os
import sys
import statistics
import subprocess
from pathlib import Path
from datetime import datetime

# ─── 配色 ─────────────────────────────────────────────────────────
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
CYAN = "\033[0;36m"
BOLD = "\033[1m"
NC = "\033[0m"

# ─── 阈值 ─────────────────────────────────────────────────────────
THRESHOLDS = {
    "cps_mean":    {"warn": 5.5, "fail": 6.5},
    "cps_p95":     {"warn": 6.5, "fail": 8.0},
    "atempo_mean": {"warn": 1.10, "fail": 1.20},
    "atempo_std":  {"warn": 0.06, "fail": 0.10},
    "utmos_mean":  {"warn": 3.5, "fail": 3.0, "direction": "lower_is_bad"},
    "jitter_mean": {"warn": 0.025, "fail": 0.050},
}

DEFAULT_VIDEO_DIRS = [
    "output/d4EgbgTm0Bg",
    "output/kCc8FmEb1nY",
    "output/zjMuIxRvygQ",
]


def _status(value, key):
    """返回 (icon, color) 基于阈值判断"""
    t = THRESHOLDS.get(key)
    if not t:
        return "  ", NC
    lower_bad = t.get("direction") == "lower_is_bad"
    if lower_bad:
        if value < t["fail"]:
            return "❌", RED
        if value < t["warn"]:
            return "⚠️ ", YELLOW
        return "✅", GREEN
    else:
        if value > t["fail"]:
            return "❌", RED
        if value > t["warn"]:
            return "⚠️ ", YELLOW
        return "✅", GREEN


def _zh_char_count(text: str) -> int:
    """统计中文字符数"""
    return sum(1 for c in text if '\u4e00' <= c <= '\u9fff')


# ═══════════════════════════════════════════════════════════════════
# CPS 评分 (零新依赖)
# ═══════════════════════════════════════════════════════════════════

def _batch_mp3_durations(mp3_paths: list) -> dict:
    """批量获取 mp3 文件时长（秒），使用 mutagen 读 MP3 头信息，极快"""
    from mutagen.mp3 import MP3
    durations = {}
    for mp3 in mp3_paths:
        try:
            audio = MP3(str(mp3))
            if audio.info and audio.info.length > 0:
                durations[str(mp3)] = audio.info.length
        except Exception:
            continue
    return durations


def compute_cps(video_dir: Path) -> dict:
    """计算每段 TTS 的 CPS (中文字/秒)"""
    cache = video_dir / "segments_cache.json"
    tts_dir = video_dir / "tts_segments"
    if not cache.exists() or not tts_dir.exists():
        return {"error": "segments_cache.json 或 tts_segments/ 不存在"}

    segments = json.loads(cache.read_text(encoding="utf-8"))

    # 收集需要测量的 mp3 路径
    candidates = []
    for i, seg in enumerate(segments):
        text_zh = seg.get("text_zh", "")
        zh_chars = _zh_char_count(text_zh)
        if zh_chars < 2:
            continue
        mp3 = tts_dir / f"seg_{i:04d}.mp3"
        if mp3.exists():
            candidates.append((i, zh_chars, mp3))

    if not candidates:
        return {"error": "无有效 TTS 片段"}

    # 批量获取时长
    mp3_paths = [c[2] for c in candidates]
    dur_map = _batch_mp3_durations(mp3_paths)

    details = []
    for i, zh_chars, mp3 in candidates:
        dur_sec = dur_map.get(str(mp3))
        if not dur_sec or dur_sec < 0.1:
            continue
        cps = zh_chars / dur_sec
        details.append({
            "idx": i,
            "zh_chars": zh_chars,
            "tts_dur_sec": round(dur_sec, 3),
            "cps": round(cps, 2),
        })

    if not details:
        return {"error": "无有效 TTS 片段"}

    cps_values = [d["cps"] for d in details]
    cps_values.sort()

    p95_idx = int(len(cps_values) * 0.95)
    p95 = cps_values[min(p95_idx, len(cps_values) - 1)]

    return {
        "mean": round(statistics.mean(cps_values), 2),
        "median": round(statistics.median(cps_values), 2),
        "p95": round(p95, 2),
        "std": round(statistics.stdev(cps_values), 3) if len(cps_values) > 1 else 0,
        "above_6_pct": round(sum(1 for c in cps_values if c > 6.0) / len(cps_values) * 100, 1),
        "above_7_pct": round(sum(1 for c in cps_values if c > 7.0) / len(cps_values) * 100, 1),
        "total_scored": len(details),
        "details": details,
    }


# ═══════════════════════════════════════════════════════════════════
# Atempo 失真评分 (读 speed_report.json)
# ═══════════════════════════════════════════════════════════════════

def compute_atempo(video_dir: Path) -> dict:
    """读取 speed_report.json 中的调速统计"""
    # 兼容新旧路径
    for candidate in [video_dir / "audit" / "speed_report.json",
                      video_dir / "speed_report.json"]:
        if candidate.exists():
            rpt = json.loads(candidate.read_text(encoding="utf-8"))
            total = rpt.get("total_segments", 1)
            clamped_fast = rpt.get("clamped_fast", 0)
            clamped_slow = rpt.get("clamped_slow", 0)
            return {
                "mean": rpt.get("avg_clamped", rpt.get("median_ratio", 0)),
                "std": rpt.get("std_clamped", 0),
                "std_raw": rpt.get("std_raw", 0),
                "outliers_gt_1.4": rpt.get("outliers_gt_1.4", 0),
                "clamped_fast": clamped_fast,
                "clamped_slow": clamped_slow,
                "pct_above_1.15": round(clamped_fast / total * 100, 1) if total else 0,
                "total_segments": total,
                "borrow_events": len(rpt.get("borrow_events", [])),
                "slowdown_rejected": rpt.get("slowdown_rejected", 0),
            }
    return {"error": "speed_report.json 不存在"}


# ═══════════════════════════════════════════════════════════════════
# UTMOS 评分 (可选)
# ═══════════════════════════════════════════════════════════════════

def compute_utmos(video_dir: Path, sample_size: int = 20) -> dict:
    """神经网络 MOS 预测，抽样评分"""
    try:
        import utmos
    except ImportError:
        return {"skipped": True, "reason": "utmos 未安装 (pip install utmos)"}

    tts_dir = video_dir / "tts_segments"
    if not tts_dir.exists():
        return {"error": "tts_segments/ 不存在"}

    mp3s = sorted(tts_dir.glob("seg_*.mp3"))
    if not mp3s:
        return {"error": "无 TTS 片段"}

    import random
    random.seed(42)
    sampled = random.sample(mp3s, min(sample_size, len(mp3s)))

    model = utmos.Score()
    scores = []
    for mp3 in sampled:
        try:
            score = model.calculate_wav_file(str(mp3))
            scores.append(round(score, 3))
        except Exception:
            continue

    if not scores:
        return {"error": "所有样本评分失败"}

    scores.sort()
    p5 = scores[max(0, int(len(scores) * 0.05))]

    return {
        "mean": round(statistics.mean(scores), 3),
        "median": round(statistics.median(scores), 3),
        "min": min(scores),
        "p5": round(p5, 3),
        "sampled": len(scores),
    }


# ═══════════════════════════════════════════════════════════════════
# Parselmouth 声学评分 (可选)
# ═══════════════════════════════════════════════════════════════════

def compute_prosody(video_dir: Path, sample_size: int = 20) -> dict:
    """Praat 声学分析: jitter, shimmer, F0"""
    try:
        import parselmouth
        from parselmouth.praat import call
    except ImportError:
        return {"skipped": True, "reason": "parselmouth 未安装 (pip install praat-parselmouth)"}

    tts_dir = video_dir / "tts_segments"
    if not tts_dir.exists():
        return {"error": "tts_segments/ 不存在"}

    mp3s = sorted(tts_dir.glob("seg_*.mp3"))
    if not mp3s:
        return {"error": "无 TTS 片段"}

    import random
    import numpy as np
    random.seed(42)
    sampled = random.sample(mp3s, min(sample_size, len(mp3s)))

    jitters, shimmers, f0s = [], [], []
    for mp3 in sampled:
        try:
            snd = parselmouth.Sound(str(mp3))
            # F0
            pitch = snd.to_pitch()
            f0_vals = pitch.selected_array["frequency"]
            f0_voiced = f0_vals[f0_vals > 0]
            if len(f0_voiced) < 3:
                continue
            f0s.append(float(np.mean(f0_voiced)))
            # Jitter & Shimmer
            pp = call(snd, "To PointProcess (periodic, cc)", 75, 500)
            jitter = call(pp, "Get jitter (local)", 0, 0, 0.0001, 0.02, 1.3)
            shimmer = call([snd, pp], "Get shimmer (local)", 0, 0, 0.0001, 0.02, 1.3, 1.6)
            if jitter == jitter and shimmer == shimmer:  # NaN check
                jitters.append(jitter)
                shimmers.append(shimmer)
        except Exception:
            continue

    if not jitters:
        return {"error": "声学分析失败"}

    return {
        "mean_jitter": round(statistics.mean(jitters), 4),
        "mean_shimmer": round(statistics.mean(shimmers), 4),
        "mean_f0": round(statistics.mean(f0s), 1) if f0s else 0,
        "f0_std": round(statistics.stdev(f0s), 1) if len(f0s) > 1 else 0,
        "sampled": len(jitters),
    }


# ═══════════════════════════════════════════════════════════════════
# 综合评分 + 输出
# ═══════════════════════════════════════════════════════════════════

def score_video(video_dir: Path) -> dict:
    """对单个视频目录做全面评分"""
    result = {
        "video_id": video_dir.name,
        "timestamp": datetime.now().isoformat(),
        "cps": compute_cps(video_dir),
        "atempo": compute_atempo(video_dir),
        "utmos": compute_utmos(video_dir),
        "prosody": compute_prosody(video_dir),
    }
    return result


def print_scores(scores: dict, gate_mode: bool = False) -> bool:
    """打印评分结果，返回是否全部通过"""
    vid = scores["video_id"]
    passed = True

    print(f"\n{'═' * 60}")
    print(f"{BOLD}📊 质量评分: {vid}{NC}")
    print(f"{'═' * 60}")

    # CPS
    cps = scores["cps"]
    if "error" in cps:
        print(f"\n  {RED}CPS: {cps['error']}{NC}")
        if gate_mode:
            passed = False
    else:
        print(f"\n  {BOLD}【CPS — 中文语速 (字/秒)】{NC}")
        for key, label in [("mean", "均值"), ("median", "中位数"),
                           ("p95", "P95"), ("std", "标准差")]:
            val = cps[key]
            tk = f"cps_{key}" if key in ("mean", "p95") else None
            icon, color = _status(val, tk) if tk else ("  ", NC)
            print(f"    {icon} {label}: {color}{val}{NC}")
        print(f"       >6.0 CPS: {cps['above_6_pct']}%")
        print(f"       >7.0 CPS: {cps['above_7_pct']}%")
        print(f"       评分段数: {cps['total_scored']}")

        if gate_mode:
            for key in ("mean", "p95"):
                val = cps[key]
                t = THRESHOLDS[f"cps_{key}"]
                if val > t["fail"]:
                    passed = False

    # Atempo
    atempo = scores["atempo"]
    if "error" in atempo:
        print(f"\n  {YELLOW}Atempo: {atempo['error']}{NC}")
    else:
        print(f"\n  {BOLD}【Atempo — 调速失真】{NC}")
        for key, label in [("mean", "均值"), ("std", "标准差"),
                           ("std_raw", "原始标准差")]:
            val = atempo[key]
            tk = f"atempo_{key}" if key in ("mean", "std") else None
            icon, color = _status(val, tk) if tk else ("  ", NC)
            print(f"    {icon} {label}: {color}{val}{NC}")
        print(f"       限速段: {atempo['clamped_fast']}/{atempo['total_segments']}"
              f" ({atempo['pct_above_1.15']}%)")
        print(f"       离群(>1.4x): {atempo['outliers_gt_1.4']}")
        if atempo.get("borrow_events"):
            print(f"       间隙借用: {atempo['borrow_events']} 次")

        if gate_mode:
            for key in ("mean", "std"):
                val = atempo[key]
                t = THRESHOLDS[f"atempo_{key}"]
                if val > t["fail"]:
                    passed = False

    # UTMOS
    utmos_s = scores["utmos"]
    if utmos_s.get("skipped"):
        print(f"\n  {CYAN}UTMOS: 跳过 ({utmos_s['reason']}){NC}")
    elif "error" in utmos_s:
        print(f"\n  {YELLOW}UTMOS: {utmos_s['error']}{NC}")
    else:
        print(f"\n  {BOLD}【UTMOS — 语音自然度 (1-5)】{NC}")
        for key, label in [("mean", "均值"), ("median", "中位数"),
                           ("min", "最低"), ("p5", "P5")]:
            val = utmos_s[key]
            tk = "utmos_mean" if key == "mean" else None
            icon, color = _status(val, tk) if tk else ("  ", NC)
            print(f"    {icon} {label}: {color}{val}{NC}")
        print(f"       抽样: {utmos_s['sampled']} 段")

        if gate_mode and utmos_s["mean"] < THRESHOLDS["utmos_mean"]["fail"]:
            passed = False

    # Prosody
    prosody = scores["prosody"]
    if prosody.get("skipped"):
        print(f"\n  {CYAN}声学: 跳过 ({prosody['reason']}){NC}")
    elif "error" in prosody:
        print(f"\n  {YELLOW}声学: {prosody['error']}{NC}")
    else:
        print(f"\n  {BOLD}【声学 — Jitter/Shimmer/F0】{NC}")
        jv = prosody["mean_jitter"]
        icon, color = _status(jv, "jitter_mean")
        print(f"    {icon} Jitter: {color}{jv:.4f}{NC} ({jv*100:.2f}%)")
        print(f"       Shimmer: {prosody['mean_shimmer']:.4f}"
              f" ({prosody['mean_shimmer']*100:.2f}%)")
        print(f"       Mean F0: {prosody['mean_f0']} Hz"
              f" (std: {prosody['f0_std']})")
        print(f"       抽样: {prosody['sampled']} 段")

        if gate_mode and jv > THRESHOLDS["jitter_mean"]["fail"]:
            passed = False

    return passed


def save_scores_json(scores: dict, video_dir: Path):
    """保存评分到 audit/quality_scores.json"""
    audit = video_dir / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    # 去掉 details 避免文件过大
    save_data = {k: v for k, v in scores.items()}
    if isinstance(save_data.get("cps"), dict):
        save_data["cps"] = {k: v for k, v in save_data["cps"].items() if k != "details"}
    out_path = audit / "quality_scores.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)


def save_baseline(scores: dict, video_dir: Path):
    """保存当前评分为基线"""
    audit = video_dir / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    # 精简数据
    baseline = {
        "timestamp": scores["timestamp"],
        "video_id": scores["video_id"],
    }
    try:
        result = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, cwd=str(video_dir.parent.parent))
        baseline["git_commit"] = result.stdout.strip()
    except Exception:
        pass
    for category in ("cps", "atempo", "utmos", "prosody"):
        data = scores.get(category, {})
        if "error" in data or data.get("skipped"):
            continue
        baseline[category] = {k: v for k, v in data.items() if k != "details"}
    out_path = audit / "baseline_scores.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2)
    print(f"  💾 基线已保存: {out_path}")


def print_comparison(scores: dict, video_dir: Path):
    """对比基线打印 delta"""
    baseline_path = video_dir / "audit" / "baseline_scores.json"
    if not baseline_path.exists():
        return
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    print(f"\n  {BOLD}📈 vs 基线 ({baseline.get('git_commit', '?')}"
          f" @ {baseline.get('timestamp', '?')[:10]}){NC}")

    comparisons = [
        ("cps", "mean", "CPS mean", False),
        ("cps", "p95", "CPS p95", False),
        ("atempo", "mean", "Atempo mean", False),
        ("atempo", "std", "Atempo std", False),
    ]
    for cat, key, label, lower_bad in comparisons:
        old = baseline.get(cat, {}).get(key)
        new = scores.get(cat, {}).get(key)
        if old is None or new is None or "error" in scores.get(cat, {}):
            continue
        delta = new - old
        if abs(delta) < 0.001:
            arrow = "→"
            color = NC
        elif (delta < 0) != lower_bad:
            arrow = "↓" if delta < 0 else "↑"
            color = GREEN
        else:
            arrow = "↑" if delta > 0 else "↓"
            color = RED
        print(f"    {arrow} {label}: {old} → {color}{new}{NC}"
              f" ({'+' if delta > 0 else ''}{delta:.3f})")


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="视频配音质量评分")
    parser.add_argument("video_dirs", nargs="*", help="视频输出目录 (默认: 全部测试视频)")
    parser.add_argument("--gate", action="store_true", help="质量门禁模式")
    parser.add_argument("--save-baseline", action="store_true", help="保存当前评分为基线")
    parser.add_argument("--compare", action="store_true", help="对比基线")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()

    # 确定项目根目录
    script_dir = Path(__file__).resolve().parent
    os.chdir(script_dir)

    dirs = args.video_dirs if args.video_dirs else DEFAULT_VIDEO_DIRS
    dirs = [Path(d) for d in dirs]

    all_passed = True
    all_scores = []

    for video_dir in dirs:
        if not video_dir.exists():
            print(f"{RED}❌ 目录不存在: {video_dir}{NC}")
            all_passed = False
            continue

        scores = score_video(video_dir)
        all_scores.append(scores)

        if args.json:
            save_data = {k: v for k, v in scores.items()}
            if isinstance(save_data.get("cps"), dict):
                save_data["cps"] = {k: v for k, v in save_data["cps"].items() if k != "details"}
            print(json.dumps(save_data, ensure_ascii=False, indent=2))
        else:
            passed = print_scores(scores, gate_mode=args.gate)
            if not passed:
                all_passed = False
            if args.compare or (not args.save_baseline):
                print_comparison(scores, video_dir)

        save_scores_json(scores, video_dir)

        if args.save_baseline:
            save_baseline(scores, video_dir)

    if not args.json:
        print(f"\n{'═' * 60}")
        if args.gate:
            if all_passed:
                print(f"{GREEN}✅ 质量门禁: 全部通过{NC}")
            else:
                print(f"{RED}❌ 质量门禁: 未通过{NC}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
