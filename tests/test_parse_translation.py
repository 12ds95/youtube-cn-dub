#!/usr/bin/env python3
"""
测试翻译解析器的 <think> 块剥离和编号前缀清理。
来源：devlog/2025-03-28-numbered-prefix-leak-in-llm-translation.md
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import _strip_think_block, _strip_numbered_prefix, _parse_numbered_translations


def test_strip_think_block():
    """<think> 推理块应被完整剥离"""
    content = "<think>\n1. reasoning\n2. more reasoning\n</think>\n\n[1] 翻译一\n[2] 翻译二"
    cleaned = _strip_think_block(content)
    assert "<think>" not in cleaned
    assert "reasoning" not in cleaned
    assert "翻译一" in cleaned
    assert "翻译二" in cleaned
    print("  ✅ test_strip_think_block")


def test_strip_numbered_prefix():
    """各种编号格式都应被正确剥离"""
    assert _strip_numbered_prefix("[1] 你好") == "你好"
    assert _strip_numbered_prefix("[14] 世界") == "世界"
    assert _strip_numbered_prefix("1. 你好") == "你好"
    assert _strip_numbered_prefix("普通文本") == "普通文本"
    # 不应错误剥离非编号开头的内容
    assert "翻译" in _strip_numbered_prefix("翻译[1]注释")
    print("  ✅ test_strip_numbered_prefix")


def test_parse_with_think_block():
    """qwen3 风格的 <think> 块 + [N] 翻译应正确解析"""
    content = (
        "<think>\nLet me translate these sentences.\n"
        "1. First about quaternion stability\n"
        "2. Second about computer graphics\n"
        "</think>\n\n"
        "[1] 它不会像其他方法那样容易出现bug\n"
        "[2] 但就计算机图形学、机器人学\n"
        "[3] 以及虚拟现实等"
    )
    result = _parse_numbered_translations(content, 3)
    assert len(result) == 3
    assert result[0] == "它不会像其他方法那样容易出现bug"
    assert result[1] == "但就计算机图形学、机器人学"
    assert result[2] == "以及虚拟现实等"
    # 确保无 [N] 前缀泄漏
    for t in result:
        assert not t.startswith("["), f"前缀泄漏: {t}"
    print("  ✅ test_parse_with_think_block")


def test_parse_clean_output():
    """标准 [N] 格式无 <think> 块应正常解析"""
    content = "[1] 你好世界\n[2] 测试翻译"
    result = _parse_numbered_translations(content, 2)
    assert result == ["你好世界", "测试翻译"]
    print("  ✅ test_parse_clean_output")


def test_parse_fallback():
    """数量不匹配时 fallback 也应去除前缀"""
    content = "[1] 翻译一\n[2] 翻译二\n[3] 翻译三"
    result = _parse_numbered_translations(content, 2)
    for t in result:
        assert not t.startswith("["), f"fallback 前缀泄漏: {t}"
    print("  ✅ test_parse_fallback")


def test_parse_single_line_concatenated():
    """回归: LLM 将多段翻译输出在同一行 [1] 文本[2] 文本[3] 文本"""
    content = "[1] 翻译一[2] 翻译二[3] 翻译三"
    result = _parse_numbered_translations(content, 3)
    assert result[0] == "翻译一", f"slot 0: {result[0]}"
    assert result[1] == "翻译二", f"slot 1: {result[1]}"
    assert result[2] == "翻译三", f"slot 2: {result[2]}"
    for i, t in enumerate(result):
        assert "[" not in t, f"slot {i} 标号泄漏: {t}"
    print("  ✅ test_parse_single_line_concatenated")


def test_parse_midline_labels():
    """回归: 文本中间嵌入的 [N] 标号应被拆分到正确槽位"""
    content = "[1] 目击者说，这片叶子原本长在树枝上，[2] 突然自己掉下来了"
    result = _parse_numbered_translations(content, 2)
    assert "目击者" in result[0], f"slot 0: {result[0]}"
    assert "突然" in result[1], f"slot 1: {result[1]}"
    assert "[2]" not in result[0], f"slot 0 标号泄漏: {result[0]}"
    print("  ✅ test_parse_midline_labels")


def test_parse_real_regression_case():
    """回归: kCc8FmEb1nY 第 17 段的实际 LLM 输出"""
    content = (
        "[1] 目击者说，这片叶子原本长在树枝上，"
        "[2] 突然自己掉下来了，场面相当震撼！"
        "[3] 这其实是个很厉害的系统"
    )
    result = _parse_numbered_translations(content, 3)
    assert "[2]" not in result[0], f"slot 0 标号泄漏: {result[0]}"
    assert "[3]" not in result[1], f"slot 1 标号泄漏: {result[1]}"
    assert "目击者" in result[0]
    assert "突然" in result[1]
    assert "系统" in result[2]
    print("  ✅ test_parse_real_regression_case")


def test_no_label_in_final_output():
    """确保最终输出不包含任何 [N] 标记（正常多行也验证）"""
    import re as _re
    content = "[1] 正常翻译一\n[2] 正常翻译二\n[3] 正常翻译三"
    result = _parse_numbered_translations(content, 3)
    for i, t in enumerate(result):
        assert not _re.search(r'\[\d+\]', t), f"slot {i} 标号泄漏: {t}"
    print("  ✅ test_no_label_in_final_output")


if __name__ == "__main__":
    print("翻译解析器测试:")
    test_strip_think_block()
    test_strip_numbered_prefix()
    test_parse_with_think_block()
    test_parse_clean_output()
    test_parse_fallback()
    test_parse_single_line_concatenated()
    test_parse_midline_labels()
    test_parse_real_regression_case()
    test_no_label_in_final_output()
    print("  全部通过")
