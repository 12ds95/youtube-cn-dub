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


# ═══════════════════════════════════════════════════════════════════
# 回归检测测试
# ═══════════════════════════════════════════════════════════════════

class TestComputeRegression:
    """_compute_regression 单元测试"""

    def test_higher_is_bad_worsening(self):
        from score_videos import _compute_regression
        # CPS mean: 5.0 → 6.0 = 20% 恶化
        pct = _compute_regression(5.0, 6.0, "higher_is_bad")
        assert abs(pct - 20.0) < 0.1

    def test_higher_is_bad_improving(self):
        from score_videos import _compute_regression
        # CPS mean: 6.0 → 5.0 = -16.7% (改善)
        pct = _compute_regression(6.0, 5.0, "higher_is_bad")
        assert pct < 0  # 负值=改善

    def test_lower_is_bad_worsening(self):
        from score_videos import _compute_regression
        # UTMOS: 4.0 → 3.0 = 25% 恶化
        pct = _compute_regression(4.0, 3.0, "lower_is_bad")
        assert abs(pct - 25.0) < 0.1

    def test_lower_is_bad_improving(self):
        from score_videos import _compute_regression
        # UTMOS: 3.0 → 4.0 = 改善
        pct = _compute_regression(3.0, 4.0, "lower_is_bad")
        assert pct < 0  # 负值=改善

    def test_no_change(self):
        from score_videos import _compute_regression
        pct = _compute_regression(5.0, 5.0, "higher_is_bad")
        assert abs(pct) < 0.01

    def test_none_values(self):
        from score_videos import _compute_regression
        assert _compute_regression(None, 5.0, "higher_is_bad") is None
        assert _compute_regression(5.0, None, "higher_is_bad") is None

    def test_zero_baseline(self):
        from score_videos import _compute_regression
        # 基线为零时应返回 0 避免除零
        pct = _compute_regression(0.0, 5.0, "higher_is_bad")
        assert pct == 0.0


class TestCheckRegression:
    """check_regression 集成测试 (使用 mock 基线)"""

    def _make_baseline(self, tmp_path, **overrides):
        """创建模拟基线文件"""
        baseline = {
            "timestamp": "2025-01-01T00:00:00",
            "video_id": "test",
            "git_commit": "abc123",
            "cps": {"mean": 5.0, "p95": 6.5, "median": 4.8, "std": 0.5,
                    "above_6_pct": 10.0, "above_7_pct": 2.0, "total_scored": 100},
            "atempo": {"mean": 1.05, "std": 0.04},
        }
        baseline.update(overrides)
        audit = tmp_path / "audit"
        audit.mkdir(parents=True, exist_ok=True)
        with open(audit / "baseline_scores.json", "w") as f:
            json.dump(baseline, f)
        return tmp_path

    def test_no_baseline_passes(self, tmp_path):
        from score_videos import check_regression
        passed, warnings, failures = check_regression({"cps": {"mean": 9.0}}, tmp_path)
        assert passed is True
        assert warnings == []
        assert failures == []

    def test_no_regression(self, tmp_path):
        from score_videos import check_regression
        video_dir = self._make_baseline(tmp_path)
        scores = {
            "cps": {"mean": 5.0, "p95": 6.5},
            "atempo": {"mean": 1.05, "std": 0.04},
        }
        passed, warnings, failures = check_regression(scores, video_dir)
        assert passed is True
        assert len(warnings) == 0
        assert len(failures) == 0

    def test_warn_level_regression(self, tmp_path):
        from score_videos import check_regression, REGRESSION_WARN_PCT
        video_dir = self._make_baseline(tmp_path)
        # CPS mean: 5.0 → 5.8 = 16% 恶化 → WARN
        scores = {
            "cps": {"mean": 5.8, "p95": 6.5},
            "atempo": {"mean": 1.05, "std": 0.04},
        }
        passed, warnings, failures = check_regression(scores, video_dir)
        assert passed is True  # WARN 不影响通过
        assert len(warnings) >= 1
        assert any("CPS" in w[0] for w in warnings)

    def test_fail_level_regression(self, tmp_path):
        from score_videos import check_regression, REGRESSION_FAIL_PCT
        video_dir = self._make_baseline(tmp_path)
        # CPS mean: 5.0 → 7.0 = 40% 恶化 → FAIL
        scores = {
            "cps": {"mean": 7.0, "p95": 6.5},
            "atempo": {"mean": 1.05, "std": 0.04},
        }
        passed, warnings, failures = check_regression(scores, video_dir)
        assert passed is False
        assert len(failures) >= 1

    def test_skipped_metrics_ignored(self, tmp_path):
        from score_videos import check_regression
        video_dir = self._make_baseline(tmp_path)
        scores = {
            "cps": {"mean": 5.0, "p95": 6.5},
            "atempo": {"mean": 1.05, "std": 0.04},
            "utmos": {"skipped": True, "reason": "not installed"},
            "prosody": {"error": "analysis failed"},
        }
        passed, warnings, failures = check_regression(scores, video_dir)
        assert passed is True

    def test_multiple_regressions(self, tmp_path):
        from score_videos import check_regression
        video_dir = self._make_baseline(tmp_path)
        # CPS mean +40%, atempo std +50% → both FAIL
        scores = {
            "cps": {"mean": 7.0, "p95": 6.5},
            "atempo": {"mean": 1.05, "std": 0.06},
        }
        passed, warnings, failures = check_regression(scores, video_dir)
        assert passed is False
        assert len(failures) >= 2
