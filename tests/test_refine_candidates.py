#!/usr/bin/env python3
"""
测试 LLM 精简候选解析 + 最优候选选择。

来源: pyvideotrans — <TRANSLATE_TEXT> 标签提取测试模式
       Google Ariel — 空响应/异常响应处理测试
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import (
    _parse_multi_candidates, _clean_refine_artifacts, _select_best_candidate,
    _identify_high_cps_segments, _fix_polyphones,
    _identify_low_cps_segments, _parse_expand_candidates,
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


# ── allow_same_length (等时翻译) 测试 ──

def test_select_allow_same_length_accepts_equal():
    """allow_same_length=True: 同等长度候选不被排除"""
    original = "这是原始翻译文本内容"
    segments = [{"text_zh": original, "start": 0, "end": 5}]
    # 候选和原文一样长，且有足够字符重叠（满足 fidelity >= 0.25）
    same_len = "这是改编翻译文本版本"
    assert len(same_len) == len(original)
    result = _select_best_candidate(
        [same_len], 5000, original, 0, segments,
        allow_same_length=True)
    assert result == same_len


def test_select_default_rejects_same_length():
    """默认行为: 同等长度候选被排除"""
    original = "这是原始翻译文本内容"
    segments = [{"text_zh": original, "start": 0, "end": 5}]
    same_len = "这是改编翻译文本版本"
    assert len(same_len) == len(original)
    result = _select_best_candidate(
        [same_len], 5000, original, 0, segments,
        allow_same_length=False)
    assert result == ""


def test_select_allow_same_length_rejects_much_longer():
    """allow_same_length=True: 超过原文 110% 仍被排除"""
    original = "短文"
    segments = [{"text_zh": original, "start": 0, "end": 5}]
    # 远超 110%
    long_cand = "这是一个远超原文长度百分之一百一十的候选"
    result = _select_best_candidate(
        [long_cand], 5000, original, 0, segments,
        allow_same_length=True)
    assert result == ""


# ── _identify_high_cps_segments 测试 ──

def test_identify_short_segment_skipped():
    """target_ms <= 500 的超短段不应被标记"""
    segments = [{"text_zh": "测试很长的文本内容", "text_en": "test", "start": 0, "end": 0.3}]
    result = _identify_high_cps_segments(segments, cps_threshold=5.5)
    assert result == []


def test_identify_few_chars_skipped():
    """字数 < 3 的段不应被标记"""
    segments = [{"text_zh": "是", "text_en": "yes", "start": 0, "end": 3}]
    result = _identify_high_cps_segments(segments, cps_threshold=5.5)
    assert result == []


def test_identify_normal_cps_not_flagged():
    """CPS 正常的段不应被标记 (10字/3秒 ≈ 3.3 CPS)"""
    segments = [{"text_zh": "这是一个正常语速的翻译", "text_en": "normal", "start": 0, "end": 3}]
    result = _identify_high_cps_segments(segments, cps_threshold=5.5)
    assert result == []


def test_identify_high_cps_flagged():
    """CPS 超标的段应被标记 (30字/3秒 = 10 CPS)"""
    segments = [{
        "text_zh": "这是一个非常长的翻译文本用来测试高语速段的识别功能是否正常工作",
        "text_en": "test", "start": 0, "end": 3,
    }]
    result = _identify_high_cps_segments(segments, cps_threshold=5.5)
    assert 0 in result


def test_identify_mixed_segments():
    """混合场景：只标记高 CPS 段"""
    segments = [
        {"text_zh": "正常", "text_en": "ok", "start": 0, "end": 3},         # 太短（<3字）
        {"text_zh": "这是正常的翻译", "text_en": "normal", "start": 3, "end": 6},  # 正常
        {"text_zh": "这是一个非常长的翻译文本用来测试高语速段的识别功能是否正常工作", "text_en": "long", "start": 6, "end": 9},  # 高CPS
    ]
    result = _identify_high_cps_segments(segments, cps_threshold=5.5)
    assert 0 not in result  # 太短跳过
    assert 1 not in result  # 正常
    assert 2 in result      # 高CPS


def test_isometric_config_default_disabled():
    """DEFAULT_CONFIG 中 isometric 默认关闭"""
    from pipeline import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["llm"]["isometric"] == 0
    assert DEFAULT_CONFIG["llm"]["isometric_cps_threshold"] == 5.5


# ── _fix_polyphones 多音字替换测试 ──

def test_polyphone_liao_le():
    """了解/了然 → 瞭解/瞭然 (liǎo, not le)"""
    assert _fix_polyphones("了解这个概念") == "瞭解这个概念"
    assert _fix_polyphones("了不起的成就") == "瞭不起的成就"


def test_polyphone_hang_xing():
    """银行/行业 → 银杭/杭业 (háng, not xíng)"""
    assert _fix_polyphones("银行系统") == "银杭系统"
    assert _fix_polyphones("行业标准") == "杭业标准"
    # "行动" 应保持不变 (xíng)
    assert _fix_polyphones("行动计划") == "行动计划"


def test_polyphone_chong_zhong():
    """重新/重复 → 虫新/虫复 (chóng, not zhòng)"""
    assert _fix_polyphones("重新启动") == "虫新启动"
    assert _fix_polyphones("重复操作") == "虫复操作"
    assert _fix_polyphones("重建索引") == "虫建索引"


def test_polyphone_tiao_diao():
    """调整/协调 → 条整/协条 (tiáo, not diào)"""
    assert _fix_polyphones("调整参数") == "条整参数"
    assert _fix_polyphones("协调工作") == "协条工作"
    # "调用" 应保持不变 (diào)
    assert _fix_polyphones("调用函数") == "调用函数"


def test_polyphone_shuai_lv():
    """率领/率先 → 帅领/帅先 (shuài, not lǜ)"""
    assert _fix_polyphones("率领团队") == "帅领团队"
    assert _fix_polyphones("率先发布") == "帅先发布"
    # "效率" 应保持不变 (lǜ)
    assert _fix_polyphones("效率很高") == "效率很高"


def test_polyphone_ying():
    """应该/应当 → 英该/英当 (yīng, not yìng)"""
    assert _fix_polyphones("应该注意") == "英该注意"
    assert _fix_polyphones("应当处理") == "英当处理"
    # "应用" 应保持不变 (yìng)
    assert _fix_polyphones("应用场景") == "应用场景"


def test_polyphone_yue_le():
    """音乐/乐器 → 音月/月器 (yuè, not lè)"""
    assert _fix_polyphones("音乐播放") == "音月播放"
    assert _fix_polyphones("乐器演奏") == "月器演奏"
    # "乐趣" 应保持不变 (lè)
    assert _fix_polyphones("乐趣无穷") == "乐趣无穷"


def test_polyphone_di_de():
    """的确 → 滴确 (dí, not de)"""
    assert _fix_polyphones("的确如此") == "滴确如此"


def test_polyphone_chai_cha():
    """出差/差遣 → 出拆/拆遣 (chāi, not chā)"""
    assert _fix_polyphones("出差在外") == "出拆在外"
    assert _fix_polyphones("差遣部队") == "拆遣部队"
    # "差异" 应保持不变 (chā)
    assert _fix_polyphones("差异很大") == "差异很大"


def test_polyphone_empty_and_none():
    """空字符串 / 无多音字"""
    assert _fix_polyphones("") == ""
    assert _fix_polyphones("普通文本没有多音字") == "普通文本没有多音字"


def test_polyphone_multiple_in_sentence():
    """一句话中多个多音字同时替换"""
    text = "银行应该重新调整"
    result = _fix_polyphones(text)
    assert "银杭" in result  # 行 → 杭
    assert "英该" in result  # 应该 → 英该
    assert "虫新" in result  # 重新 → 虫新
    assert "条整" in result  # 调整 → 条整


def test_polyphone_mixed_chinese_english():
    """中英混合文本：pypinyin 对齐不错位"""
    assert _fix_polyphones("Hello了解世界") == "Hello瞭解世界"
    assert _fix_polyphones("Test重新启动End") == "Test虫新启动End"


def test_polyphone_with_digits():
    """含数字文本：数字不干扰对齐"""
    assert _fix_polyphones("数不胜数") == "属不胜属"
    # 行 在「第3行代码」中读 xíng，不应替换
    assert _fix_polyphones("第3行代码") == "第3行代码"


def test_polyphone_no_false_positive():
    """edge-tts 默认读音正确的场景不应替换"""
    assert _fix_polyphones("重点很重要") == "重点很重要"  # zhòng
    assert _fix_polyphones("行走江湖") == "行走江湖"      # xíng
    assert _fix_polyphones("调用函数") == "调用函数"      # diào
    assert _fix_polyphones("效率很高") == "效率很高"      # lǜ
    assert _fix_polyphones("应用场景") == "应用场景"      # yìng
    assert _fix_polyphones("好处和坏处") == "好触和坏触"  # chù → 触


# ── _identify_low_cps_segments 测试 ──

def test_identify_low_cps_basic():
    """CPS < 3.5 的段应被标记"""
    segments = [{
        "text_zh": "这是一个简短文本",  # 7 chars / 5s = 1.4 CPS
        "text_en": "short text", "start": 0, "end": 5,
    }]
    result = _identify_low_cps_segments(segments, cps_threshold=3.5)
    assert 0 in result


def test_identify_low_cps_normal_not_flagged():
    """CPS 正常的段不应被标记 (10字/2秒 = 5.0 CPS)"""
    segments = [{"text_zh": "这是一个比较正常的翻译文本段", "text_en": "normal", "start": 0, "end": 3}]
    result = _identify_low_cps_segments(segments, cps_threshold=3.5)
    assert result == []


def test_identify_low_cps_skip_short_segment():
    """target_ms <= 500 的超短段不应被标记"""
    segments = [{"text_zh": "很短的文本", "text_en": "test", "start": 0, "end": 0.4}]
    result = _identify_low_cps_segments(segments, cps_threshold=3.5)
    assert result == []


def test_identify_low_cps_skip_few_chars():
    """字数 < 3 的段不应被标记"""
    segments = [{"text_zh": "是", "text_en": "yes", "start": 0, "end": 5}]
    result = _identify_low_cps_segments(segments, cps_threshold=3.5)
    assert result == []


# ── _parse_expand_candidates 测试 ──

def test_parse_expand_candidates_standard():
    """标准 [N] + [轻扩][中扩][重扩] 格式"""
    content = """[1]
[轻扩] 这是轻度扩展版本一号文本
[中扩] 这是中度扩展版本一号文本内容
[重扩] 这是重度扩展版本一号文本内容加上更多细节

[2]
[轻扩] 轻度扩展版本二
[中扩] 中度扩展版本二文本
[重扩] 重度扩展版本二更长的文本"""
    result = _parse_expand_candidates(content, 2)
    assert len(result) == 2
    assert len(result[0]) == 3
    assert "轻度扩展版本一号文本" in result[0][0]
    assert "重度扩展版本二" in result[1][2]


def test_parse_expand_candidates_markdown():
    """markdown 加粗变体"""
    content = """**[1]**
**[轻扩]** 轻度版本
**[中扩]** 中度版本
**[重扩]** 重度版本"""
    result = _parse_expand_candidates(content, 1)
    assert len(result) == 1
    assert len(result[0]) == 3
    assert result[0][0] == "轻度版本"


def test_parse_expand_candidates_empty():
    """空内容返回填充空列表"""
    result = _parse_expand_candidates("", 2)
    assert len(result) == 2
    assert result[0] == []
    assert result[1] == []


def test_parse_expand_candidates_system_echo_skipped():
    """LLM 回显系统指令行应被跳过"""
    content = """以下为每段翻译的[轻扩]/[中扩]/[重扩]三个版本：

[1]
[轻扩] 实际扩展
[中扩] 中度
[重扩] 重度"""
    result = _parse_expand_candidates(content, 1)
    assert len(result[0]) == 3
    assert "实际扩展" in result[0][0]


# ── _select_best_candidate fill mode 测试 ──

def test_select_fill_mode_prefers_longest():
    """fill 模式: 选不超 target 的最长候选"""
    original = "这是一段翻译文本内容示例"  # 12 chars, 2x cap = 24
    segments = [{"text_zh": original, "start": 0, "end": 10}]
    # 候选比原文长，且共享核心词汇，均在 2x 上限内
    short_expand = "这是一段更加详细的翻译文本内容"  # 14 chars
    long_expand = "这是一段更加详细且完整的翻译文本内容示例"  # 19 chars, < 24
    candidates = [short_expand, long_expand]
    result = _select_best_candidate(
        candidates, 10000, original, 0, segments,
        mode="fill", fidelity_threshold=0.15)
    # 应选最长的（都不超 target 10s）
    assert result == long_expand


def test_select_fill_mode_rejects_shorter():
    """fill 模式: 比原文短的候选被拒绝"""
    original = "这是原始的翻译文本内容"
    segments = [{"text_zh": original, "start": 0, "end": 10}]
    # 候选比原文短
    result = _select_best_candidate(
        ["短"], 10000, original, 0, segments,
        mode="fill", fidelity_threshold=0.15)
    assert result == ""


def test_select_fill_mode_caps_at_2x():
    """fill 模式: 超过原文 2 倍长度被拒绝"""
    original = "短文"
    segments = [{"text_zh": original, "start": 0, "end": 10}]
    # 远超 2x
    long_cand = "这是一个远远超过原文两倍长度的极长候选文本用来测试上限过滤功能是否正常工作应该被拒绝"
    result = _select_best_candidate(
        [long_cand], 10000, original, 0, segments,
        mode="fill", fidelity_threshold=0.10)
    assert result == ""


def test_select_fill_backward_compatible():
    """不传 mode 参数时行为不变 (默认 shrink)"""
    original = "这是原始翻译文本内容"
    segments = [{"text_zh": original, "start": 0, "end": 5}]
    shorter = "精简版文本"
    result = _select_best_candidate(
        [shorter], 5000, original, 0, segments)
    assert result == shorter


# ── _clean_refine_artifacts 扩展标签测试 ──

def test_clean_expand_light_tag():
    assert _clean_refine_artifacts("[轻扩] 扩展后的文本") == "扩展后的文本"


def test_clean_expand_medium_tag():
    assert _clean_refine_artifacts("[中扩] 中度扩展") == "中度扩展"


def test_clean_expand_heavy_tag():
    assert _clean_refine_artifacts("[重扩] 重度扩展") == "重度扩展"


def test_clean_expand_system_echo():
    """LLM 回显扩展系统指令 → 返回空"""
    result = _clean_refine_artifacts("以下为[轻扩]/[中扩]/[重扩]三个版本")
    assert result == ""


# ── 配置默认值测试 ──

def test_expand_config_default():
    """DEFAULT_CONFIG 中 isometric_expand_cps_threshold 默认 3.5"""
    from pipeline import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["llm"]["isometric_expand_cps_threshold"] == 3.5


if __name__ == "__main__":
    print("精简候选解析测试:")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✅ {name}")
    print("  全部通过")
