#!/usr/bin/env python3
"""
测试语音一致性优化：全局语速基线计算与平滑。
来源：devlog/2026-03-28-five-todos-implementation.md (TODO2)
"""
import statistics


def _compute_smoothed_ratios(raw_ratios, blend_weight=0.4, smooth_alpha=0.3):
    """复现 pipeline.py _align_tts_to_timeline 中的平滑逻辑"""
    valid_ratios = [r for r in raw_ratios if r is not None and r > 0]
    if valid_ratios:
        sorted_ratios = sorted(valid_ratios)
        median_ratio = sorted_ratios[len(sorted_ratios) // 2]
    else:
        median_ratio = 1.0

    blended = []
    for r in raw_ratios:
        if r is None:
            blended.append(None)
        else:
            blended.append(r * (1 - blend_weight) + median_ratio * blend_weight)

    smoothed = list(blended)
    prev_valid = None
    for i, r in enumerate(smoothed):
        if r is not None:
            if prev_valid is not None:
                smoothed[i] = smooth_alpha * prev_valid + (1 - smooth_alpha) * r
            prev_valid = smoothed[i]

    return smoothed, median_ratio


def test_median_baseline():
    """全局基线应为有效比率的中位数"""
    raw = [0.8, 1.0, 1.2, 1.5, 2.0]
    _, median = _compute_smoothed_ratios(raw)
    assert median == 1.2, f"Expected 1.2, got {median}"
    print("  ✅ test_median_baseline")


def test_blend_toward_median():
    """混合后的比率应更接近中位数"""
    raw = [0.5, 1.0, 2.0]  # median=1.0
    smoothed, median = _compute_smoothed_ratios(raw, blend_weight=0.4, smooth_alpha=0.0)
    assert median == 1.0
    # 0.5 * 0.6 + 1.0 * 0.4 = 0.7
    assert abs(smoothed[0] - 0.7) < 0.01, f"Expected ~0.7, got {smoothed[0]}"
    # 2.0 * 0.6 + 1.0 * 0.4 = 1.6
    assert abs(smoothed[2] - 1.6) < 0.01, f"Expected ~1.6, got {smoothed[2]}"
    print("  ✅ test_blend_toward_median")


def test_smoothing_reduces_variance():
    """平滑后的方差应小于原始方差"""
    raw = [0.7, 1.5, 0.8, 1.4, 0.9, 1.3, 0.7, 1.5]
    smoothed, _ = _compute_smoothed_ratios(raw)
    valid_smoothed = [r for r in smoothed if r is not None]
    raw_var = statistics.variance(raw)
    smooth_var = statistics.variance(valid_smoothed)
    assert smooth_var < raw_var, f"Smoothed variance {smooth_var:.4f} not < raw {raw_var:.4f}"
    print(f"  ✅ test_smoothing_reduces_variance (raw={raw_var:.4f} → smooth={smooth_var:.4f})")


def test_none_handling():
    """None 值（跳过的片段）不应影响计算"""
    raw = [1.0, None, 1.2, None, 0.8]
    smoothed, median = _compute_smoothed_ratios(raw)
    assert smoothed[1] is None
    assert smoothed[3] is None
    assert smoothed[0] is not None
    assert smoothed[2] is not None
    print("  ✅ test_none_handling")


def test_all_none():
    """全部 None 时应默认基线为 1.0"""
    raw = [None, None, None]
    smoothed, median = _compute_smoothed_ratios(raw)
    assert median == 1.0
    assert all(r is None for r in smoothed)
    print("  ✅ test_all_none")


if __name__ == "__main__":
    print("语音一致性（平滑）测试:")
    test_median_baseline()
    test_blend_toward_median()
    test_smoothing_reduces_variance()
    test_none_handling()
    test_all_none()
    print("  全部通过")
