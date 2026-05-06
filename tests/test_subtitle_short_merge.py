"""测试: 字幕短行合并 — 切完后若末尾产生 <min_chars 的短行,
   合并到前一行 (或重切),避免字幕只显示零碎尾巴。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_lead_short_line_merged_into_next():
    """开头出现 ≤4 字短行时合并到下一行 (real case: '举个例子，...' 4 字)"""
    from pipeline import split_unit_into_subtitle_lines
    seg = {
        "text_zh": "举个例子，我一位曾供职于苹果的朋友——安迪·马图扎恰克——",
        "text_en": "EN",
        "start": 0.0, "end": 6.0,
    }
    parts = split_unit_into_subtitle_lines(seg, max_chars=14)
    for p in parts:
        chars = sum(1 for c in p["text_zh"] if "一" <= c <= "鿿")
        assert chars >= 5 or len(parts) == 1, \
            f"短行 ({chars}字) 未合并: {[x['text_zh'] for x in parts]}"


def test_tail_short_line_merged_into_prev():
    """末尾出现 ≤4 字短行时合并到前一行"""
    from pipeline import split_unit_into_subtitle_lines
    seg = {
        # 18 汉字 → max=14 切两行,但第二行可能只 3 字
        "text_zh": "现在让我们开始详细介绍这个复杂的概念。完结。",
        "text_en": "EN",
        "start": 0.0, "end": 6.0,
    }
    parts = split_unit_into_subtitle_lines(seg, max_chars=14)
    for p in parts:
        chars = sum(1 for c in p["text_zh"] if "一" <= c <= "鿿")
        assert chars >= 5 or len(parts) == 1, \
            f"短行 ({chars}字) 未合并: {[x['text_zh'] for x in parts]}"


def test_no_short_line_no_change():
    """切分均匀时,行数和顺序不变"""
    from pipeline import split_unit_into_subtitle_lines
    seg = {
        "text_zh": "第一段大约十二个字内容。第二段也是十几个字内容。",
        "text_en": "EN",
        "start": 0.0, "end": 6.0,
    }
    parts = split_unit_into_subtitle_lines(seg, max_chars=14)
    assert len(parts) == 2
    # 按句末标点切, 顺序保留
    assert parts[0]["text_zh"].endswith("。")
    assert parts[1]["text_zh"].startswith("第二段")


def test_only_one_line_when_total_short():
    """总字数 <= max → 单行不切"""
    from pipeline import split_unit_into_subtitle_lines
    seg = {
        "text_zh": "短句一行就够了。",
        "text_en": "EN",
        "start": 0.0, "end": 2.0,
    }
    parts = split_unit_into_subtitle_lines(seg, max_chars=14)
    assert len(parts) == 1


def test_merged_text_preserves_punctuation():
    """合并后标点保留,顺序正确"""
    from pipeline import split_unit_into_subtitle_lines
    seg = {
        "text_zh": "前面是较长的内容内容内容。短尾。",
        "text_en": "EN",
        "start": 0.0, "end": 5.0,
    }
    parts = split_unit_into_subtitle_lines(seg, max_chars=14)
    # 拼起来必须等于 (或近似等于,允许 strip) 原文
    joined = "".join(p["text_zh"] for p in parts)
    # 允许 strip 掉首尾空格,但内容字符一致
    expected = seg["text_zh"]
    # 比较忽略空格
    j = "".join(c for c in joined if not c.isspace())
    e = "".join(c for c in expected if not c.isspace())
    assert j == e, f"内容丢失: {joined!r} vs {expected!r}"


def test_time_continuity_after_merge():
    """合并后时间戳仍连续 (start_n+1 == end_n)"""
    from pipeline import split_unit_into_subtitle_lines
    seg = {
        "text_zh": "前面是较长的内容内容内容。短尾。",
        "text_en": "EN",
        "start": 0.0, "end": 5.0,
    }
    parts = split_unit_into_subtitle_lines(seg, max_chars=14)
    for i in range(len(parts) - 1):
        assert abs(parts[i]["end"] - parts[i + 1]["start"]) < 1e-6
    # 总区间不变
    assert parts[0]["start"] == 0.0
    assert abs(parts[-1]["end"] - 5.0) < 1e-6
