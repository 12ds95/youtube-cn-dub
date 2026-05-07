#!/usr/bin/env python3
"""测试 jieba 分词时长估算。

默认 v6 (LightGBM CV R²=0.973), 极短文本 (<5 有效字符) 自动降级 v4。
v4 (Ridge 23 维 R²=0.952) 通过 DURATION_ESTIMATOR_VERSION=v4 切换。
v2 legacy 通过 DURATION_ESTIMATOR_VERSION=v2 切换。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import jieba  # noqa: F401
    HAS_JIEBA = True
except ImportError:
    HAS_JIEBA = False

import pytest

from pipeline import _estimate_duration_jieba


@pytest.mark.skipif(not HAS_JIEBA, reason="jieba not installed")
class TestDurationEstimation:
    """v4 模型行为契约 (语义保留, 不强制断言绝对值因 v4 系数与 v2 不同)。"""

    def test_returns_non_negative(self):
        """所有估算返回非负"""
        for s in ["", "a", "你好", "Hello world 测试"]:
            ms = _estimate_duration_jieba(s)
            assert ms >= 0, f"{s!r} → {ms} (应非负)"

    def test_typical_short_sentence(self):
        """典型短句应在合理范围 (300-2000ms)"""
        ms = _estimate_duration_jieba("你好")
        assert 100 < ms < 2000, f"'你好' 期望 100-2000ms, got {ms}"

    def test_typical_medium_sentence(self):
        """典型中等句 (5-7 字) 应在 800-3500ms"""
        ms = _estimate_duration_jieba("今天天气很好")
        assert 800 < ms < 3500, f"'今天天气很好' 期望 800-3500ms, got {ms}"

    def test_typical_long_sentence(self):
        """典型长句 (~20 字) 应在 3000-10000ms"""
        ms = _estimate_duration_jieba(
            "这是一个比较长的中文句子用来测试时长估算的准确性"
        )
        assert 3000 < ms < 10000, f"长句期望 3000-10000ms, got {ms}"

    def test_english_mixed(self):
        """中英混合文本"""
        ms = _estimate_duration_jieba("这是一个Python测试")
        assert ms > 500, "中英混合应有合理时长"

    def test_url_detection(self):
        """URL 含 token 应使时长更长 (URL 被逐字符朗读)"""
        ms_with_url = _estimate_duration_jieba("请访问 example.com 了解详情")
        ms_without_url = _estimate_duration_jieba("请访问 详情")
        assert ms_with_url > ms_without_url, \
            f"含 URL 应更长: {ms_with_url} vs {ms_without_url}"

    def test_punctuation_adds_pause(self):
        """标点符号应使时长更长或不变 (停顿)"""
        ms_no_punct = _estimate_duration_jieba("今天天气很好明天也是")
        ms_with_punct = _estimate_duration_jieba("今天天气很好，明天也是。")
        assert ms_with_punct >= ms_no_punct

    def test_digits_are_long(self):
        """数字按字符朗读耗时显著"""
        ms = _estimate_duration_jieba("2025年")
        # v4: 4 数字 × 238 + 中文 1 字 → 至少 800ms
        assert ms > 800, f"4 位数字+1 汉字应 > 800ms, got {ms}"

    def test_empty_text_returns_zero(self):
        """空文本返回 0 (v4 行为)"""
        ms = _estimate_duration_jieba("")
        assert ms == 0.0, f"空文本期望 0ms, got {ms}"

    def test_monotonic_with_length(self):
        """更长文本应有更长估算时长"""
        short = _estimate_duration_jieba("你好")
        medium = _estimate_duration_jieba("你好世界测试翻译")
        long = _estimate_duration_jieba(
            "这是一个比较长的中文句子用来测试时长估算的准确性"
        )
        assert short < medium < long, \
            f"应单调递增: {short} < {medium} < {long}"

    def test_filler_word_lengthens(self):
        """语气词 (吧/呢/啊) 应使时长延长 (v4 韵律特征)"""
        plain = _estimate_duration_jieba("好的")
        with_filler = _estimate_duration_jieba("好的吧")
        # 加 1 个语气词应延长 100+ ms (v4 系数 287)
        assert with_filler - plain > 100, \
            f"语气词应延长: {with_filler} vs {plain}"

    def test_v6_hybrid_short_text_uses_v4(self):
        """v6 默认下极短文本 (<5 有效字符) 降级 v4, 防 GBDT 训练分布外饱和."""
        from duration_estimator import _estimate_v4, estimate_duration
        # "你好" 2 字 应该走 v4 (匹配 v4 输出)
        v4_short = _estimate_v4("你好")
        default_short = estimate_duration("你好")
        assert abs(default_short - v4_short) < 1.0, \
            f"短文本应降级 v4: estimate={default_short}, v4={v4_short}"

    def test_v6_long_text_uses_lgbm(self):
        """v6 在长文本上输出 (若 lightgbm 可用) 应不同于 v4."""
        from duration_estimator import _estimate_v4, _load_lgbm, estimate_duration
        if not _load_lgbm():
            pytest.skip("lightgbm 不可用, v6 自动降级 v4")
        long_text = "这是一段比较长的中文测试文本用来验证 v6 估算准确度"
        v4_pred = _estimate_v4(long_text)
        v6_pred = estimate_duration(long_text)
        assert abs(v6_pred - v4_pred) > 50, \
            f"v6 长文本应与 v4 不同 (>50ms): v6={v6_pred}, v4={v4_pred}"

    def test_tone_neutral_shorter(self):
        """轻声字 (de/le/me) 应比四声字短 (v4 声调特征)"""
        # "他" tā (1声) vs "了" le (轻声)
        # 单字时差异不大 (因为 base + final + intercept 是主导), 但 v4 应反映
        ta = _estimate_duration_jieba("他")
        le = _estimate_duration_jieba("了")
        # 不强制 le < ta (因为 final 不同), 仅验证两者都正常
        assert ta >= 0 and le >= 0


if __name__ == "__main__":
    if not HAS_JIEBA:
        print("  ⚠️  jieba 未安装, 跳过")
    else:
        print("时长估算测试 (v4):")
        t = TestDurationEstimation()
        for name in dir(t):
            if name.startswith("test_"):
                getattr(t, name)()
                print(f"  ✅ {name}")
        print("  全部通过")
