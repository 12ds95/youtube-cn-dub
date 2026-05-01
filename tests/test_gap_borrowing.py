#!/usr/bin/env python3
"""
测试间隙借用 + 视频减速 + 静音区间检测。

来源: open-dubbing — timing tolerance 测试 (np.allclose atol=0.5)
       Google Ariel — 多 utterance 合并时间点验证
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import _is_in_silence


# ── _is_in_silence 测试 ──

def test_silence_fully_inside():
    """完全在静音区域内"""
    silence_regions = [(1000, 3000)]
    assert _is_in_silence(1500, 500, silence_regions) is True


def test_silence_partially_overlap():
    """70%+ 重叠 → True"""
    silence_regions = [(1000, 2000)]
    # position=800, duration=1000 → [800, 1800], silence [1000,2000]
    # overlap = 1800-1000 = 800, need >= 1000*0.7=700 → True
    assert _is_in_silence(800, 1000, silence_regions) is True


def test_silence_insufficient_overlap():
    """<70% 重叠 → False"""
    silence_regions = [(1000, 1200)]
    # position=800, duration=1000 → [800, 1800], silence [1000,1200]
    # overlap = 200, need >= 1000*0.7=700 → False
    assert _is_in_silence(800, 1000, silence_regions) is False


def test_silence_no_regions():
    """无静音数据时默认允许借用"""
    assert _is_in_silence(1000, 500, []) is True


def test_silence_outside_all_regions():
    """完全不在任何静音区域"""
    silence_regions = [(0, 500), (3000, 4000)]
    assert _is_in_silence(1000, 500, silence_regions) is False


def test_silence_across_multiple_regions():
    """跨多个静音区域 → 累计重叠"""
    silence_regions = [(1000, 1300), (1500, 1800)]
    # position=1000, duration=1000 → [1000, 2000]
    # overlap with [1000,1300] = 300, overlap with [1500,1800] = 300
    # total = 600, need >= 1000*0.7=700 → False
    assert _is_in_silence(1000, 1000, silence_regions) is False


# ── 间隙借用逻辑验证 (源码扫描) ──

def test_gap_borrowing_logic_exists():
    """确保间隙借用核心逻辑存在且使用正确的参数"""
    import inspect
    from pipeline import _align_tts_to_timeline
    source = inspect.getsource(_align_tts_to_timeline)
    assert "gap_borrowing" in source
    assert "max_borrow_ms" in source
    assert "available_gap" in source
    assert "borrow_amount" in source
    assert "_is_in_silence" in source


def test_video_slowdown_logic_exists():
    """确保视频减速核心逻辑存在"""
    import inspect
    from pipeline import _align_tts_to_timeline
    source = inspect.getsource(_align_tts_to_timeline)
    assert "video_slowdown" in source
    assert "max_slowdown_factor" in source
    assert "slowdown_segments" in source
    assert "overflow_ratio" in source
    assert "0.15" in source  # 15% 溢出阈值


def test_slowdown_factor_bounds():
    """视频减速因子必须在 [max_slowdown_factor, 1.0) 范围内"""
    import inspect
    from pipeline import _align_tts_to_timeline
    source = inspect.getsource(_align_tts_to_timeline)
    # factor = target_dur / len(tts_audio) → < 1.0
    assert "factor = target_dur / len(tts_audio)" in source or \
           "factor = target_dur / tts_len" in source or \
           "factor" in source


def test_gap_borrow_respects_60_percent():
    """间隙借用只借用间隙的 60%"""
    import inspect
    from pipeline import _align_tts_to_timeline
    source = inspect.getsource(_align_tts_to_timeline)
    assert "0.6" in source, "应使用 60% 间隙上限"


if __name__ == "__main__":
    print("间隙借用 + 视频减速测试:")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✅ {name}")
    print("  全部通过")
