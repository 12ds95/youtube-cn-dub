"""测试: proper_nouns 解析 + 翻译后 rule-base 校验。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_parse_simple_name():
    from translation_style import parse_proper_noun
    en, zh = parse_proper_noun("Ben Eater")
    assert en == "Ben Eater"
    assert zh is None


def test_parse_with_chinese_alt():
    from translation_style import parse_proper_noun
    en, zh = parse_proper_noun("Apple (苹果)")
    assert en == "Apple"
    assert zh == "苹果"


def test_parse_chinese_paren():
    from translation_style import parse_proper_noun
    en, zh = parse_proper_noun("Home Alone （《小鬼当家》）")
    assert en == "Home Alone"
    assert zh == "《小鬼当家》"


def test_verify_preserved_english():
    """中文中保留英文原文 → 通过"""
    from translation_style import verify_proper_nouns
    segs = [
        {"text_en": "Ben Eater is awesome", "text_zh": "Ben Eater 太棒了"},
    ]
    issues = verify_proper_nouns(segs, ["Ben Eater"])
    assert issues == []


def test_verify_preserved_chinese_alt():
    """中文用了指定的译法 → 通过"""
    from translation_style import verify_proper_nouns
    segs = [
        {"text_en": "I love Apple devices", "text_zh": "我爱苹果设备"},
    ]
    issues = verify_proper_nouns(segs, ["Apple (苹果)"])
    assert issues == []


def test_verify_lost_proper_noun():
    """中文既没保留原文也没用译法 → noun_lost"""
    from translation_style import verify_proper_nouns
    segs = [
        {"text_en": "Eater did awesome things", "text_zh": "《吃货》团队做了很棒的事"},
    ]
    issues = verify_proper_nouns(segs, ["Ben Eater (本·伊瑟)"])
    # 注意: en_anchor='Ben Eater', sample 中只有 'Eater', 不匹配 → 不会触发
    # 修改测试: 让原文中也有完整 proper_noun
    segs2 = [
        {"text_en": "Apple is great", "text_zh": "梨子很棒"},
    ]
    issues = verify_proper_nouns(segs2, ["Apple (苹果)"])
    assert len(issues) == 1
    assert issues[0]["kind"] == "noun_lost"
    assert issues[0]["idx"] == 0


def test_verify_short_anchor_skipped():
    """单字母 anchor 不查 (避免假阳性)"""
    from translation_style import verify_proper_nouns
    segs = [{"text_en": "I am here", "text_zh": "我在这里"}]
    issues = verify_proper_nouns(segs, ["I"])
    assert issues == []


def test_score_detection_prefers_anchored():
    """评分: anchor 覆盖率高 + 总数量多的检测胜出"""
    from translation_style import _score_detection
    sample = "Ben Eater runs a channel about Apple silicon and quaternions."
    detected_good = {
        "term_rules": ["Apple silicon → 苹果芯片", "quaternions → 四元数"],
        "proper_nouns": ["Ben Eater", "Apple (苹果)"],
    }
    detected_bad = {
        "term_rules": ["NonExistentThing → 某物"],
        "proper_nouns": [],
    }
    s1 = _score_detection(detected_good, sample)
    s2 = _score_detection(detected_bad, sample)
    assert s1 > s2


def test_detect_prompt_contains_proper_nouns():
    """识别 prompt 必须显式要求提取 proper_nouns"""
    from translation_style import _build_detect_prompt
    prompt = _build_detect_prompt("sample text", "title")
    assert "proper_nouns" in prompt
    assert "人名" in prompt
    # R2 改用"维基百科或公众认知中作为独立实体存在"做泛化判定标准
    assert "独立实体" in prompt or "维基百科" in prompt
