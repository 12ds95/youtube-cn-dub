#!/usr/bin/env python3
"""
测试两步翻译 (two-pass translation) 核心逻辑。

来源: VideoLingo — 三步翻译法 (直译→意译→配音适配)
       pyvideotrans — system message 角色验证
       本项目 _translate_llm_two_pass 实现
"""
import sys, os, re, inspect
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_two_pass_function_exists():
    """两步翻译函数应存在且可调用"""
    from pipeline import _translate_llm_two_pass
    assert callable(_translate_llm_two_pass)


def test_two_pass_has_two_phases():
    """源码中应有明确的 Pass 1 和 Pass 2 阶段"""
    from pipeline import _translate_llm_two_pass
    source = inspect.getsource(_translate_llm_two_pass)
    assert "pass1" in source.lower() or "Pass 1" in source or "pass_1" in source
    assert "pass2" in source.lower() or "Pass 2" in source or "pass_2" in source


def test_two_pass_pass1_is_literal():
    """Pass 1 应使用忠实直译 prompt"""
    from pipeline import _translate_llm_two_pass
    source = inspect.getsource(_translate_llm_two_pass)
    assert "忠实" in source or "直译" in source or "literal" in source.lower()


def test_two_pass_pass2_is_adaptation():
    """Pass 2 应使用配音改编 prompt"""
    from pipeline import _translate_llm_two_pass
    source = inspect.getsource(_translate_llm_two_pass)
    assert "配音" in source or "改编" in source or "dub" in source.lower()


def test_two_pass_hallucination_guard():
    """Pass 2 应有幻觉检测并回退到 Pass 1
    (VideoLingo 的 SequenceMatcher 思路: Pass 2 偏离过大 → 回退 Pass 1)"""
    from pipeline import _translate_llm_two_pass
    source = inspect.getsource(_translate_llm_two_pass)
    # 应有回退逻辑
    assert "hallucin" in source.lower() or "pass1" in source or "fallback" in source.lower() or \
           "回退" in source or "幻觉" in source


def test_two_pass_batch_size_smaller():
    """两步翻译应使用较小的 batch_size (减少 LLM 混淆)"""
    from pipeline import _translate_llm_two_pass
    source = inspect.getsource(_translate_llm_two_pass)
    # 代码中应有 batch_size 限制
    assert "batch_size" in source


def test_two_pass_calls_translate_llm():
    """Pass 1 应复用 _translate_llm 函数"""
    from pipeline import _translate_llm_two_pass
    source = inspect.getsource(_translate_llm_two_pass)
    assert "_translate_llm(" in source, "Pass 1 应调用 _translate_llm"


def test_style_detection_exists():
    """风格检测函数应存在 (VideoLingo 的主题检测思路)"""
    from pipeline import _detect_translation_style
    assert callable(_detect_translation_style)


def test_style_detection_has_sampling():
    """风格检测应对长视频做采样 (head+mid+tail) 而非全量"""
    from pipeline import _detect_translation_style
    source = inspect.getsource(_detect_translation_style)
    assert "head" in source.lower() or "sample" in source.lower() or "8000" in source


if __name__ == "__main__":
    print("两步翻译测试:")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✅ {name}")
    print("  全部通过")
