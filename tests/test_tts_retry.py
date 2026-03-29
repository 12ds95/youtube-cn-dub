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
    """智能重试应支持多轮重试（远程引擎连续无改善才放弃）"""
    from pipeline import _smart_retry_engine
    source = inspect.getsource(_smart_retry_engine)
    assert 'no_improve_count' in source or 'retry_round' in source, \
        "_smart_retry_engine 应包含多轮重试逻辑"


def test_retry_has_lower_concurrency():
    """远程引擎重试时应降低并发数"""
    from pipeline import _smart_retry_engine
    source = inspect.getsource(_smart_retry_engine)
    # 远程引擎: 阶梯降并发 → concurrency // 2 → 1
    assert 'concurrency // 2' in source or 'retry_c = 1' in source, \
        "重试时应使用更低的并发限制"


def test_retry_has_backoff():
    """重试间应有递增间隔（backoff）"""
    from pipeline import _smart_retry_engine
    source = inspect.getsource(_smart_retry_engine)
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
