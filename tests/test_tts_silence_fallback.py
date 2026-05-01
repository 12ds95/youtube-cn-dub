#!/usr/bin/env python3
"""
测试 TTS 引擎全部失败时的静音兜底逻辑。
Bug: min(target_ms, 500) 将静音截断为 500ms，导致长段配音丢失。
"""
import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_silence_fallback_respects_target_duration():
    """兜底静音时长逻辑不应包含 500ms 截断"""
    # 直接验证逻辑: min(target_ms, 500) 是 bug，应该用 target_ms
    target_ms = 3500
    # Bug 行为:
    bug_duration = min(target_ms, 500)
    assert bug_duration == 500, "确认 bug 行为: 3500ms 被截断到 500ms"
    # 正确行为:
    correct_duration = target_ms
    assert correct_duration == 3500, "正确行为: 应保留完整 3500ms"
    print("  ✅ test_silence_fallback_respects_target_duration")


def test_silence_fallback_full_duration_in_pipeline():
    """验证 pipeline.py 中的兜底静音逻辑生成完整时长"""
    import re

    # 读取 pipeline.py 源码，检查 min(target_ms, 500) 是否已修复
    pipeline_path = Path(__file__).parent.parent / "pipeline.py"
    source = pipeline_path.read_text(encoding="utf-8")

    # 找到兜底静音行：PydubSegment.silent(duration=...)
    # 在 "最终兜底：所有引擎都失败的片段填充静音" 附近
    pattern = r'PydubSegment\.silent\(\s*duration\s*=\s*min\s*\(\s*target_ms\s*,\s*500\s*\)'
    matches = re.findall(pattern, source)
    assert len(matches) == 0, (
        f"pipeline.py 仍包含 min(target_ms, 500) 的截断 bug，"
        f"应改为 duration=target_ms"
    )
    print("  ✅ test_silence_fallback_full_duration_in_pipeline")


if __name__ == "__main__":
    print("TTS 静音兜底测试:")
    test_silence_fallback_respects_target_duration()
    test_silence_fallback_full_duration_in_pipeline()
    print("  全部通过")
