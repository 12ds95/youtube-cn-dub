#!/usr/bin/env python3
"""
质量评分测试 — 验证 TTS 产出的自然度和调速失真

借鉴来源:
  - IWSLT 2025: CPS (chars/sec) 作为等时翻译合规度指标
  - VoiceMOS Challenge: UTMOS 自动 MOS 预测
  - Praat: jitter/shimmer 声学自然度分析

测试逻辑:
  读取 output/VIDEO_ID/ 下的实际产出（segments_cache.json + tts_segments/），
  计算 CPS、atempo 失真等指标，断言在自然范围内。
"""
import os
import sys
import json
import pytest
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUTPUT_DIR = ROOT / "output"
VIDEO_IDS = ["d4EgbgTm0Bg", "kCc8FmEb1nY", "zjMuIxRvygQ"]


def _has_tts(video_id):
    tts_dir = OUTPUT_DIR / video_id / "tts_segments"
    return tts_dir.exists() and any(tts_dir.glob("seg_*.mp3"))


def _has_speed_report(video_id):
    vdir = OUTPUT_DIR / video_id
    return ((vdir / "audit" / "speed_report.json").exists()
            or (vdir / "speed_report.json").exists())


# ═══════════════════════════════════════════════════════════════════
# CPS 测试
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("video_id", VIDEO_IDS)
def test_cps_mean_within_natural_range(video_id):
    """CPS 均值应在自然中文语速范围 (≤ 6.5)"""
    if not _has_tts(video_id):
        pytest.skip(f"{video_id}: 无 TTS 片段")

    from score_videos import compute_cps
    result = compute_cps(OUTPUT_DIR / video_id)
    if "error" in result:
        pytest.skip(result["error"])

    mean_cps = result["mean"]
    print(f"  CPS mean={mean_cps}, median={result['median']}, "
          f"p95={result['p95']}, scored={result['total_scored']}")
    assert mean_cps <= 6.5, f"CPS 均值 {mean_cps} 超过 6.5 (不自然)"
    assert mean_cps >= 1.5, f"CPS 均值 {mean_cps} 低于 1.5 (异常慢)"


@pytest.mark.parametrize("video_id", VIDEO_IDS)
def test_cps_p95_within_ceiling(video_id):
    """CPS P95 应 ≤ 8.0 (允许少量快速段)"""
    if not _has_tts(video_id):
        pytest.skip(f"{video_id}: 无 TTS 片段")

    from score_videos import compute_cps
    result = compute_cps(OUTPUT_DIR / video_id)
    if "error" in result:
        pytest.skip(result["error"])

    p95 = result["p95"]
    print(f"  CPS p95={p95}, >7.0 占比={result['above_7_pct']}%")
    assert p95 <= 8.0, f"CPS P95 {p95} 超过 8.0 上限"


@pytest.mark.parametrize("video_id", VIDEO_IDS)
def test_no_extreme_cps_segments(video_id):
    """不应有超过 7.0 CPS 的段 (>7 CPS 占比 < 5%)"""
    if not _has_tts(video_id):
        pytest.skip(f"{video_id}: 无 TTS 片段")

    from score_videos import compute_cps
    result = compute_cps(OUTPUT_DIR / video_id)
    if "error" in result:
        pytest.skip(result["error"])

    above_7 = result["above_7_pct"]
    print(f"  >7.0 CPS: {above_7}%")
    assert above_7 < 5.0, f"{above_7}% 段超过 7.0 CPS"


# ═══════════════════════════════════════════════════════════════════
# Atempo 失真测试
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("video_id", VIDEO_IDS)
def test_atempo_mean_acceptable(video_id):
    """Atempo 均值 ≤ 1.20 (无严重调速)"""
    if not _has_speed_report(video_id):
        pytest.skip(f"{video_id}: 无 speed_report.json")

    from score_videos import compute_atempo
    result = compute_atempo(OUTPUT_DIR / video_id)
    if "error" in result:
        pytest.skip(result["error"])

    mean = result["mean"]
    print(f"  Atempo mean={mean}, std={result['std']}, "
          f"clamped_fast={result['clamped_fast']}/{result['total_segments']}")
    assert mean <= 1.20, f"Atempo 均值 {mean} 超过 1.20"


@pytest.mark.parametrize("video_id", VIDEO_IDS)
def test_atempo_std_acceptable(video_id):
    """Atempo 标准差 < 0.10 (调速一致性)"""
    if not _has_speed_report(video_id):
        pytest.skip(f"{video_id}: 无 speed_report.json")

    from score_videos import compute_atempo
    result = compute_atempo(OUTPUT_DIR / video_id)
    if "error" in result:
        pytest.skip(result["error"])

    std = result["std"]
    print(f"  Atempo std={std}")
    assert std < 0.10, f"Atempo 标准差 {std} 超过 0.10"


# ═══════════════════════════════════════════════════════════════════
# UTMOS 测试 (可选依赖)
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("video_id", VIDEO_IDS)
def test_utmos_above_threshold(video_id):
    """UTMOS MOS 均值 ≥ 3.0"""
    pytest.importorskip("utmos", reason="utmos 未安装")
    if not _has_tts(video_id):
        pytest.skip(f"{video_id}: 无 TTS 片段")

    from score_videos import compute_utmos
    result = compute_utmos(OUTPUT_DIR / video_id)
    if "error" in result or result.get("skipped"):
        pytest.skip(result.get("error", result.get("reason", "unknown")))

    mean_mos = result["mean"]
    print(f"  UTMOS mean={mean_mos}, min={result['min']}, sampled={result['sampled']}")
    assert mean_mos >= 3.0, f"UTMOS 均值 {mean_mos} 低于 3.0"


# ═══════════════════════════════════════════════════════════════════
# Parselmouth 声学测试 (可选依赖)
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("video_id", VIDEO_IDS)
def test_jitter_within_range(video_id):
    """Jitter < 5% (TTS 语音容许范围)"""
    pytest.importorskip("parselmouth", reason="parselmouth 未安装")
    if not _has_tts(video_id):
        pytest.skip(f"{video_id}: 无 TTS 片段")

    from score_videos import compute_prosody
    result = compute_prosody(OUTPUT_DIR / video_id)
    if "error" in result or result.get("skipped"):
        pytest.skip(result.get("error", result.get("reason", "unknown")))

    jitter = result["mean_jitter"]
    print(f"  Jitter={jitter:.4f} ({jitter*100:.2f}%), "
          f"Shimmer={result['mean_shimmer']:.4f}, F0={result['mean_f0']}Hz")
    assert jitter < 0.05, f"Jitter {jitter:.4f} ({jitter*100:.2f}%) 超过 5%"


@pytest.mark.parametrize("video_id", VIDEO_IDS)
def test_f0_in_human_range(video_id):
    """Mean F0 应在人类语音范围 (80-400 Hz)"""
    pytest.importorskip("parselmouth", reason="parselmouth 未安装")
    if not _has_tts(video_id):
        pytest.skip(f"{video_id}: 无 TTS 片段")

    from score_videos import compute_prosody
    result = compute_prosody(OUTPUT_DIR / video_id)
    if "error" in result or result.get("skipped"):
        pytest.skip(result.get("error", result.get("reason", "unknown")))

    f0 = result["mean_f0"]
    print(f"  Mean F0={f0} Hz (std={result['f0_std']})")
    assert 80 < f0 < 400, f"Mean F0 {f0} Hz 超出人类语音范围"
