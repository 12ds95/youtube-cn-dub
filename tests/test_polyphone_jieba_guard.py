"""测试: _fix_polyphones 双重守卫 (pypinyin + jieba 词级白名单)。

修复 pypinyin 误判:
  - "构成了"/"查看了" 中"了" 被错误判为 liǎo, 应保留 le (不替换)
  - 仅当字所在 jieba 词命中白名单时才替换

补漏字:
  - "觉" (jué/jiào): edge-tts 默认 jiào, 但 "直觉/感觉/觉得/觉悟" 应读 jué
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import _fix_polyphones


# ───────────────────────────────────────────────────────────────────
# "了" 字: 助词 le vs 动词 liǎo 区分
# ───────────────────────────────────────────────────────────────────
def test_le_helper_after_verb_not_replaced():
    """'构成了当前的实现' — '了' 是助词 le, 不应替换"""
    out = _fix_polyphones("这构成了当前的实现")
    assert "瞭" not in out, f"助词 le 不应替换为 瞭: {out}"


def test_le_helper_after_action_not_replaced():
    """'向下滚动查看了结果' — '了' 是助词 le, 不应替换"""
    out = _fix_polyphones("向下滚动查看了结果")
    assert "瞭" not in out, f"助词 le 不应替换: {out}"


def test_le_simple_helper_not_replaced():
    """'我去了' — 句末助词, 不应替换"""
    out = _fix_polyphones("我去了")
    assert "瞭" not in out


def test_liao_in_liaojie_replaced():
    """'我了解你' — '了解' 应读 liǎo jiě, 替换为 瞭"""
    out = _fix_polyphones("我了解你的意思")
    assert "瞭解" in out, f"'了解' 应替换: {out}"


def test_liao_in_minliao_replaced():
    """'清晰明了' — '明了' 应读 míng liǎo, 替换为 瞭"""
    out = _fix_polyphones("清晰明了")
    assert "明瞭" in out, f"'明了' 应替换: {out}"


def test_liao_in_liaoduan_replaced():
    """'了断恩怨' — '了断' 应读 liǎo duàn"""
    out = _fix_polyphones("了断恩怨")
    assert "瞭断" in out


# ───────────────────────────────────────────────────────────────────
# "觉" 字: jué (觉得/感觉) vs jiào (睡觉)
# ───────────────────────────────────────────────────────────────────
def test_jue_in_zhijue_replaced():
    """'我的直觉' — '直觉' 应读 zhí jué, edge-tts 默认 jiào, 替换为 决"""
    out = _fix_polyphones("我的直觉")
    assert "直决" in out, f"'觉' 应替换为 决: {out}"


def test_jue_in_ganjue_replaced():
    """'我感觉' — '感觉' 应读 gǎn jué"""
    out = _fix_polyphones("我感觉很奇怪")
    assert "感决" in out, f"'觉' 应替换为 决: {out}"


def test_jue_in_juede_replaced():
    """'觉得' — 应读 jué de"""
    out = _fix_polyphones("我觉得这个不对")
    assert "决得" in out


def test_jiao_in_shuijiao_kept():
    """'睡觉' — 应读 shuì jiào, edge-tts 默认 jiào 正确, 不应替换"""
    out = _fix_polyphones("他要去睡觉了")
    assert "睡决" not in out, f"'睡觉' 不应替换: {out}"
    assert "睡觉" in out


# ───────────────────────────────────────────────────────────────────
# "处" 字: chǔ (动词处理) vs chù (名词此处)
# ───────────────────────────────────────────────────────────────────
def test_chu_locator_replaced():
    """'在此处' — chù, edge-tts 默认 chǔ 错, 替换为 触"""
    out = _fix_polyphones("在此处")
    assert "此触" in out


def test_chu_verb_kept():
    """'正在处理' — chǔ, edge-tts 默认正确, 不应替换"""
    out = _fix_polyphones("正在处理数据")
    assert "触理" not in out


# ───────────────────────────────────────────────────────────────────
# "重" 字: chóng (重新) vs zhòng (重要)
# ───────────────────────────────────────────────────────────────────
def test_chong_chongxin_replaced():
    """'重新开始' — chóng, 替换为 虫"""
    out = _fix_polyphones("重新开始")
    assert "虫新" in out


def test_chong_chongfu_replaced():
    """'重复' — chóng"""
    out = _fix_polyphones("不要重复")
    assert "虫复" in out


def test_zhong_zhongyao_kept():
    """'重要' — zhòng, edge-tts 默认正确, 不应替换"""
    out = _fix_polyphones("这很重要")
    assert "虫要" not in out


# ───────────────────────────────────────────────────────────────────
# "行" 字: háng (银行/行业) vs xíng (执行)
# ───────────────────────────────────────────────────────────────────
def test_hang_yinhang_replaced():
    """'银行' — háng, 替换为 杭"""
    out = _fix_polyphones("去银行办事")
    assert "银杭" in out


def test_xing_zhixing_kept():
    """'执行' — xíng, 默认正确, 不应替换"""
    out = _fix_polyphones("执行命令")
    assert "杭" not in out


# ───────────────────────────────────────────────────────────────────
# 调 调用 / 协调 区分
# ───────────────────────────────────────────────────────────────────
def test_tiao_xietiao_replaced():
    """'协调' — tiáo, 替换为 条"""
    out = _fix_polyphones("协调各方")
    assert "协条" in out


def test_diao_diaoyong_kept():
    """'调用' — diào, 默认正确, 不应替换"""
    out = _fix_polyphones("调用函数")
    assert "条用" not in out


# ───────────────────────────────────────────────────────────────────
# 边界: 空文本 / 无多音字 / pypinyin 异常
# ───────────────────────────────────────────────────────────────────
def test_empty_text():
    assert _fix_polyphones("") == ""


def test_no_polyphone():
    out = _fix_polyphones("普通中文文本无多音字")
    assert out == "普通中文文本无多音字"


def test_punctuation_preserved():
    out = _fix_polyphones("我了解你，所以构成了这个！")
    # "了解" 应替换, "构成了" 不替换
    assert "瞭解" in out
    assert "构成瞭" not in out
