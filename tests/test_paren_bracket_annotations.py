"""测试: strip_parenthetical_annotations 支持多种括号符号
   ()、（）、《》、[]、【】、{} 都需被识别为注解候选。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_book_quote_redundant_removed():
    """《》冗余注解 (前文已是相同含义) → 去除"""
    from text_utils import strip_parenthetical_annotations
    s = "Ben 在此搭建了可交互式教学视频《Explorable Videos》"
    out = strip_parenthetical_annotations(s)
    assert "《Explorable Videos》" not in out, f"冗余《》应去除, got: {out!r}"
    assert "可交互式教学视频" in out


def test_book_quote_unique_preserved():
    """《》在独立语境 (前文非同义) → 保留"""
    from text_utils import strip_parenthetical_annotations
    s = "我看了《Home Alone》这部电影"
    out = strip_parenthetical_annotations(s)
    # 这是真实书名/影名引用，不应去除
    assert "《Home Alone》" in out, f"独立《》应保留, got: {out!r}"


def test_square_bracket_redundant_removed():
    """[] 冗余注解 → 去除"""
    from text_utils import strip_parenthetical_annotations
    s = "可交互式教学视频[Explorable Videos]"
    out = strip_parenthetical_annotations(s)
    assert "[Explorable Videos]" not in out, f"冗余[]应去除, got: {out!r}"


def test_curly_brace_redundant_removed():
    """{} 冗余注解 → 去除"""
    from text_utils import strip_parenthetical_annotations
    s = "可交互式教学视频{Explorable Videos}"
    out = strip_parenthetical_annotations(s)
    assert "{Explorable Videos}" not in out, f"冗余{{}}应去除, got: {out!r}"


def test_full_square_bracket_redundant_removed():
    """【】 冗余注解 → 去除"""
    from text_utils import strip_parenthetical_annotations
    s = "可交互式教学视频【Explorable Videos】"
    out = strip_parenthetical_annotations(s)
    assert "【Explorable Videos】" not in out, f"冗余【】应去除, got: {out!r}"


def test_paren_redundant_still_works():
    """原有 () 行为保持: 冗余括号去除"""
    from text_utils import strip_parenthetical_annotations
    s = "可交互式教学视频(Explorable Videos)"
    out = strip_parenthetical_annotations(s)
    assert "(Explorable Videos)" not in out, f"冗余()应去除, got: {out!r}"


def test_full_paren_redundant_still_works():
    """原有 （） 行为保持"""
    from text_utils import strip_parenthetical_annotations
    s = "可交互式教学视频（Explorable Videos）"
    out = strip_parenthetical_annotations(s)
    assert "（Explorable Videos）" not in out, f"冗余（）应去除, got: {out!r}"


def test_no_latin_inside_preserved():
    """括号内无拉丁字母 → 保留 (纯中文解释)"""
    from text_utils import strip_parenthetical_annotations
    s = "我看了一部电影《小鬼当家》"
    out = strip_parenthetical_annotations(s)
    assert "《小鬼当家》" in out, f"纯中文《》应保留, got: {out!r}"


def test_no_brackets_at_all():
    """文本无任何括号 → 原样返回"""
    from text_utils import strip_parenthetical_annotations
    s = "这是一段普通文本，没有括号"
    out = strip_parenthetical_annotations(s)
    assert out == s


def test_empty_input():
    from text_utils import strip_parenthetical_annotations
    assert strip_parenthetical_annotations("") == ""
    assert strip_parenthetical_annotations(None) is None
