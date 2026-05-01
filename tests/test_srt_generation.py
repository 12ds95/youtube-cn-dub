#!/usr/bin/env python3
"""
测试 SRT 字幕生成。

来源: open-dubbing 项目 — SRT 写入→回读→逐行断言模式
       Google Ariel 项目 — 时间戳精度验证
"""
import sys, os, tempfile, re
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import format_srt_time, generate_srt_files


# ── format_srt_time 精度测试 ──

def test_srt_time_zero():
    assert format_srt_time(0.0) == "00:00:00,000"


def test_srt_time_basic():
    assert format_srt_time(1.5) == "00:00:01,500"


def test_srt_time_minutes():
    assert format_srt_time(125.123) == "00:02:05,123"


def test_srt_time_hours():
    assert format_srt_time(3661.5) == "01:01:01,500"


def test_srt_time_fractional_ms():
    """毫秒截断（非四舍五入）: 1.9999 → 999ms"""
    result = format_srt_time(1.9999)
    assert result == "00:00:01,999"


# ── generate_srt_files 端到端回读测试 ──

def _make_segments():
    return [
        {"start": 0.0, "end": 2.5, "text_en": "Hello world",
         "text_zh": "你好世界"},
        {"start": 2.5, "end": 5.0, "text_en": "How are you",
         "text_zh": "你好吗"},
        {"start": 5.0, "end": 8.123, "text_en": "Fine thanks",
         "text_zh": "很好谢谢"},
    ]


def test_srt_generates_three_files():
    """应生成 en/zh/bilingual 三个文件"""
    with tempfile.TemporaryDirectory() as tmpdir:
        en, zh, bi = generate_srt_files(_make_segments(), Path(tmpdir))
        assert en.exists()
        assert zh.exists()
        assert bi.exists()


def test_srt_en_content():
    """英文 SRT 内容回读验证 (open-dubbing roundtrip 模式)"""
    with tempfile.TemporaryDirectory() as tmpdir:
        en, _, _ = generate_srt_files(_make_segments(), Path(tmpdir))
        lines = en.read_text(encoding="utf-8").strip().split("\n")
        # 第一个字幕块
        assert lines[0] == "1"
        assert "00:00:00,000 --> 00:00:02,500" in lines[1]
        assert lines[2] == "Hello world"


def test_srt_zh_content():
    """中文 SRT 应用 strip_markdown + clean_refine_artifacts"""
    segs = [{"start": 0.0, "end": 3.0, "text_en": "test",
             "text_zh": "[轻] 这是测试"}]
    with tempfile.TemporaryDirectory() as tmpdir:
        _, zh, _ = generate_srt_files(segs, Path(tmpdir))
        content = zh.read_text(encoding="utf-8")
        assert "[轻]" not in content, "refine 标签应被清理"
        assert "这是测试" in content


def test_srt_bilingual_format():
    """双语 SRT: 中文在上、英文在下"""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, _, bi = generate_srt_files(_make_segments(), Path(tmpdir))
        content = bi.read_text(encoding="utf-8")
        blocks = content.strip().split("\n\n")
        # 第一个字幕块的第3行是中文、第4行是英文
        first_block_lines = blocks[0].split("\n")
        assert first_block_lines[2] == "你好世界"
        assert first_block_lines[3] == "Hello world"


def test_srt_index_sequential():
    """字幕索引应从 1 连续递增"""
    with tempfile.TemporaryDirectory() as tmpdir:
        en, _, _ = generate_srt_files(_make_segments(), Path(tmpdir))
        content = en.read_text(encoding="utf-8")
        blocks = content.strip().split("\n\n")
        for i, block in enumerate(blocks, 1):
            idx = int(block.split("\n")[0])
            assert idx == i, f"期望索引 {i}, 实际 {idx}"


def test_srt_timestamp_arrow_format():
    """时间戳必须使用 ' --> ' 分隔 (SRT 标准格式)"""
    with tempfile.TemporaryDirectory() as tmpdir:
        en, _, _ = generate_srt_files(_make_segments(), Path(tmpdir))
        content = en.read_text(encoding="utf-8")
        timestamps = re.findall(r"\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}", content)
        assert len(timestamps) == 3


if __name__ == "__main__":
    print("SRT 字幕生成测试:")
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✅ {name}")
    print("  全部通过")
