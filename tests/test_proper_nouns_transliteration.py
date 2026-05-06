"""测试: verify_proper_nouns 对拼音/音译/大小写的放松判定"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_transliteration_accepted_andy():
    """Andy Matuszczak → 安迪·马图扎克 是合法音译, 不应触发 noun_lost"""
    from translation_style import verify_proper_nouns
    segs = [{
        "text_en": "Take Andy Matuszczak for example, a friend of mine who used to work at Apple",
        "text_zh": "举个例子，我一位曾供职于苹果的朋友——安迪·马图扎克——",
    }]
    issues = verify_proper_nouns(segs, ["Andy Matuszczak"])
    assert issues == [], f"音译应通过, got: {issues}"


def test_transliteration_accepted_ben_eater():
    """Ben Eater → 本·伊瑟 (拼音相似度边界, 应通过)"""
    from translation_style import verify_proper_nouns
    segs = [{
        "text_en": "It was done in collaboration with Ben Eater, who runs a channel",
        "text_zh": "这是在与本·伊瑟合作下完成的；他运营着一个频道",
    }]
    issues = verify_proper_nouns(segs, ["Ben Eater"])
    assert issues == [], f"音译应通过, got: {issues}"


def test_unrelated_chinese_not_accepted():
    """无关中文不应误判为音译"""
    from translation_style import verify_proper_nouns
    segs = [{
        "text_en": "Apple is great",
        "text_zh": "梨子很棒",  # 完全无关
    }]
    issues = verify_proper_nouns(segs, ["Apple (苹果)"])
    assert len(issues) == 1
    assert issues[0]["kind"] == "noun_lost"


def test_case_insensitive_preservation():
    """text_zh 中保留小写英文 (如 URL 中 'eater.net') 也算保留"""
    from translation_style import verify_proper_nouns
    segs = [{
        "text_en": "Find the link: eater.net/quaternions",
        "text_zh": "可在 eater.net/quaternions 找到链接",
    }]
    issues = verify_proper_nouns(segs, ["Eater"])
    assert issues == [], f"小写形式应视为保留, got: {issues}"


def test_zh_alt_still_works():
    """显式 zh_alt 仍然必须能匹配"""
    from translation_style import verify_proper_nouns
    segs = [{
        "text_en": "Apple devices",
        "text_zh": "苹果设备",
    }]
    issues = verify_proper_nouns(segs, ["Apple (苹果)"])
    assert issues == []


def test_short_anchor_still_skipped():
    """单字母 anchor 仍被跳过"""
    from translation_style import verify_proper_nouns
    segs = [{"text_en": "I am here", "text_zh": "我在这里"}]
    issues = verify_proper_nouns(segs, ["I"])
    assert issues == []
