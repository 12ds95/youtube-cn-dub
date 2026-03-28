#!/usr/bin/env python3
"""
测试：字符数估算语速比 _estimate_speed_ratios。
来源：流程重构——迭代优化阶段改用字符估算取代 TTS 实测。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_estimate_basic():
    """基本估算：中文字符越多，ratio 越高"""
    from pipeline import _estimate_speed_ratios
    segments = [
        {"start": 0, "end": 2, "text_en": "Hi", "text_zh": "你好"},           # 2字 / 2s
        {"start": 2, "end": 4, "text_en": "Hello world", "text_zh": "你好世界朋友们大家好"},  # 8字 / 2s
    ]
    results = _estimate_speed_ratios(segments, threshold=1.5)
    assert results[0]["speed_ratio"] < results[1]["speed_ratio"], \
        "更长的中文应该有更高的 speed_ratio"
    # 2字/2秒 ratio ~0.28 → underslow; 8字/2秒 ratio ~1.38 → ok
    assert results[0]["status"] in ("underslow", "ok"), \
        f"短文本状态应为 underslow 或 ok, got {results[0]['status']}"


def test_estimate_overfast():
    """明显超速的长翻译"""
    from pipeline import _estimate_speed_ratios
    segments = [
        # 30 个中文字符 / 2 秒窗口 → ~30*275/2000 ≈ 4.1x
        {"start": 0, "end": 2, "text_en": "Short",
         "text_zh": "这是一段非常非常非常长的中文翻译文本用来测试超速检测功能"},
    ]
    results = _estimate_speed_ratios(segments, threshold=1.5)
    assert results[0]["status"] == "overfast", \
        f"30字/2秒应该是 overfast, got {results[0]['status']} (ratio={results[0]['speed_ratio']})"


def test_estimate_underslow():
    """明显过短的翻译"""
    from pipeline import _estimate_speed_ratios
    segments = [
        # 2 个中文字符 / 10 秒窗口 → ~2*275/10000 ≈ 0.055x
        {"start": 0, "end": 10, "text_en": "A very long English sentence here",
         "text_zh": "你好"},
    ]
    results = _estimate_speed_ratios(segments, threshold=1.5)
    assert results[0]["status"] == "underslow", \
        f"2字/10秒应该是 underslow, got {results[0]['status']} (ratio={results[0]['speed_ratio']})"


def test_estimate_mixed_text():
    """中英混合文本的估算"""
    from pipeline import _estimate_speed_ratios
    segments = [
        {"start": 0, "end": 5, "text_en": "3x3 matrix",
         "text_zh": "3x3矩阵可用于描述三维变换"},
    ]
    results = _estimate_speed_ratios(segments, threshold=1.5)
    # 混合文本应该有合理的 ratio（不是 0）
    assert results[0]["speed_ratio"] > 0, "混合文本的 ratio 应该大于 0"
    assert results[0]["estimated_ms"] > 0, "估算时长应该大于 0"


def test_estimate_empty_text():
    """空文本应该被跳过"""
    from pipeline import _estimate_speed_ratios
    segments = [
        {"start": 0, "end": 5, "text_en": "Hello", "text_zh": ""},
    ]
    results = _estimate_speed_ratios(segments, threshold=1.5)
    assert results[0]["status"] == "skipped"


def test_refinement_uses_estimate_not_tts():
    """确认 run_refinement_loop 使用 _estimate_speed_ratios 而非 _measure_speed_ratios"""
    import inspect
    from pipeline import run_refinement_loop
    source = inspect.getsource(run_refinement_loop)
    assert '_estimate_speed_ratios' in source, \
        "run_refinement_loop 应该使用 _estimate_speed_ratios"
    # _measure_speed_ratios 不应在非注释行中出现
    for line in source.split('\n'):
        stripped = line.strip()
        if '_measure_speed_ratios' in stripped and not stripped.startswith('#'):
            assert False, f"run_refinement_loop 仍在使用 _measure_speed_ratios: {stripped}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  \u2705 {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  \u274c {t.__name__}: {e}")
            failed += 1
    icon = '\u2705' if failed == 0 else '\u274c'
    print(f"\n{icon} {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
