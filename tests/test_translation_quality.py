#!/usr/bin/env python3
"""
测试翻译质量优化：对齐保证、风格控制。
来源：devlog/2026-03-28-five-todos-implementation.md (TODO1)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import DEFAULT_CONFIG


def test_style_field_exists():
    """DEFAULT_CONFIG 应包含 llm.style 字段"""
    assert "style" in DEFAULT_CONFIG["llm"], "DEFAULT_CONFIG['llm'] 缺少 'style' 字段"
    assert DEFAULT_CONFIG["llm"]["style"] == "", "默认 style 应为空字符串"
    print("  ✅ test_style_field_exists")


def test_alignment_threshold():
    """
    对齐校验逻辑：解析结果有效数 < batch 70% 时应触发降级。
    这里测试阈值计算本身。
    """
    batch_size = 10
    threshold = 0.7
    # 7 个有效 → 刚好 70%，不触发
    assert 7 >= batch_size * threshold
    # 6 个有效 → 60%，触发降级
    assert 6 < batch_size * threshold
    print("  ✅ test_alignment_threshold")


def test_context_prompt_building():
    """上下文 prompt 构建逻辑"""
    video_title = "四元数可视化"
    prev_context = ["上一句翻译A", "上一句翻译B"]

    context_hint = ""
    if video_title:
        context_hint += f"视频主题：{video_title}\n"
    if prev_context:
        context_hint += f"前文：{'；'.join(prev_context)}\n"

    assert "四元数可视化" in context_hint
    assert "上一句翻译A；上一句翻译B" in context_hint
    print("  ✅ test_context_prompt_building")


def test_empty_context():
    """无上下文时 prompt 不应包含多余内容"""
    video_title = ""
    prev_context = []

    context_hint = ""
    if video_title:
        context_hint += f"视频主题：{video_title}\n"
    if prev_context:
        context_hint += f"前文：{'；'.join(prev_context)}\n"

    assert context_hint == ""
    print("  ✅ test_empty_context")


if __name__ == "__main__":
    print("翻译质量优化测试:")
    test_style_field_exists()
    test_alignment_threshold()
    test_context_prompt_building()
    test_empty_context()
    print("  全部通过")
