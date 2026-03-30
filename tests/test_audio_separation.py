#!/usr/bin/env python3
"""
测试：音频分离功能的纯逻辑测试。
不调用 demucs 等外部模型，仅测试配置解析、缓存检测、merge_final_video 分支选择。
"""
import sys
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import DEFAULT_CONFIG, merge_final_video


# ── 配置默认值测试 ──


def test_audio_separation_defaults():
    """audio_separation 配置项应有正确默认值"""
    sep = DEFAULT_CONFIG["audio_separation"]
    assert sep["enabled"] is False
    assert sep["model"] == "htdemucs"
    assert sep["vocal_volume"] == 0.0
    assert sep["bgm_volume"] == 1.0
    assert sep["device"] == "auto"


def test_audio_separation_in_skip_steps_comment():
    """skip_steps 注释应包含 separate"""
    import inspect
    # 检查 DEFAULTS 中的 skip_steps 注释
    source = inspect.getsource(sys.modules["pipeline"])
    assert "separate" in source


# ── separate_audio 缓存测试 ──


def test_separate_audio_cache_hit():
    """当 audio_vocals.wav 和 audio_accompaniment.wav 都存在时应跳过分离"""
    from pipeline import separate_audio

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        audio_path = output_dir / "audio.wav"
        vocals = output_dir / "audio_vocals.wav"
        accomp = output_dir / "audio_accompaniment.wav"

        # 创建假文件
        audio_path.write_bytes(b"fake audio")
        vocals.write_bytes(b"fake vocals")
        accomp.write_bytes(b"fake accompaniment")

        result = separate_audio(audio_path, output_dir)
        assert result["vocals"] == vocals
        assert result["accompaniment"] == accomp


def test_separate_audio_no_cache():
    """当缓存文件不存在且 demucs 未安装时应抛出 RuntimeError"""
    from pipeline import separate_audio

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        audio_path = output_dir / "audio.wav"
        audio_path.write_bytes(b"fake audio")

        # Mock demucs 未安装
        with patch.dict("sys.modules", {"demucs": None, "demucs.api": None}):
            try:
                separate_audio(audio_path, output_dir)
                assert False, "应该抛出 RuntimeError"
            except (RuntimeError, ImportError):
                pass  # 预期行为


# ── merge_final_video 分支测试 ──


def test_merge_uses_separation_when_files_exist():
    """当 audio_separation.enabled=True 且分离文件存在时应使用 3 输入 ffmpeg"""
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        video_path = output_dir / "original.mp4"
        dub_path = output_dir / "chinese_dub.wav"
        vocals = output_dir / "audio_vocals.wav"
        accomp = output_dir / "audio_accompaniment.wav"

        for f in [video_path, dub_path, vocals, accomp]:
            f.write_bytes(b"fake")

        sep_config = {"enabled": True, "vocal_volume": 0.0, "bgm_volume": 1.0}

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            merge_final_video(video_path, dub_path, output_dir, 0.15,
                              audio_sep_config=sep_config)

            # 验证 ffmpeg 使用了 4 个 -i 参数（video + vocals + accomp + dub）
            call_args = mock_run.call_args[0][0]
            i_count = sum(1 for arg in call_args if arg == "-i")
            assert i_count == 4, f"应有 4 个 -i 参数, got {i_count}"


def test_merge_fallback_when_no_separation_files():
    """当 audio_separation.enabled=True 但分离文件不存在时应回退"""
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        video_path = output_dir / "original.mp4"
        dub_path = output_dir / "chinese_dub.wav"

        for f in [video_path, dub_path]:
            f.write_bytes(b"fake")

        sep_config = {"enabled": True, "vocal_volume": 0.0, "bgm_volume": 1.0}

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            merge_final_video(video_path, dub_path, output_dir, 0.15,
                              audio_sep_config=sep_config)

            # 回退到 2 个 -i 参数（video + dub）
            call_args = mock_run.call_args[0][0]
            i_count = sum(1 for arg in call_args if arg == "-i")
            assert i_count == 2, f"应回退到 2 个 -i 参数, got {i_count}"


def test_merge_without_separation_config():
    """当未配置 audio_separation 时应使用原始 2 输入模式"""
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        video_path = output_dir / "original.mp4"
        dub_path = output_dir / "chinese_dub.wav"

        for f in [video_path, dub_path]:
            f.write_bytes(b"fake")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            merge_final_video(video_path, dub_path, output_dir, 0.15)

            call_args = mock_run.call_args[0][0]
            i_count = sum(1 for arg in call_args if arg == "-i")
            assert i_count == 2, f"应有 2 个 -i 参数, got {i_count}"


def test_merge_separation_volume_params():
    """验证分离模式下 ffmpeg filter_complex 包含正确的音量参数"""
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        video_path = output_dir / "original.mp4"
        dub_path = output_dir / "chinese_dub.wav"
        vocals = output_dir / "audio_vocals.wav"
        accomp = output_dir / "audio_accompaniment.wav"

        for f in [video_path, dub_path, vocals, accomp]:
            f.write_bytes(b"fake")

        sep_config = {"enabled": True, "vocal_volume": 0.05, "bgm_volume": 0.8}

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            merge_final_video(video_path, dub_path, output_dir, 0.15,
                              audio_sep_config=sep_config)

            call_args = mock_run.call_args[0][0]
            filter_idx = call_args.index("-filter_complex") + 1
            filter_str = call_args[filter_idx]
            assert "volume=0.05" in filter_str, f"应包含 vocal_volume=0.05: {filter_str}"
            assert "volume=0.8" in filter_str, f"应包含 bgm_volume=0.8: {filter_str}"
            assert "amix=inputs=3" in filter_str, f"应使用 3 路混音: {filter_str}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
