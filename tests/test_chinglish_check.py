"""测试: 中英混杂 deterministic 后校验
   text_zh 中包含 4+字符连续英文,且不在 proper_nouns / URL 白名单 → 标记为 chinglish。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_detect_normal_english_word():
    """普通英文词 (e.g. 'hosting') 在中文中 → 标记"""
    from translation_style import detect_chinglish_issues
    segs = [
        {"text_en": "EN", "text_zh": "网站上 hosting 一段视频"},
    ]
    issues = detect_chinglish_issues(segs, proper_nouns=[])
    assert len(issues) == 1
    assert "hosting" in issues[0]["leftover"]
    assert issues[0]["idx"] == 0


def test_proper_noun_not_flagged():
    """proper_nouns 列表中的英文 → 不标记"""
    from translation_style import detect_chinglish_issues
    segs = [{"text_en": "EN", "text_zh": "这是与 Ben Eater 合作完成的"}]
    issues = detect_chinglish_issues(segs, proper_nouns=["Ben Eater"])
    assert issues == []


def test_url_not_flagged():
    """URL (含 .net / .com / .org) → 不标记"""
    from translation_style import detect_chinglish_issues
    segs = [{"text_en": "EN", "text_zh": "请访问 eater.net/quaternions"}]
    issues = detect_chinglish_issues(segs, proper_nouns=[])
    assert issues == []


def test_short_english_not_flagged():
    """3 字符以下的英文 (单字母变量/缩写) → 不标记"""
    from translation_style import detect_chinglish_issues
    segs = [{"text_en": "EN", "text_zh": "用i、j、k 分量表示"}]
    issues = detect_chinglish_issues(segs, proper_nouns=[])
    assert issues == []


def test_multi_word_proper_noun_partial():
    """proper_noun 中只出现部分单词 (e.g. 只列 'Ben Eater', 文中只有 'Ben') 也算保护"""
    from translation_style import detect_chinglish_issues
    segs = [{"text_en": "thanks to Ben.", "text_zh": "归功于 Ben"}]
    # proper_nouns 列出 'Ben Eater', 单独 'Ben' 也应被白名单覆盖
    issues = detect_chinglish_issues(segs, proper_nouns=["Ben Eater"])
    assert issues == []


def test_empty_proper_nouns_fine():
    """无 proper_nouns 列表也能工作"""
    from translation_style import detect_chinglish_issues
    segs = [{"text_en": "EN", "text_zh": "纯中文翻译"}]
    issues = detect_chinglish_issues(segs, proper_nouns=None)
    assert issues == []


def test_multiple_segments():
    """多段, 每段独立检查"""
    from translation_style import detect_chinglish_issues
    segs = [
        {"text_en": "EN1", "text_zh": "网站 deploy 在云端"},     # 含 deploy → 标记
        {"text_en": "EN2", "text_zh": "正常中文"},              # 干净 → 不标记
        {"text_en": "EN3", "text_zh": "请 click 这里"},         # 含 click → 标记
    ]
    issues = detect_chinglish_issues(segs, proper_nouns=[])
    assert len(issues) == 2
    assert {issue["idx"] for issue in issues} == {0, 2}
