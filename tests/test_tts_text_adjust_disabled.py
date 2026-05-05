"""测试: alignment.pre_tts_text_adjust / llm_text_loop / refine.post_tts_calibration
开关默认关闭, 与 isometric=0 共同保证 TTS 阶段不再以时长为目标改写译文。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_pre_tts_text_adjust_default_off():
    """alignment.pre_tts_text_adjust 默认 False 时 Phase 3 预检改写不触发。
    这是个 config 契约测试 — 检查 pipeline.py 确实读这个 key。
    """
    import pipeline as p
    src = open(p.__file__).read()
    # 入口处必须包含 pre_tts_text_adjust 开关读取
    assert "pre_tts_text_adjust" in src
    # 默认值应当是 False
    assert 'pre_tts_text_adjust", False' in src


def test_llm_text_loop_default_off():
    """alignment.llm_text_loop 默认 False 时 _llm_duration_feedback 直接 return。"""
    import pipeline as p
    src = open(p.__file__).read()
    assert "llm_text_loop" in src
    assert 'llm_text_loop", False' in src


def test_unit_grouping_default_on():
    """unit_grouping.enabled 默认 True (sentence-unit 流水线核心)。"""
    import pipeline as p
    src = open(p.__file__).read()
    assert 'unit_grouping' in src
    assert 'unit_grouping", {}).get("enabled", True' in src
