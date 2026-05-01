#!/usr/bin/env python3
"""
测试 atempo 滤镜构建逻辑。

来源: open-dubbing 项目 — FFmpeg 音频变速测试模式
       (speed clamping, cascaded filters for extreme ratios)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import _build_atempo_filter


def test_normal_speed():
    """普通加速: 单个 atempo 滤镜"""
    assert _build_atempo_filter(1.0) == "atempo=1.0000"
    assert _build_atempo_filter(1.2) == "atempo=1.2000"
    assert _build_atempo_filter(0.8) == "atempo=0.8000"


def test_exactly_half():
    """0.5 不需要级联"""
    result = _build_atempo_filter(0.5)
    assert result == "atempo=0.5000"


def test_cascade_below_half():
    """速度 < 0.5 时需要级联多个 atempo=0.5 滤镜 (FFmpeg 限制)"""
    # 0.25 = 0.5 * 0.5 → atempo=0.5,atempo=0.5000
    result = _build_atempo_filter(0.25)
    parts = result.split(",")
    assert len(parts) == 2, f"应级联 2 个滤镜, got: {result}"
    assert parts[0] == "atempo=0.5"
    # 复合后 = 0.5 * 0.5 = 0.25
    product = 1.0
    for p in parts:
        val = float(p.replace("atempo=", ""))
        product *= val
    assert abs(product - 0.25) < 0.001


def test_cascade_very_slow():
    """极慢速度 0.125 = 0.5^3 → 三级级联"""
    result = _build_atempo_filter(0.125)
    parts = result.split(",")
    assert len(parts) == 3, f"应级联 3 个滤镜, got: {result}"
    product = 1.0
    for p in parts:
        val = float(p.replace("atempo=", ""))
        product *= val
    assert abs(product - 0.125) < 0.001


def test_cap_at_100():
    """超高速度应被 cap 到 100"""
    result = _build_atempo_filter(200.0)
    assert "100.0000" in result


def test_roundtrip_accuracy():
    """级联滤镜复合后的速度应精确回到原始值
    (参考 open-dubbing 的 speed rounding 测试)"""
    for speed in [0.3, 0.4, 0.15, 0.6, 0.75, 1.5, 2.0]:
        result = _build_atempo_filter(speed)
        product = 1.0
        for p in result.split(","):
            product *= float(p.replace("atempo=", ""))
        assert abs(product - min(speed, 100.0)) < 0.01, \
            f"speed={speed}: filter={result}, product={product}"


if __name__ == "__main__":
    print("atempo 滤镜测试:")
    test_normal_speed()
    print("  ✅ normal_speed")
    test_exactly_half()
    print("  ✅ exactly_half")
    test_cascade_below_half()
    print("  ✅ cascade_below_half")
    test_cascade_very_slow()
    print("  ✅ cascade_very_slow")
    test_cap_at_100()
    print("  ✅ cap_at_100")
    test_roundtrip_accuracy()
    print("  ✅ roundtrip_accuracy")
    print("  全部通过")
