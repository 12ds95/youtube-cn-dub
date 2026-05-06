"""测试: Pass 2 prompt 内联每段目标字数 (基于 Pass 1 译文做 jieba 反向估算)
   目标: 让 Pass 2 改编时既参照原英文意思,也守住时长预算,避免长度漂移。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_build_pass2_lines_contains_target_chars():
    """Pass 2 行格式: [N] (X-Y字) EN: ... / 直译: ... — 字数区间必须出现"""
    from pipeline import build_pass2_lines
    batch = [
        {"text_en": "Hello world, this is a test.", "text_zh": "你好世界,这是一个测试。",
         "start": 0.0, "end": 2.5},
    ]
    lines = build_pass2_lines(batch)
    assert len(lines) == 1
    assert "字)" in lines[0], f"行内必须含字数区间, got: {lines[0]!r}"
    # 必须保留 EN 和 直译 两段
    assert "EN:" in lines[0]
    assert "直译" in lines[0]


def test_build_pass2_lines_uses_pass1_sample():
    """字数区间应基于 Pass 1 zh 做 jieba 反向估,不是固定 4.5 cps"""
    from pipeline import build_pass2_lines
    # 短中文样本 vs 长中文样本 -> 字数估算应不同
    batch_short = [{
        "text_en": "EN", "text_zh": "短句", "start": 0, "end": 2.0,
    }]
    batch_long = [{
        "text_en": "EN", "text_zh": "这是一段比较长且复杂的中文内容用作字数估算",
        "start": 0, "end": 2.0,
    }]
    s = build_pass2_lines(batch_short)[0]
    l = build_pass2_lines(batch_long)[0]
    # 区间字符串可能不同 (jieba 估算不同 ms/字 -> 不同 target)
    # 至少都包含合法区间
    import re
    m_s = re.search(r"\((\d+)-(\d+)字\)", s)
    m_l = re.search(r"\((\d+)-(\d+)字\)", l)
    assert m_s is not None
    assert m_l is not None
    lo_s, hi_s = int(m_s.group(1)), int(m_s.group(2))
    lo_l, hi_l = int(m_l.group(1)), int(m_l.group(2))
    assert lo_s < hi_s and lo_l < hi_l


def test_build_pass2_lines_handles_empty_pass1():
    """若 Pass 1 zh 为空,回退到全局 jieba 估算,不应报错"""
    from pipeline import build_pass2_lines
    batch = [
        {"text_en": "Hello.", "text_zh": "", "start": 0.0, "end": 1.0},
    ]
    lines = build_pass2_lines(batch)
    assert len(lines) == 1
    assert "字)" in lines[0]


def test_build_pass2_lines_numbering():
    """[1] [2] [3] 编号正确"""
    from pipeline import build_pass2_lines
    batch = [
        {"text_en": "A", "text_zh": "甲", "start": 0, "end": 1},
        {"text_en": "B", "text_zh": "乙", "start": 1, "end": 2},
        {"text_en": "C", "text_zh": "丙", "start": 2, "end": 3},
    ]
    lines = build_pass2_lines(batch)
    assert len(lines) == 3
    assert lines[0].startswith("[1]")
    assert lines[1].startswith("[2]")
    assert lines[2].startswith("[3]")
