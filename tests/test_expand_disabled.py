#!/usr/bin/env python3
"""
测试：_expand_with_llm 已被禁用，underslow 片段不应被 LLM 修改翻译文本。
来源：devlog/2026-03-29-expand-llm-garbage.md
复现场景：32884a7ba3d #44/#52/#71 被 expand 生成与英文原文无关的垃圾翻译
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ast
import inspect


def test_expand_not_called_in_refinement_loop():
    """确认 run_refinement_loop 中 _expand_with_llm 调用已被注释/移除"""
    import pipeline
    source = inspect.getsource(pipeline.run_refinement_loop)
    # _expand_with_llm 不应在非注释行中被调用
    for line in source.split('\n'):
        stripped = line.strip()
        if '_expand_with_llm' in stripped and not stripped.startswith('#'):
            assert False, (
                f"_expand_with_llm 仍在 run_refinement_loop 中被调用: {stripped}"
            )


def test_underslow_segments_unchanged_after_refine_loop():
    """确认 refine loop 使用 _isometric_expand_batch 而非 _expand_with_llm"""
    import pipeline
    source = inspect.getsource(pipeline.run_refinement_loop)
    # 应使用 _isometric_expand_batch 替代 _expand_with_llm
    found_isometric = False
    for line in source.split('\n'):
        stripped = line.strip()
        if '_isometric_expand_batch' in stripped and not stripped.startswith('#'):
            found_isometric = True
    assert found_isometric, \
        "run_refinement_loop 应使用 _isometric_expand_batch 进行过短片段扩展"


def test_expand_with_llm_function_still_exists():
    """_expand_with_llm 函数本身应保留（未来可能加入忠实度校验后重新启用）"""
    import pipeline
    assert hasattr(pipeline, '_expand_with_llm'), \
        "_expand_with_llm 函数被删除了，应保留以备将来改进"


def test_real_case_44_garbage_detection():
    """
    复现 32884a7ba3d #44 的真实案例：
    英文: "The only rule you need to remember is..."
    正确翻译: "唯一需要记住的规则是……"
    LLM 扩展的垃圾: "四元数非交换、天然适配三维旋转，数值稳定。"
    验证字符重叠率极低（说明扩展结果与原文无关）
    """
    from pipeline import _char_overlap_ratio

    original_zh = "唯一需要记住的规则是……"
    garbage_zh = "四元数非交换、天然适配三维旋转，数值稳定。"

    overlap = _char_overlap_ratio(original_zh, garbage_zh)
    # 两个完全不同含义的句子，重叠率应该很低
    assert overlap < 0.3, (
        f"原文与垃圾扩展的重叠率 {overlap:.2f} 过高，"
        f"说明检测逻辑可能无法识别偏离原文的扩展"
    )


def test_real_case_52_garbage_detection():
    """
    复现 32884a7ba3d #52：
    英文: "Similar to complex numbers, you can construct a quaternion based on this angle:"
    正确翻译: "类似复数情形，你可根据该角度构造一个四元数："
    LLM 扩展的垃圾: "就其本身而言，四元数在数学上具有诸多引人入胜的特性——"
    """
    from pipeline import _char_overlap_ratio

    original_zh = "类似复数情形，你可根据该角度构造一个四元数："
    garbage_zh = "就其本身而言，四元数在数学上具有诸多引人入胜的特性——例如其代数结构的完备性"

    overlap = _char_overlap_ratio(original_zh, garbage_zh)
    assert overlap < 0.3, (
        f"原文与垃圾扩展的重叠率 {overlap:.2f} 过高"
    )


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1
    print(f"\n{'✅' if failed == 0 else '❌'} {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
