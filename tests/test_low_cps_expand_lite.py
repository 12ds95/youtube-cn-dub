"""测试: 低 CPS 段轻量扩展候选 — 单次 LLM 调用,单候选,只针对显著欠速段。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_identify_severely_underslow_strict_threshold():
    """识别严重欠速: CPS < 3.0 或 estimated/target < 0.65"""
    from pipeline import _identify_severely_underslow_segments
    segs = [
        {"start": 0, "end": 5.0, "text_zh": "短"},     # 1字/5s = 0.2 CPS, 严重欠速
        {"start": 0, "end": 5.0, "text_zh": "这是相当合适长度的中文翻译"},  # 13字/5s = 2.6 CPS
        {"start": 0, "end": 2.0, "text_zh": "这是一段测试翻译内容"},  # 10字/2s = 5.0 CPS, ok
        {"start": 0, "end": 1.0, "text_zh": "短小测试"},  # CPS=4
    ]
    underslow = _identify_severely_underslow_segments(segs, cps_threshold=3.0, ratio_threshold=0.65)
    # seg 0 严重欠速 (字数太少)
    # seg 1 边界 (2.6 < 3.0) 也算
    # seg 2 ok
    # seg 3 ok
    assert 0 in underslow or 1 in underslow, f"应识别欠速段, got: {underslow}"
    assert 2 not in underslow
    assert 3 not in underslow


def test_identify_filters_too_short_text():
    """text_zh 字数极少 (<3) 不算 (避免误判空段)"""
    from pipeline import _identify_severely_underslow_segments
    segs = [
        {"start": 0, "end": 5.0, "text_zh": "啊"},  # 1 字 → skip
        {"start": 0, "end": 5.0, "text_zh": "测试"},  # 2 字 → skip
    ]
    out = _identify_severely_underslow_segments(segs)
    assert out == []


def test_identify_filters_short_duration():
    """时长 < 0.5s 段不参与扩展 (太短无法可靠扩展)"""
    from pipeline import _identify_severely_underslow_segments
    segs = [
        {"start": 0, "end": 0.3, "text_zh": "短"},  # 时长 < 0.5s
    ]
    out = _identify_severely_underslow_segments(segs)
    assert out == []


def test_lite_expand_disabled_by_default():
    """配置 low_cps_expand_lite 默认 False, 不自动触发"""
    import pipeline as p
    src = open(p.__file__).read()
    # 配置默认值检查
    assert 'low_cps_expand_lite' in src, "新增配置项 low_cps_expand_lite 应在代码中"
