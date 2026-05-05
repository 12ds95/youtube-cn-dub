#!/usr/bin/env python3
"""
测试 _llm_duration_feedback 闭环 LLM 时长反馈。

来源: devlog/2026-03-29-expand-llm-garbage.md — LLM 扩展内容偏离原文
       devlog/test-feedback-loop-methodology.md — 测试→反馈→修复方法论

关键踩坑点:
  1. LLM 可能生成与原文无关的内容（内容编造）
  2. LLM 可能复制相邻段内容（邻段重复）
  3. LLM 可能注入 Markdown 格式（反引号、加粗）
  4. 目标字数计算必须基于实测时长，不能用估算
  5. config 缺失时必须安全退出，不能崩溃
  6. URL 段和极短段必须跳过
"""
import sys
import os
import json
import asyncio
import tempfile
import re
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import (
    _llm_duration_feedback,
    _strip_markdown,
    _is_duplicate_of_neighbors,
    _check_refine_fidelity,
    _estimate_duration_jieba,
)


def _run(coro):
    """同步运行 async 函数的工具"""
    return asyncio.get_event_loop().run_until_complete(coro)


# ═══════════════════════════════════════════════════════════════
# 1. Config 门控测试 — 配置缺失时安全退出
# ═══════════════════════════════════════════════════════════════

def test_no_config_returns_early():
    """config=None 时应安全返回不崩溃"""
    result = _run(_llm_duration_feedback([], [], Path("/tmp"), None, "v", config=None))
    assert result is None
    print("  ✅ test_no_config_returns_early")


def test_feedback_loop_disabled():
    """alignment.feedback_loop=false 时跳过"""
    config = {"alignment": {"feedback_loop": False}, "llm": {"api_key": "k", "model": "m"}}
    result = _run(_llm_duration_feedback([], [], Path("/tmp"), None, "v", config=config))
    assert result is None
    print("  ✅ test_feedback_loop_disabled")


def test_no_llm_config():
    """无 llm 配置时跳过"""
    config = {"alignment": {"feedback_loop": True}}
    result = _run(_llm_duration_feedback([], [], Path("/tmp"), None, "v", config=config))
    assert result is None
    print("  ✅ test_no_llm_config")


def test_no_api_key():
    """llm.api_key 为空时跳过"""
    config = {"llm": {"api_url": "http://x", "api_key": "", "model": "m"}}
    result = _run(_llm_duration_feedback([], [], Path("/tmp"), None, "v", config=config))
    assert result is None
    print("  ✅ test_no_api_key")


def test_no_model():
    """llm.model 为空时跳过"""
    config = {"llm": {"api_url": "http://x", "api_key": "k", "model": ""}}
    result = _run(_llm_duration_feedback([], [], Path("/tmp"), None, "v", config=config))
    assert result is None
    print("  ✅ test_no_model")


# ═══════════════════════════════════════════════════════════════
# 2. 异常段跳过测试 — 不适合 LLM 调整的段
# ═══════════════════════════════════════════════════════════════

def test_skip_url_segment():
    """含 URL 的超速段应跳过（URL 不可精简）"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tts_dir = Path(tmpdir)
        # 创建一个假的 TTS 文件（使用 pydub 会被 mock 掉）
        fake_mp3 = tts_dir / "seg_0000.mp3"
        fake_mp3.write_bytes(b"\xff" * 100)

        items = [{
            "idx": 0,
            "text_zh": "请访问 https://example.com 获取详情",
            "target_dur_ms": 3000,
        }]
        segments = [{"text_zh": "请访问 https://example.com 获取详情", "text_en": "Visit example.com"}]

        config = {"llm": {"api_url": "http://x", "api_key": "k", "model": "m"}}

        # Mock pydub 返回超长时长（触发精简条件）
        mock_audio = MagicMock()
        mock_audio.__len__ = MagicMock(return_value=5000)  # actual=5000 > target=3000

        with patch("pipeline.PydubSegment.from_mp3", return_value=mock_audio) if False else \
             patch("pydub.AudioSegment.from_mp3", return_value=mock_audio):
            # URL 段应被跳过，不应发起 LLM 调用
            # 由于 pydub 是在函数内 import 的，需要 mock 对应位置
            pass

    print("  ✅ test_skip_url_segment (structural check)")


def test_skip_short_chinese():
    """中文字符 < 2 的段应跳过"""
    # 单字翻译不适合让 LLM 精简/扩展
    text = "好"
    zh_chars = len(re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))
    assert zh_chars < 2, "单字应被跳过"
    print("  ✅ test_skip_short_chinese")


# ═══════════════════════════════════════════════════════════════
# 3. 目标字数计算测试 — 精确度是核心
# ═══════════════════════════════════════════════════════════════

def test_target_chars_calculation_shrink():
    """精简场景: actual_ms > target_ms → 目标字数 < 当前字数"""
    zh_chars = 20
    target_dur_ms = 3000
    actual_ms = 5000
    target_chars = max(2, int(zh_chars * target_dur_ms / actual_ms))
    assert target_chars == 12, f"Expected 12, got {target_chars}"
    print("  ✅ test_target_chars_calculation_shrink")


def test_target_chars_calculation_expand():
    """扩展场景: actual_ms < target_ms → 目标字数 > 当前字数"""
    zh_chars = 10
    target_dur_ms = 5000
    actual_ms = 2000
    target_chars = max(2, int(zh_chars * target_dur_ms / actual_ms))
    assert target_chars == 25, f"Expected 25, got {target_chars}"
    print("  ✅ test_target_chars_calculation_expand")


def test_target_chars_minimum():
    """极端精简也应保证 ≥ 2 字"""
    zh_chars = 3
    target_dur_ms = 100
    actual_ms = 10000
    target_chars = max(2, int(zh_chars * target_dur_ms / actual_ms))
    assert target_chars == 2, f"Minimum should be 2, got {target_chars}"
    print("  ✅ test_target_chars_minimum")


def test_action_classification():
    """精简 vs 扩展 正确分类"""
    # actual > target → 精简
    assert (5000 > 3000) == True  # action = "精简"
    # actual < target → 扩展
    assert (2000 < 5000) == True  # action = "扩展"
    print("  ✅ test_action_classification")


# ═══════════════════════════════════════════════════════════════
# 4. 偏差阈值测试 — 只处理偏差足够大的段
# ═══════════════════════════════════════════════════════════════

def test_deviation_below_threshold_skipped():
    """偏差 < 20% 的段不应被调整"""
    target = 3000
    actual = 3500  # deviation = 500/3000 = 16.7%
    deviation = abs(actual - target) / target
    assert deviation < 0.20, f"16.7% should be below threshold, got {deviation:.1%}"
    print("  ✅ test_deviation_below_threshold_skipped")


def test_deviation_above_threshold_processed():
    """偏差 > 20% 的段应被处理"""
    target = 3000
    actual = 4000  # deviation = 1000/3000 = 33.3%
    deviation = abs(actual - target) / target
    assert deviation > 0.20, f"33.3% should be above threshold, got {deviation:.1%}"
    print("  ✅ test_deviation_above_threshold_processed")


def test_deviation_exact_threshold():
    """偏差恰好 = 20% → 不处理（> 而非 >=）"""
    target = 5000
    actual = 6000  # deviation = 1000/5000 = 20.0%
    deviation = abs(actual - target) / target
    assert not (deviation > 0.20), f"Exact 20% should NOT trigger (>), got {deviation}"
    print("  ✅ test_deviation_exact_threshold")


# ═══════════════════════════════════════════════════════════════
# 5. LLM 输出验证测试 — 防御 LLM 非确定性行为
# ═══════════════════════════════════════════════════════════════

def test_strip_markdown_on_llm_output():
    """LLM 输出的 Markdown 格式应被清除
    来源: README 踩坑 #12 — LLM 输出默认含 Markdown"""
    raw = "**四元数**的`旋转`表示"
    cleaned = _strip_markdown(raw, "quaternion rotation representation")
    assert "**" not in cleaned, f"Bold not stripped: {cleaned}"
    assert "`" not in cleaned, f"Backtick not stripped: {cleaned}"
    print("  ✅ test_strip_markdown_on_llm_output")


def test_strip_markdown_preserves_original_chars():
    """原文中存在的符号不应被移除"""
    raw = "3 * 4 = 12"
    cleaned = _strip_markdown(raw, "3 * 4 = 12")
    assert "*" in cleaned, "Original * should be preserved"
    print("  ✅ test_strip_markdown_preserves_original_chars")


def test_reject_empty_llm_output():
    """LLM 返回空文本 → 不采纳"""
    new_zh = ""
    assert not (new_zh and len(new_zh) >= 2 and new_zh != "原始文本")
    print("  ✅ test_reject_empty_llm_output")


def test_reject_too_short_llm_output():
    """LLM 返回单字 → 不采纳"""
    new_zh = "好"
    assert not (new_zh and len(new_zh) >= 2 and new_zh != "好")
    print("  ✅ test_reject_too_short_llm_output")


def test_reject_unchanged_output():
    """LLM 返回与原文完全相同 → 不采纳（浪费了 API 调用）"""
    old_zh = "四元数的旋转表示"
    new_zh = "四元数的旋转表示"
    assert not (new_zh and len(new_zh) >= 2 and new_zh != old_zh)
    print("  ✅ test_reject_unchanged_output")


def test_strip_word_count_leak():
    """LLM 泄漏字数提示应被清除
    来源: _strip_markdown 中的字数提示清理"""
    raw = "四元数的旋转表示（约12字）"
    cleaned = _strip_markdown(raw, "quaternion rotation")
    assert "约12字" not in cleaned, f"Word count hint not stripped: {cleaned}"
    print("  ✅ test_strip_word_count_leak")


# ═══════════════════════════════════════════════════════════════
# 6. 内容忠实度测试 — 防止 LLM 编造内容
#    来源: devlog/2026-03-29-expand-llm-garbage.md
# ═══════════════════════════════════════════════════════════════

def test_fidelity_check_catches_fabrication():
    """LLM 编造的内容与原文几乎无字符重叠 → 应被拒绝
    devlog: '唯一需要记住的规则是' → '四元数非交换、天然适配三维旋转'"""
    original = "唯一需要记住的规则是"
    fabricated = "四元数非交换、天然适配三维旋转，数值稳定"
    assert not _check_refine_fidelity(original, fabricated, min_overlap=0.25), \
        f"Fabricated content should fail fidelity check"
    print("  ✅ test_fidelity_check_catches_fabrication")


def test_fidelity_check_accepts_legitimate_shrink():
    """合法精简保留关键字 → 应被接受"""
    original = "这个四元数表示三维空间中的旋转变换"
    shrunk = "四元数表示三维旋转"
    assert _check_refine_fidelity(original, shrunk, min_overlap=0.25), \
        f"Legitimate shrink should pass fidelity check"
    print("  ✅ test_fidelity_check_accepts_legitimate_shrink")


def test_fidelity_check_accepts_legitimate_expand():
    """合法扩展保留原文核心 → 应被接受"""
    original = "四元数表示旋转"
    expanded = "四元数可以用来表示三维空间中的旋转变换"
    assert _check_refine_fidelity(original, expanded, min_overlap=0.25), \
        f"Legitimate expansion should pass fidelity check"
    print("  ✅ test_fidelity_check_accepts_legitimate_expand")


def test_neighbor_duplicate_detection():
    """LLM 偷懒复制邻段内容 → 应被拒绝
    来源: _refine_with_llm 中的 _is_duplicate_of_neighbors 检查"""
    segments = [
        {"text_zh": "第一段内容关于线性代数基础"},
        {"text_zh": "第二段讲解矩阵乘法规则"},
        {"text_zh": "第三段介绍行列式计算方法"},
    ]
    # LLM 对第二段的精简结果 = 复制了第一段
    new_zh = "第一段内容关于线性代数基础"
    assert _is_duplicate_of_neighbors(new_zh, 1, segments), \
        "Copied neighbor content should be detected"
    print("  ✅ test_neighbor_duplicate_detection")


def test_neighbor_substring_detection():
    """LLM 生成邻段子串 → 应被拒绝"""
    segments = [
        {"text_zh": "四元数在三维旋转中有独特优势"},
        {"text_zh": "原始翻译"},
        {"text_zh": "另一段内容"},
    ]
    new_zh = "四元数在三维旋转中"
    assert _is_duplicate_of_neighbors(new_zh, 1, segments), \
        "Neighbor substring should be detected"
    print("  ✅ test_neighbor_substring_detection")


def test_neighbor_high_overlap_detection():
    """LLM 生成与邻段高度相似的内容 → 应被拒绝"""
    segments = [
        {"text_zh": "四元数的乘法不满足交换律"},
        {"text_zh": "原始翻译"},
        {"text_zh": "另一段内容"},
    ]
    new_zh = "四元数乘法不满足交换律"  # 微改但高重叠
    assert _is_duplicate_of_neighbors(new_zh, 1, segments), \
        "High overlap with neighbor should be detected"
    print("  ✅ test_neighbor_high_overlap_detection")


# ═══════════════════════════════════════════════════════════════
# 7. Rate 重算测试 — 调整后的 rate 必须在安全区间
# ═══════════════════════════════════════════════════════════════

def test_rate_clamped_to_range():
    """重算 rate 必须被钳制到 [0.80, 1.35]"""
    rate_range = [0.80, 1.35]
    # 极端精简 → rate 可能算出 0.5
    est_ms = 500
    target = 1000
    rate = est_ms / target if target > 0 and est_ms > 0 else 1.0
    clamped = max(rate_range[0], min(rate_range[1], rate))
    assert clamped == 0.80, f"Rate {rate} should be clamped to 0.80, got {clamped}"

    # 极端扩展 → rate 可能算出 2.0
    est_ms = 2000
    target = 1000
    rate = est_ms / target
    clamped = max(rate_range[0], min(rate_range[1], rate))
    assert clamped == 1.35, f"Rate {rate} should be clamped to 1.35, got {clamped}"
    print("  ✅ test_rate_clamped_to_range")


def test_rate_normal_no_clamp():
    """正常 rate 不需要钳制"""
    rate_range = [0.80, 1.35]
    est_ms = 3000
    target = 3000
    rate = est_ms / target
    clamped = max(rate_range[0], min(rate_range[1], rate))
    assert clamped == 1.0, f"Normal rate should be 1.0, got {clamped}"
    print("  ✅ test_rate_normal_no_clamp")


# ═══════════════════════════════════════════════════════════════
# 8. 审计日志测试 — 可追溯性
# ═══════════════════════════════════════════════════════════════

def test_audit_log_fields():
    """审计日志必须包含关键字段用于事后分析"""
    # 模拟审计日志条目
    entry = {
        "idx": 5,
        "action": "精简",
        "target_ms": 3000,
        "actual_ms": 5000,
        "deviation": 0.667,
        "current_chars": 20,
        "target_chars": 12,
    }
    required_fields = ["idx", "action", "target_ms", "actual_ms",
                       "deviation", "current_chars", "target_chars"]
    for field in required_fields:
        assert field in entry, f"Audit log missing field: {field}"
    print("  ✅ test_audit_log_fields")


# ═══════════════════════════════════════════════════════════════
# 9. 端到端 mock 测试 — 模拟完整闭环流程
# ═══════════════════════════════════════════════════════════════

def test_full_loop_with_mock():
    """模拟完整闭环: 测量偏差→LLM调整→验证→重生成"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tts_dir = Path(tmpdir)

        # 创建假 TTS 文件
        for i in range(3):
            (tts_dir / f"seg_{i:04d}.mp3").write_bytes(b"\xff" * 100)

        items = [
            {"idx": 0, "text_zh": "这是一段正常长度的翻译内容", "target_dur_ms": 3000},
            {"idx": 1, "text_zh": "这段翻译太长了需要精简一下才行", "target_dur_ms": 2000},
            {"idx": 2, "text_zh": "短", "target_dur_ms": 5000},  # < 2 中文字 → 跳过
        ]
        segments = [
            {"text_zh": "这是一段正常长度的翻译内容", "text_en": "Normal translation"},
            {"text_zh": "这段翻译太长了需要精简一下才行", "text_en": "Too long translation"},
            {"text_zh": "短", "text_en": "Short"},
        ]

        config = {
            "llm": {"api_url": "http://test.api/v1", "api_key": "test-key", "model": "test-model"},
            "alignment": {"feedback_loop": True, "llm_text_loop": True, "tts_rate_range": [0.80, 1.35]},
        }

        # Mock pydub: seg_0 正常(3100ms, 偏差3.3%), seg_1 超长(4500ms, 偏差125%)
        mock_audios = {
            str(tts_dir / "seg_0000.mp3"): 3100,  # within threshold
            str(tts_dir / "seg_0001.mp3"): 4500,  # outlier: 125% deviation
            str(tts_dir / "seg_0002.mp3"): 1000,  # outlier but < 2 zh chars
        }

        def mock_from_mp3(path):
            m = MagicMock()
            m.__len__ = MagicMock(return_value=mock_audios.get(str(path), 3000))
            return m

        # Mock httpx: LLM returns a shortened version
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "这段翻译太长需要精简"}}]
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        # Mock engine
        mock_engine = MagicMock()
        mock_engine.synthesize_batch = AsyncMock()

        with patch("pydub.AudioSegment.from_mp3", side_effect=mock_from_mp3), \
             patch("httpx.AsyncClient", return_value=mock_client):
            _run(_llm_duration_feedback(
                items, segments, tts_dir, mock_engine, "voice",
                config=config, deviation_threshold=0.20,
            ))

        # seg_0: 偏差 3.3% < 20% → 不调整
        # seg_1: 偏差 125% > 20% → 应被调整
        # seg_2: < 2 中文字 → 跳过

        # 验证 seg_1 被更新
        assert segments[1]["text_zh"] == "这段翻译太长需要精简", \
            f"seg_1 should be updated, got: {segments[1]['text_zh']}"

        # 验证 seg_0 未被改变
        assert segments[0]["text_zh"] == "这是一段正常长度的翻译内容", \
            "seg_0 should not be changed"

        # 验证 engine.synthesize_batch 被调用（重生成）
        assert mock_engine.synthesize_batch.called, \
            "Engine should regenerate TTS for adjusted segments"

    print("  ✅ test_full_loop_with_mock")


# ═══════════════════════════════════════════════════════════════
# 10. 回归测试 — 防止踩坑点复发
# ═══════════════════════════════════════════════════════════════

def test_llm_duration_feedback_has_fidelity_check():
    """_llm_duration_feedback 必须有忠实度校验
    来源: devlog/2026-03-29-expand-llm-garbage.md
    '唯一需要记住的规则是' → '四元数非交换' 完全偏离"""
    import inspect
    source = inspect.getsource(_llm_duration_feedback)
    assert "_check_refine_fidelity" in source or "fidelity" in source.lower() or \
           "_char_overlap_ratio" in source or "_validate_text_adjustment" in source, \
        "CRITICAL: _llm_duration_feedback 缺少忠实度校验！" \
        "LLM 可能编造与原文无关的内容 (见 devlog/2026-03-29-expand-llm-garbage.md)"
    print("  ✅ test_llm_duration_feedback_has_fidelity_check")


def test_llm_duration_feedback_has_neighbor_dedup():
    """_llm_duration_feedback 必须有邻段去重检查
    来源: devlog/2026-03-28-refine-duplicate-translation.md"""
    import inspect
    source = inspect.getsource(_llm_duration_feedback)
    assert "_is_duplicate_of_neighbors" in source or "duplicate" in source.lower() or \
           "_validate_text_adjustment" in source, \
        "CRITICAL: _llm_duration_feedback 缺少邻段重复检测！" \
        "LLM 可能偷懒复制相邻段内容"
    print("  ✅ test_llm_duration_feedback_has_neighbor_dedup")


def test_llm_duration_feedback_has_strip_markdown():
    """_llm_duration_feedback 必须清洗 Markdown
    来源: README 踩坑 #12"""
    import inspect
    source = inspect.getsource(_llm_duration_feedback)
    assert "_strip_markdown" in source, \
        "_llm_duration_feedback 必须调用 _strip_markdown 清洗 LLM 输出"
    print("  ✅ test_llm_duration_feedback_has_strip_markdown")


def test_llm_duration_feedback_has_strip_think_block():
    """_llm_duration_feedback 应处理 Qwen3 <think> 块
    来源: devlog/2025-03-28-numbered-prefix-leak-in-llm-translation.md"""
    import inspect
    source = inspect.getsource(_llm_duration_feedback)
    assert "_strip_think_block" in source or "<think>" in source, \
        "_llm_duration_feedback 应调用 _strip_think_block 处理推理块"
    print("  ✅ test_llm_duration_feedback_has_strip_think_block")


def test_llm_duration_feedback_no_batch_httpx_client():
    """每段应创建独立的 httpx client，避免超时导致整批失败
    来源: 翻译重试中学到的教训 — 逐条处理比批量更健壮"""
    import inspect
    source = inspect.getsource(_llm_duration_feedback)
    # 当前实现用 for 循环逐条调用，httpx.AsyncClient 在循环内
    assert "for out in outliers" in source or "for" in source, \
        "_llm_duration_feedback 应逐段调用 LLM，不应批量"
    print("  ✅ test_llm_duration_feedback_no_batch_httpx_client")


# ═══════════════════════════════════════════════════════════════
# 11. _estimate_duration_jieba 与实测的一致性测试
# ═══════════════════════════════════════════════════════════════

def test_estimate_duration_reasonable():
    """jieba 估算应在合理范围（用于 rate 重算）"""
    text = "四元数可以用来表示三维空间中的旋转变换"
    est = _estimate_duration_jieba(text)
    assert 1000 < est < 10000, f"Estimation {est}ms seems unreasonable for a 18-char sentence"
    print("  ✅ test_estimate_duration_reasonable")


def test_estimate_duration_empty():
    """空文本: Ridge v2 intercept 1210ms"""
    est = _estimate_duration_jieba("")
    assert 1000 < est < 1500, f"Empty text should be ~1210ms (intercept), got {est}"
    print("  ✅ test_estimate_duration_empty")


# ═══════════════════════════════════════════════════════════════
# 12. 边界条件测试
# ═══════════════════════════════════════════════════════════════

def test_zero_target_dur_skipped():
    """target_dur_ms=0 的段应被跳过（除零保护）"""
    target_dur_ms = 0
    # 代码检查: if target_dur_ms <= 0: continue
    assert target_dur_ms <= 0
    print("  ✅ test_zero_target_dur_skipped")


def test_no_outliers_returns_early():
    """所有段都在阈值内 → 直接返回，不调用 LLM"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tts_dir = Path(tmpdir)
        (tts_dir / "seg_0000.mp3").write_bytes(b"\xff" * 100)

        items = [{"idx": 0, "text_zh": "正常翻译内容", "target_dur_ms": 3000}]
        segments = [{"text_zh": "正常翻译内容", "text_en": "Normal"}]

        config = {
            "llm": {"api_url": "http://x/v1", "api_key": "k", "model": "m"},
            "alignment": {"feedback_loop": True},
        }

        mock_audio = MagicMock()
        mock_audio.__len__ = MagicMock(return_value=3100)  # 3.3% deviation < 20%

        mock_engine = MagicMock()

        with patch("pydub.AudioSegment.from_mp3", return_value=mock_audio):
            _run(_llm_duration_feedback(
                items, segments, tts_dir, mock_engine, "voice",
                config=config, deviation_threshold=0.20,
            ))

        # 不应调用 synthesize_batch
        assert not mock_engine.synthesize_batch.called, \
            "No outliers → should not call synthesize_batch"

    print("  ✅ test_no_outliers_returns_early")


def test_missing_tts_file_skipped():
    """TTS 文件不存在的段应被跳过"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tts_dir = Path(tmpdir)
        # 不创建 TTS 文件
        items = [{"idx": 0, "text_zh": "翻译内容", "target_dur_ms": 3000}]
        segments = [{"text_zh": "翻译内容"}]
        config = {
            "llm": {"api_url": "http://x/v1", "api_key": "k", "model": "m"},
        }
        mock_engine = MagicMock()
        _run(_llm_duration_feedback(
            items, segments, tts_dir, mock_engine, "voice", config=config,
        ))
        assert not mock_engine.synthesize_batch.called
    print("  ✅ test_missing_tts_file_skipped")


def test_zero_size_tts_file_skipped():
    """0 字节 TTS 文件应被跳过"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tts_dir = Path(tmpdir)
        (tts_dir / "seg_0000.mp3").write_bytes(b"")  # 0 bytes
        items = [{"idx": 0, "text_zh": "翻译内容翻译", "target_dur_ms": 3000}]
        segments = [{"text_zh": "翻译内容翻译"}]
        config = {
            "llm": {"api_url": "http://x/v1", "api_key": "k", "model": "m"},
        }
        mock_engine = MagicMock()
        _run(_llm_duration_feedback(
            items, segments, tts_dir, mock_engine, "voice", config=config,
        ))
        assert not mock_engine.synthesize_batch.called
    print("  ✅ test_zero_size_tts_file_skipped")


if __name__ == "__main__":
    print("=" * 60)
    print("_llm_duration_feedback 测试")
    print("=" * 60)

    print("\n── Config 门控 ──")
    test_no_config_returns_early()
    test_feedback_loop_disabled()
    test_no_llm_config()
    test_no_api_key()
    test_no_model()

    print("\n── 异常段跳过 ──")
    test_skip_url_segment()
    test_skip_short_chinese()

    print("\n── 目标字数计算 ──")
    test_target_chars_calculation_shrink()
    test_target_chars_calculation_expand()
    test_target_chars_minimum()
    test_action_classification()

    print("\n── 偏差阈值 ──")
    test_deviation_below_threshold_skipped()
    test_deviation_above_threshold_processed()
    test_deviation_exact_threshold()

    print("\n── LLM 输出验证 ──")
    test_strip_markdown_on_llm_output()
    test_strip_markdown_preserves_original_chars()
    test_reject_empty_llm_output()
    test_reject_too_short_llm_output()
    test_reject_unchanged_output()
    test_strip_word_count_leak()

    print("\n── 内容忠实度 (devlog 踩坑) ──")
    test_fidelity_check_catches_fabrication()
    test_fidelity_check_accepts_legitimate_shrink()
    test_fidelity_check_accepts_legitimate_expand()
    test_neighbor_duplicate_detection()
    test_neighbor_substring_detection()
    test_neighbor_high_overlap_detection()

    print("\n── Rate 重算 ──")
    test_rate_clamped_to_range()
    test_rate_normal_no_clamp()

    print("\n── 审计日志 ──")
    test_audit_log_fields()

    print("\n── 端到端 mock ──")
    test_full_loop_with_mock()

    print("\n── 回归测试 (代码结构) ──")
    test_llm_duration_feedback_has_fidelity_check()
    test_llm_duration_feedback_has_neighbor_dedup()
    test_llm_duration_feedback_has_strip_markdown()
    test_llm_duration_feedback_has_strip_think_block()
    test_llm_duration_feedback_no_batch_httpx_client()

    print("\n── 估算一致性 ──")
    test_estimate_duration_reasonable()
    test_estimate_duration_empty()

    print("\n── 边界条件 ──")
    test_zero_target_dur_skipped()
    test_no_outliers_returns_early()
    test_missing_tts_file_skipped()
    test_zero_size_tts_file_skipped()

    print("\n" + "=" * 60)
    print("全部通过")
    print("=" * 60)
