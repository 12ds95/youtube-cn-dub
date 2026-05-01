#!/usr/bin/env python3
"""
测试 TTS 速度测量 + 速度报告分类。

来源: open-dubbing — 已知时长音频 → 速度比计算 → 断言精确值
       Google Ariel — assertAlmostEqual 精度验证 + in-memory WAV 生成
"""
import sys, os, tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from pydub import AudioSegment as PydubSegment
    HAS_PYDUB = True
except ImportError:
    HAS_PYDUB = False

import pytest

from pipeline import _measure_speed_ratios


def _create_test_audio(path: Path, duration_ms: int):
    """创建指定时长的测试 mp3 文件 (Ariel in-memory audio 模式)"""
    silence = PydubSegment.silent(duration=duration_ms, frame_rate=16000)
    silence.export(str(path), format="mp3")


@pytest.mark.skipif(not HAS_PYDUB, reason="pydub not installed")
class TestSpeedMeasurement:

    def test_exact_match(self):
        """TTS 时长 == 目标时长 → ratio ≈ 1.0"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tts_dir = Path(tmpdir)
            _create_test_audio(tts_dir / "seg_0000.mp3", 2000)
            segments = [{"start": 0.0, "end": 2.0, "text_en": "test", "text_zh": "测试"}]
            results = _measure_speed_ratios(segments, tts_dir)
            assert len(results) == 1
            assert abs(results[0]["speed_ratio"] - 1.0) < 0.1

    def test_overfast_classification(self):
        """TTS 远长于目标 → status=overfast"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tts_dir = Path(tmpdir)
            _create_test_audio(tts_dir / "seg_0000.mp3", 4000)
            segments = [{"start": 0.0, "end": 2.0, "text_en": "test", "text_zh": "测试"}]
            results = _measure_speed_ratios(segments, tts_dir, threshold=1.5)
            assert results[0]["status"] == "overfast"
            assert results[0]["speed_ratio"] > 1.5

    def test_underslow_classification(self):
        """TTS 远短于目标 → status=underslow"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tts_dir = Path(tmpdir)
            _create_test_audio(tts_dir / "seg_0000.mp3", 500)
            segments = [{"start": 0.0, "end": 3.0, "text_en": "test", "text_zh": "测试"}]
            results = _measure_speed_ratios(segments, tts_dir)
            assert results[0]["status"] == "underslow"
            assert results[0]["speed_ratio"] < 0.7

    def test_missing_tts_file(self):
        """TTS 文件不存在 → status=skipped"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tts_dir = Path(tmpdir)
            segments = [{"start": 0.0, "end": 2.0, "text_en": "test", "text_zh": "测试"}]
            results = _measure_speed_ratios(segments, tts_dir)
            assert results[0]["status"] == "skipped"
            assert results[0]["skip_reason"] == "no_tts"

    def test_zero_duration_segment(self):
        """目标时长为 0 → status=skipped"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tts_dir = Path(tmpdir)
            _create_test_audio(tts_dir / "seg_0000.mp3", 1000)
            segments = [{"start": 5.0, "end": 5.0, "text_en": "test", "text_zh": "测试"}]
            results = _measure_speed_ratios(segments, tts_dir)
            assert results[0]["status"] == "skipped"

    def test_multiple_segments(self):
        """多段混合: ok + overfast + skipped"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tts_dir = Path(tmpdir)
            _create_test_audio(tts_dir / "seg_0000.mp3", 2000)
            _create_test_audio(tts_dir / "seg_0001.mp3", 5000)
            # seg_0002 故意不创建
            segments = [
                {"start": 0.0, "end": 2.0, "text_en": "a", "text_zh": "甲"},
                {"start": 2.0, "end": 4.0, "text_en": "b", "text_zh": "乙"},
                {"start": 4.0, "end": 6.0, "text_en": "c", "text_zh": "丙"},
            ]
            results = _measure_speed_ratios(segments, tts_dir)
            assert len(results) == 3
            statuses = [r["status"] for r in results]
            assert "ok" in statuses or "overfast" in statuses
            assert statuses[2] == "skipped"

    def test_result_fields(self):
        """结果应包含所有必要字段 (Ariel model validation 模式)"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tts_dir = Path(tmpdir)
            _create_test_audio(tts_dir / "seg_0000.mp3", 2000)
            segments = [{"start": 0.0, "end": 2.0, "text_en": "test", "text_zh": "测试"}]
            results = _measure_speed_ratios(segments, tts_dir)
            r = results[0]
            required_fields = {"idx", "speed_ratio", "tts_ms", "target_ms", "status"}
            assert required_fields.issubset(set(r.keys())), \
                f"缺少字段: {required_fields - set(r.keys())}"


if __name__ == "__main__":
    if not HAS_PYDUB:
        print("  ⚠️  pydub 未安装, 跳过")
    else:
        print("速度测量测试:")
        t = TestSpeedMeasurement()
        for name in sorted(dir(t)):
            if name.startswith("test_"):
                getattr(t, name)()
                print(f"  ✅ {name}")
        print("  全部通过")
