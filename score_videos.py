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

# 国内环境: UTMOSv2 依赖 facebook/wav2vec2-base，走镜像避免被墙
if "HF_ENDPOINT" not in os.environ:
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

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
    "iso_compliance": {"warn": 50.0, "fail": 40.0, "direction": "lower_is_bad"},
    "no_atempo_compliance": {"warn": 60.0, "fail": 40.0, "direction": "lower_is_bad"},
    "raw_ratio_std": {"warn": 0.15, "fail": 0.25},
}

# ─── 回归检测阈值 ─────────────────────────────────────────────
# 指标恶化超过 warn% → 警告，超过 fail% → 失败
REGRESSION_WARN_PCT = 15  # 15% 恶化 → WARN
REGRESSION_FAIL_PCT = 30  # 30% 恶化 → FAIL

# 用于回归比较的核心指标: (category, key, direction)
# direction: "higher_is_bad" 表示值上升=恶化, "lower_is_bad" 表示值下降=恶化
REGRESSION_METRICS = [
    ("cps",     "mean",                   "CPS 均值",      "higher_is_bad"),
    ("cps",     "p95",                    "CPS P95",       "higher_is_bad"),
    ("cps",     "isometric_compliance_pct", "等时合规率",    "lower_is_bad"),
    ("atempo",  "mean",                   "Atempo 均值",    "higher_is_bad"),
    ("atempo",  "std",                    "Atempo 标准差",  "higher_is_bad"),
    ("naturalness", "no_atempo_compliance_pct", "无调速合规率", "lower_is_bad"),
    ("naturalness", "raw_ratio_std",      "原始时长比标准差", "higher_is_bad"),
    ("utmos",   "mean",                   "UTMOS 均值",     "lower_is_bad"),
    ("prosody", "mean_jitter",            "Jitter 均值",    "higher_is_bad"),
]

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
        "isometric_compliance_pct": round(
            sum(1 for c in cps_values if 3.5 <= c <= 6.0) / len(cps_values) * 100, 1),
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
# Speed Naturalness 评分 — 衡量不需要 atempo 的程度
# ═══════════════════════════════════════════════════════════════════

def compute_speed_naturalness(video_dir: Path) -> dict:
    """评估语速自然度 — TTS 原音速匹配时间窗的程度。

    从 speed_report.json 读取数据，计算:
    - no_atempo_compliance_pct: raw_ratio 在 [0.85, 1.15] 的段占比
    - raw_ratio_mean/std: 原始时长比的均值/标准差
    - atempo_disabled: 是否已禁用 atempo
    """
    for candidate in [video_dir / "audit" / "speed_report.json",
                      video_dir / "speed_report.json"]:
        if candidate.exists():
            rpt = json.loads(candidate.read_text(encoding="utf-8"))
            atempo_disabled = rpt.get("atempo_disabled", False)
            result = {
                "atempo_disabled": atempo_disabled,
            }

            # 新模式直接从 speed_report 读取
            if atempo_disabled:
                result.update({
                    "no_atempo_compliance_pct": rpt.get("raw_ratio_within_115_pct", 0),
                    "raw_ratio_mean": rpt.get("raw_ratio_mean", rpt.get("baseline", 0)),
                    "raw_ratio_std": rpt.get("std_raw", 0),
                    "padded": rpt.get("padded", 0),
                    "truncated": rpt.get("truncated", 0),
                    "atempo_fallback": rpt.get("atempo_fallback", 0),
                    "within_tolerance": rpt.get("within_tolerance", 0),
                    "overflow_tolerance": rpt.get("overflow_tolerance", 0.10),
                    "total_segments": rpt.get("total_segments", 0),
                })
            else:
                # 旧模式: 从 raw std 推算
                result.update({
                    "no_atempo_compliance_pct": 0,  # 旧模式无此数据
                    "raw_ratio_mean": rpt.get("baseline", 0),
                    "raw_ratio_std": rpt.get("std_raw", 0),
                    "total_segments": rpt.get("total_segments", 0),
                })
            return result
    return {"error": "speed_report.json 不存在"}


# ═══════════════════════════════════════════════════════════════════
# UTMOS 评分 (可选)
# ═══════════════════════════════════════════════════════════════════

def compute_utmos(video_dir: Path, sample_size: int = 20) -> dict:
    """神经网络 MOS 预测，抽样评分（UTMOSv2）"""
    try:
        import utmosv2
    except ImportError:
        return {"skipped": True, "reason": "utmosv2 未安装 (pip install git+https://github.com/sarulab-speech/UTMOSv2.git)"}

    tts_dir = video_dir / "tts_segments"
    if not tts_dir.exists():
        return {"error": "tts_segments/ 不存在"}

    mp3s = sorted(tts_dir.glob("seg_*.mp3"))
    if not mp3s:
        return {"error": "无 TTS 片段"}

    import random
    random.seed(42)
    sampled = random.sample(mp3s, min(sample_size, len(mp3s)))

    try:
        import warnings
        warnings.filterwarnings("ignore", message=".*gradient_checkpointing.*")
        warnings.filterwarnings("ignore", message=".*torch\\.load.*")
        model = utmosv2.create_model(pretrained=True)
    except Exception as e:
        msg = str(e).split('\n')[0][:80]  # 只取首行，截断长错误
        return {"error": f"模型加载失败: {msg}"}

    scores = []
    for mp3 in sampled:
        try:
            score = model.predict(input_path=str(mp3))
            scores.append(round(float(score), 3))
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
        "naturalness": compute_speed_naturalness(video_dir),
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
        compliance = cps.get('isometric_compliance_pct', 0)
        icon_c, color_c = _status(compliance, "iso_compliance")
        print(f"    {icon_c} 合规率 [3.5-6.0]: {color_c}{compliance}%{NC}")
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

    # Speed Naturalness
    nat = scores.get("naturalness", {})
    if "error" in nat:
        print(f"\n  {YELLOW}自然度: {nat['error']}{NC}")
    else:
        mode = "无atempo" if nat.get("atempo_disabled") else "atempo模式"
        print(f"\n  {BOLD}【语速自然度 — {mode}】{NC}")
        compliance = nat.get("no_atempo_compliance_pct", 0)
        icon_c, color_c = _status(compliance, "no_atempo_compliance")
        print(f"    {icon_c} 无调速合规率 [0.85-1.15]: {color_c}{compliance}%{NC}")
        raw_mean = nat.get("raw_ratio_mean", 0)
        print(f"       原始时长比均值: {raw_mean:.4f} (理想=1.0)")
        raw_std = nat.get("raw_ratio_std", 0)
        icon_s, color_s = _status(raw_std, "raw_ratio_std")
        print(f"    {icon_s} 原始时长比标准差: {color_s}{raw_std}{NC} (理想→0)")
        if nat.get("atempo_disabled"):
            print(f"       填充:{nat.get('padded', 0)}"
                  f" 容忍:{nat.get('within_tolerance', 0)}"
                  f" atempo降级:{nat.get('atempo_fallback', 0)}"
                  f" 截断:{nat.get('truncated', 0)}")
        print(f"       总段数: {nat.get('total_segments', 0)}")

        if gate_mode:
            if compliance < THRESHOLDS["no_atempo_compliance"]["fail"]:
                passed = False
            if raw_std > THRESHOLDS["raw_ratio_std"]["fail"]:
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
    for category in ("cps", "atempo", "naturalness", "utmos", "prosody"):
        data = scores.get(category, {})
        if "error" in data or data.get("skipped"):
            continue
        baseline[category] = {k: v for k, v in data.items() if k != "details"}
    out_path = audit / "baseline_scores.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2)
    print(f"  💾 基线已保存: {out_path}")


def _compute_regression(old_val, new_val, direction):
    """计算回归百分比。正值=恶化，负值=改善。"""
    if old_val is None or new_val is None:
        return None
    # 避免除零
    if abs(old_val) < 1e-9:
        return 0.0
    delta = new_val - old_val
    if direction == "lower_is_bad":
        # 值下降=恶化，所以 regression = -delta/old (下降为正回归)
        pct = -delta / abs(old_val) * 100
    else:
        # 值上升=恶化，所以 regression = delta/old
        pct = delta / abs(old_val) * 100
    return pct


def check_regression(scores: dict, video_dir: Path) -> tuple:
    """
    检查当前评分是否相对基线有回归。

    返回 (passed, warnings, failures)
      - passed: True 如果无 FAIL 级回归
      - warnings: [(label, old, new, pct)] 列表
      - failures: [(label, old, new, pct)] 列表
    """
    baseline_path = video_dir / "audit" / "baseline_scores.json"
    if not baseline_path.exists():
        return True, [], []

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    warnings = []
    failures = []

    for cat, key, label, direction in REGRESSION_METRICS:
        old_val = baseline.get(cat, {}).get(key)
        new_data = scores.get(cat, {})
        if isinstance(new_data, dict) and ("error" in new_data or new_data.get("skipped")):
            continue
        new_val = new_data.get(key) if isinstance(new_data, dict) else None
        if old_val is None or new_val is None:
            continue

        regression_pct = _compute_regression(old_val, new_val, direction)
        if regression_pct is None:
            continue

        if regression_pct >= REGRESSION_FAIL_PCT:
            failures.append((label, old_val, new_val, regression_pct))
        elif regression_pct >= REGRESSION_WARN_PCT:
            warnings.append((label, old_val, new_val, regression_pct))

    passed = len(failures) == 0
    return passed, warnings, failures


def print_comparison(scores: dict, video_dir: Path) -> bool:
    """对比基线打印 delta 和回归检测结果。返回回归检测是否通过。"""
    baseline_path = video_dir / "audit" / "baseline_scores.json"
    if not baseline_path.exists():
        return True
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    print(f"\n  {BOLD}📈 vs 基线 ({baseline.get('git_commit', '?')}"
          f" @ {baseline.get('timestamp', '?')[:10]}){NC}")

    # 打印所有指标的 delta
    for cat, key, label, direction in REGRESSION_METRICS:
        old = baseline.get(cat, {}).get(key)
        new_data = scores.get(cat, {})
        if isinstance(new_data, dict) and ("error" in new_data or new_data.get("skipped")):
            continue
        new = new_data.get(key) if isinstance(new_data, dict) else None
        if old is None or new is None:
            continue
        delta = new - old
        regression_pct = _compute_regression(old, new, direction)

        # 方向判断: 改善=绿色, 恶化=红色
        lower_bad = (direction == "lower_is_bad")
        if abs(delta) < 0.001:
            arrow = "→"
            color = NC
            severity = ""
        elif (delta < 0) != lower_bad:
            # 改善
            arrow = "↓" if delta < 0 else "↑"
            color = GREEN
            severity = ""
        else:
            # 恶化
            arrow = "↑" if delta > 0 else "↓"
            if regression_pct is not None and regression_pct >= REGRESSION_FAIL_PCT:
                color = RED
                severity = f" {RED}[FAIL: +{regression_pct:.1f}%]{NC}"
            elif regression_pct is not None and regression_pct >= REGRESSION_WARN_PCT:
                color = YELLOW
                severity = f" {YELLOW}[WARN: +{regression_pct:.1f}%]{NC}"
            else:
                color = RED
                severity = ""
        print(f"    {arrow} {label}: {old} → {color}{new}{NC}"
              f" ({'+' if delta > 0 else ''}{delta:.3f}){severity}")

    # 回归检测汇总
    passed, warnings, failures = check_regression(scores, video_dir)
    if failures:
        print(f"\n    {RED}🚨 回归检测: {len(failures)} 项 FAIL{NC}")
        for label, old, new, pct in failures:
            print(f"      ❌ {label}: {old} → {new} (恶化 {pct:.1f}%)")
    if warnings:
        print(f"\n    {YELLOW}⚠️  回归检测: {len(warnings)} 项 WARN{NC}")
        for label, old, new, pct in warnings:
            print(f"      ⚠️  {label}: {old} → {new} (恶化 {pct:.1f}%)")
    if not failures and not warnings:
        print(f"\n    {GREEN}✅ 回归检测: 无恶化{NC}")

    return passed


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
                regression_passed = print_comparison(scores, video_dir)
                if args.gate and not regression_passed:
                    all_passed = False

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
