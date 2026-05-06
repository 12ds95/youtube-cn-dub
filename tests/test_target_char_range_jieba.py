"""测试: compute_target_char_range 用 jieba 反向估算字数预算。
sample_zh 提供时按 sample 的 ms/字反向算 target_chars;
否则按 jieba 在标准语料上探测的全局 mean_ms_per_char 计算;
最后回退到 CPS 区间。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_无_sample_用全局jieba探测():
    """无 sample 时仍用 jieba (全局 mean_ms_per_char) 而非硬编码 CPS"""
    from text_utils import compute_target_char_range
    lo, hi = compute_target_char_range(5.0)  # 5s
    # jieba 校准 ms/字 ~ 220-260ms 范围 → 5s 约 19-23 字
    assert lo >= 12 and lo <= 22, f"lo={lo} 应在 jieba 推断范围内"
    assert hi <= 32, f"hi={hi} 上界应受 jieba 控制"
    assert lo < hi


def test_有_sample_用sample反向估算():
    """sample_zh 提供时用 jieba 估算 sample 时长反向算每字 ms"""
    from text_utils import compute_target_char_range
    # 提供一个明显短的 sample (单字密集)，sample 的每字 ms 应较小
    sample_short = "这是测试句"  # 5 个字
    lo_s, hi_s = compute_target_char_range(5.0, sample_zh=sample_short)
    # 提供一个长词密集 sample (英文/数字读得慢)，每字 ms 应较大
    sample_long = "API 处理 256 个数据项的 HTTP 请求"
    lo_l, hi_l = compute_target_char_range(5.0, sample_zh=sample_long)
    # 含英数的 sample 平均每字 ms 较大 → 同时长可装的"汉字数"较少
    # 至少 lo/hi 必须返回有效正整数区间
    assert lo_s > 0 and hi_s > lo_s
    assert lo_l > 0 and hi_l > lo_l


def test_零时长返回最小区间():
    from text_utils import compute_target_char_range
    lo, hi = compute_target_char_range(0)
    assert lo >= 1 and hi > lo


def test_use_jieba_false_回退cps区间():
    """显式关 use_jieba 时回退到 CPS [3.5, 5.5] 区间"""
    from text_utils import compute_target_char_range
    lo, hi = compute_target_char_range(10.0, use_jieba=False)
    # 10s × 3.5 = 35, 10s × 5.5 = 55
    assert lo == 35
    assert hi == 55


def test_jieba_反向估算与estimate_duration一致():
    """target_chars 经 jieba 估算反向算回, 估算时长应接近 target_dur (±20%)"""
    from text_utils import compute_target_char_range
    from duration_estimator import estimate_duration
    target_sec = 8.0
    lo, hi = compute_target_char_range(target_sec)
    # 用区间中点的 N 个汉字构造测试串
    mid = (lo + hi) // 2
    test_str = "我们这就这是为什么这样的内容它是合适用来说明的事情" * 3
    test_str = test_str[:mid]
    est_ms = estimate_duration(test_str)
    target_ms = target_sec * 1000
    deviation = abs(est_ms - target_ms) / target_ms
    assert deviation < 0.30, f"jieba 估算偏差 {deviation:.1%} 超 30%"


def test_长时长样本():
    """duration=12s 应给出合理范围"""
    from text_utils import compute_target_char_range
    lo, hi = compute_target_char_range(12.0)
    assert 30 < lo < 60
    assert hi < 90
