"""测试: TTS 截断防御契约。
关键: speed_needed > 1.35 时 atempo 兜底必须运行 (而非走截断), 截断仅当 ffmpeg 失败。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_atempo_fallback_always_attempted():
    """pipeline.py 中 atempo 兜底必须无条件 try, 不再用 if speed_needed <= 1.35 卫语句"""
    import pipeline as p
    src = open(p.__file__).read()
    # 旧逻辑标记: 'if speed_needed <= 1.35:'
    assert "if speed_needed <= 1.35:" not in src, \
        "速度阈值卫语句仍存在; atempo 必须无条件兜底"


def test_max_atempo_fallback_configurable():
    """alignment.max_atempo_fallback 应可配置, 默认 1.5"""
    import pipeline as p
    src = open(p.__file__).read()
    assert "max_atempo_fallback" in src
    assert 'max_atempo_fallback", 1.5' in src


def test_truncate_has_fadeout():
    """截断路径必须 fade_out, 避免断崖式听感"""
    import pipeline as p
    src = open(p.__file__).read()
    # 截断分支应配 fade_out
    assert "fade_out(min(80, target_dur // 4))" in src or \
           "fade_out(fade_ms)" in src, "截断必须 fade_out 平滑"
