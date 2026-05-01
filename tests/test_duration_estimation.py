#!/usr/bin/env python3
"""
测试 jieba 分词时长估算。

来源: VideoLingo — AdvancedSyllableEstimator (per-language syllable duration)
       Google Ariel — assertAlmostEqual 精度验证
       open-dubbing — 已知时长 → 计算速度比 测试模式
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import jieba
    HAS_JIEBA = True
except ImportError:
    HAS_JIEBA = False

import pytest

from pipeline import _estimate_duration_jieba


@pytest.mark.skipif(not HAS_JIEBA, reason="jieba not installed")
class TestDurationEstimation:

    def test_single_char_word(self):
        """单字词 ~200ms"""
        ms = _estimate_duration_jieba("的")
        assert 100 < ms < 400, f"单字词期望 ~200ms, got {ms}"

    def test_two_char_word(self):
        """双字词 ~380ms"""
        ms = _estimate_duration_jieba("今天")
        assert 200 < ms < 600, f"双字词期望 ~380ms, got {ms}"

    def test_four_char_word(self):
        """四字词 ~600ms (4*150)"""
        ms = _estimate_duration_jieba("人工智能")
        assert 400 < ms < 900, f"四字词期望 ~600ms, got {ms}"

    def test_sentence_reasonable_range(self):
        """一句话的估算应在合理范围内
        '今天天气很好' ≈ 6 字 → 大约 1-2 秒"""
        ms = _estimate_duration_jieba("今天天气很好")
        assert 800 < ms < 3000, f"6字句子期望 1-2s, got {ms}ms"

    def test_english_mixed(self):
        """中英混合文本"""
        ms = _estimate_duration_jieba("这是一个Python测试")
        assert ms > 500, "中英混合应有合理时长"

    def test_url_detection(self):
        """URL 逐字母朗读: 每字符 280ms
        (参考 VideoLingo 的特殊字符处理)"""
        ms_with_url = _estimate_duration_jieba("请访问 example.com 了解详情")
        ms_without_url = _estimate_duration_jieba("请访问了解详情")
        assert ms_with_url > ms_without_url + 1000, \
            "包含 URL 的文本应显著更长"

    def test_punctuation_adds_pause(self):
        """标点符号增加停顿 (50ms/个)"""
        ms_no_punct = _estimate_duration_jieba("今天天气很好明天也是")
        ms_with_punct = _estimate_duration_jieba("今天天气很好，明天也是。")
        # 标点停顿小但应存在
        assert ms_with_punct >= ms_no_punct

    def test_digits(self):
        """数字: ~120ms/字符"""
        ms = _estimate_duration_jieba("2025年")
        assert ms > 400, "4位数字+1汉字应 > 400ms"

    def test_empty_text(self):
        """空文本返回 0 或接近 0"""
        ms = _estimate_duration_jieba("")
        assert ms < 100

    def test_monotonic_with_length(self):
        """更长的文本应有更长的估算时长
        (open-dubbing 的 duration 单调性测试模式)"""
        short = _estimate_duration_jieba("你好")
        medium = _estimate_duration_jieba("你好世界测试翻译")
        long = _estimate_duration_jieba("这是一个比较长的中文句子用来测试时长估算的准确性")
        assert short < medium < long, \
            f"应单调递增: {short} < {medium} < {long}"


if __name__ == "__main__":
    if not HAS_JIEBA:
        print("  ⚠️  jieba 未安装, 跳过")
    else:
        print("时长估算测试:")
        t = TestDurationEstimation()
        for name in dir(t):
            if name.startswith("test_"):
                getattr(t, name)()
                print(f"  ✅ {name}")
        print("  全部通过")
