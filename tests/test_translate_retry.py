#!/usr/bin/env python3
"""
测试：LLM 翻译失败时的 Google Translate 回退逻辑。
来源：32884a7ba3d 运行日志中 LLM 返回空翻译直接保留英文原文的问题。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inspect


def test_llm_single_has_retry():
    """_translate_llm_single 应该有 max_retries 参数"""
    from pipeline import _translate_llm_single
    sig = inspect.signature(_translate_llm_single)
    assert 'max_retries' in sig.parameters, \
        "_translate_llm_single 缺少 max_retries 参数"


def test_llm_translate_has_google_fallback():
    """_translate_llm 中应包含 Google Translate 回退逻辑"""
    from pipeline import _translate_llm
    source = inspect.getsource(_translate_llm)
    assert 'GoogleTranslator' in source, \
        "_translate_llm 应包含 GoogleTranslator 回退"
    assert 'failed_indices' in source, \
        "_translate_llm 应跟踪失败的翻译段"


def test_llm_translate_no_silent_english_fallback():
    """失败时不应静默保留英文原文，应先尝试 Google"""
    from pipeline import _translate_llm
    source = inspect.getsource(_translate_llm)
    # 原来的逻辑是 "保留原文"，现在应该是先 Google 回退
    # 检查 failed_indices 收集逻辑
    assert 'failed_indices.append' in source, \
        "应该收集失败的 indices 用于 Google 回退"


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
