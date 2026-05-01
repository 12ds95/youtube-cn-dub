#!/usr/bin/env python3
"""
测试 LLM 精简候选解析 + 最优候选选择。

来源: pyvideotrans — <TRANSLATE_TEXT> 标签提取测试模式
       Google Ariel — 空响应/异常响应处理测试
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import (
    _parse_multi_candidates, _clean_refine_artifacts, _select_best_candidate
)


# ── _clean_refine_artifacts 测试 ──

def test_clean_light_tag():
    assert _clean_refine_artifacts("[轻] 简化后的文本") == "简化后的文本"


def test_clean_medium_tag_markdown():
    assert _clean_refine_artifacts("**[中]** 中度精简") == "中度精简"


def test_clean_short_tag_bullet():
    assert _clean_refine_artifacts("- [短] 极简版本") == "极简版本"


def test_clean_system_echo():
    """LLM 回显系统指令 → 返回空"""
    result = _clean_refine_artifacts("以下为[轻]/[中]/[短]三个版本")
    assert result == ""


def test_clean_no_artifact():
    assert _clean_refine_artifacts("正常文本") == "正常文本"


def test_clean_empty():
    assert _clean_refine_artifacts("") == ""


# ── _parse_multi_candidates 测试 ──

def test_parse_standard_format():
    """标准 [N] + [轻][中][短] 格式"""
    content = """[1]
[轻] 轻度精简版本一
[中] 中度精简版本一
[短] 短版本一

[2]
[轻] 轻度精简版本二
[中] 中度精简版本二
[短] 短版本二"""
    result = _parse_multi_candidates(content, 2)
    assert len(result) == 2
    assert len(result[0]) == 3
    assert result[0][0] == "轻度精简版本一"
    assert result[1][2] == "短版本二"


def test_parse_markdown_format():
    """markdown 加粗变体 (pyvideotrans 标签提取思路)"""
    content = """**[1]**
**[轻]** 版本一A
**[中]** 版本一B
**[短]** 版本一C"""
    result = _parse_multi_candidates(content, 1)
    assert len(result) == 1
    assert len(result[0]) == 3
    assert result[0][0] == "版本一A"


def test_parse_bullet_format():
    """列表符号变体"""
    content = """[1]
- [轻] 列表版本A
- [中] 列表版本B
- [短] 列表版本C"""
    result = _parse_multi_candidates(content, 1)
    assert len(result[0]) == 3
    assert result[0][0] == "列表版本A"


def test_parse_fewer_than_expected():
    """LLM 返回比预期少 → 用空列表补齐
    (pyvideotrans: line count mismatch → padding with empty)"""
    content = """[1]
[轻] 只有一段"""
    result = _parse_multi_candidates(content, 3)
    assert len(result) == 3
    assert len(result[0]) >= 1
    assert result[1] == []
    assert result[2] == []


def test_parse_system_echo_skipped():
    """LLM 回显系统指令行应被跳过"""
    content = """以下为每段翻译的[轻]/[中]/[短]三个版本：

[1]
[轻] 实际精简
[中] 中度
[短] 极简"""
    result = _parse_multi_candidates(content, 1)
    assert len(result[0]) == 3
    assert "实际精简" in result[0][0]


def test_parse_empty_content():
    """空内容 (Google Ariel: 空响应 → sentinel 处理)"""
    result = _parse_multi_candidates("", 2)
    assert len(result) == 2
    assert result[0] == []
    assert result[1] == []


# ── _select_best_candidate 测试 ──

def test_select_empty_candidates():
    """无候选返回空"""
    result = _select_best_candidate([], 2000, "原文", 0, [])
    assert result == ""


def test_select_rejects_longer_than_original():
    """候选比原文长 → 排除"""
    segments = [{"text_zh": "原文短", "start": 0, "end": 3}]
    result = _select_best_candidate(
        ["这是一个比原文长很多的候选文本用来测试排除逻辑"],
        2000, "原文短", 0, segments)
    assert result == ""


def test_select_prefers_within_target():
    """优先选不超出目标时长的候选"""
    segments = [
        {"text_zh": "这是一个需要被精简的比较长的中文翻译句子", "start": 0, "end": 3},
    ]
    # 候选都比原文短
    candidates = ["精简版", "中等长度的精简"]
    result = _select_best_candidate(
        candidates, 3000, "这是一个需要被精简的比较长的中文翻译句子",
        0, segments)
    # 应该返回一个候选（具体哪个取决于 jieba 估算）
    assert result in candidates


if __name__ == "__main__":
    print("精简候选解析测试:")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✅ {name}")
    print("  全部通过")
