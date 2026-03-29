#!/usr/bin/env python3
"""
测试：TTS 重试增强逻辑（纯代码检查，不调用 edge-tts 服务）。
来源：f09d1957a98 首次生成 110 个 0 字节，重试后仍有 9 个失败。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inspect


def test_retry_has_multiple_rounds():
    """_generate_tts_segments 应有多轮重试（而非单轮）"""
    from pipeline import _generate_tts_segments
    source = inspect.getsource(_generate_tts_segments)
    assert 'max_retry_rounds' in source, \
        "_generate_tts_segments 应包含 max_retry_rounds 多轮重试"


def test_retry_has_lower_concurrency():
    """重试时应降低并发数"""
    from pipeline import _generate_tts_segments
    source = inspect.getsource(_generate_tts_segments)
    # 重试时用独立的 semaphore，并发更低
    assert 'retry_sem' in source or 'Semaphore(2)' in source, \
        "重试时应使用更低的并发限制"


def test_retry_has_backoff():
    """重试间应有递增间隔（backoff）"""
    from pipeline import _generate_tts_segments
    source = inspect.getsource(_generate_tts_segments)
    assert 'sleep' in source, "重试间应有 sleep 间隔"


def test_silence_fallback_exists():
    """最终兜底应生成静音占位文件"""
    from pipeline import _generate_tts_segments
    source = inspect.getsource(_generate_tts_segments)
    assert 'silent' in source.lower() or 'silence' in source.lower(), \
        "应有静音填充兜底"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  \u2705 {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  \u274c {t.__name__}: {e}")
            failed += 1
    icon = '\u2705' if failed == 0 else '\u274c'
    print(f"\n{icon} {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
