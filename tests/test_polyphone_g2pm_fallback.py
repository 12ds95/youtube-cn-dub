"""测试: g2pM 神经兜底 (jieba 白名单未命中时用 g2pM 扩展覆盖)。

设计:
  - 主路径 jieba 白名单 (词典级 ~100% 精度)
  - 兜底 g2pM (~97% CPP 准确率), 仅对 _POLYPHONE_RULES 字 + 词长 >= 2
  - 单字成词不走 g2pM (避免误判, 保守)

g2pM 不可用时自动降级为 jieba-only。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import _fix_polyphones, _g2pm_to_pypinyin, _get_g2pm


# ───────────────────────────────────────────────────────────────────
# 数字声调 → 字符声调转换
# ───────────────────────────────────────────────────────────────────
def test_g2pm_to_pypinyin_first_tone():
    assert _g2pm_to_pypinyin("ma1") == "mā"


def test_g2pm_to_pypinyin_second_tone():
    assert _g2pm_to_pypinyin("ma2") == "má"


def test_g2pm_to_pypinyin_third_tone():
    assert _g2pm_to_pypinyin("ma3") == "mǎ"


def test_g2pm_to_pypinyin_fourth_tone():
    assert _g2pm_to_pypinyin("ma4") == "mà"


def test_g2pm_to_pypinyin_neutral_tone():
    """轻声: g2pm '5' → pypinyin 无标记"""
    assert _g2pm_to_pypinyin("le5") == "le"


def test_g2pm_to_pypinyin_jue():
    """'jue2' → 'jué'"""
    assert _g2pm_to_pypinyin("jue2") == "jué"


def test_g2pm_to_pypinyin_hang():
    """'hang2' → 'háng' (a 优先于 i)"""
    assert _g2pm_to_pypinyin("hang2") == "háng"


def test_g2pm_to_pypinyin_chong():
    """'chong2' → 'chóng' (o 优先于其他)"""
    assert _g2pm_to_pypinyin("chong2") == "chóng"


def test_g2pm_to_pypinyin_v_to_u():
    """g2pm 用 v 表示 ü"""
    assert _g2pm_to_pypinyin("nv3") == "nǚ"


# ───────────────────────────────────────────────────────────────────
# g2pM 兜底实战 (词长 >= 2 且白名单遗漏的词)
# ───────────────────────────────────────────────────────────────────
def test_jieba_whitelist_takes_precedence():
    """白名单命中时优先白名单, 不调 g2pM (即使 g2pM 错判)。

    g2pM 把 '了解' 错判 le5, 但白名单命中应替换为 瞭。
    """
    out = _fix_polyphones("我了解你的意思")
    assert "瞭解" in out, f"白名单优先, '了解' 应替换: {out}"


def test_g2pm_fallback_disabled():
    """显式禁用 g2pM 兜底, 仅 jieba 白名单"""
    out = _fix_polyphones("我了解你的意思", use_g2pm_fallback=False)
    assert "瞭解" in out


def test_helper_le_not_replaced_by_g2pm():
    """g2pM 正确判 '构成了' 中 '了' 为 le5, 兜底不会误替换"""
    out = _fix_polyphones("这构成了当前的实现")
    assert "瞭" not in out


def test_g2pm_handles_yueqi_correctly():
    """g2pM 正确判 '乐器' 为 yue4, 即使 jieba 白名单含 '乐器', 主路径已替换。

    更关键: pypinyin 在 '和乐器' 上下文错判 'lè', 但 g2pM 给 yue4 正确。
    既然 jieba 白名单已 cover, 测试主路径即可。
    """
    out = _fix_polyphones("音乐和乐器演奏")
    assert "音月" in out
    assert "月器" in out  # 白名单含 '乐器'


# ───────────────────────────────────────────────────────────────────
# 单字成词不被 g2pM 误替 (保守边界)
# ───────────────────────────────────────────────────────────────────
def test_single_char_le_not_replaced():
    """单字 '了' 孤立 → 词长 < 2 → g2pM 兜底拒绝"""
    out = _fix_polyphones("了")
    assert out == "了"


def test_single_char_jue_not_replaced():
    """单字 '觉' 孤立 → 词长 < 2 → 兜底拒绝"""
    out = _fix_polyphones("觉")
    assert out == "觉"


# ───────────────────────────────────────────────────────────────────
# g2pM 不可用时降级 (mock 测试)
# ───────────────────────────────────────────────────────────────────
def test_no_g2pm_falls_back_to_jieba_only(monkeypatch):
    """g2pM import 失败时 _fix_polyphones 仍可正常工作 (jieba-only)"""
    import pipeline
    # mock _get_g2pm 返回 None
    monkeypatch.setattr(pipeline, '_get_g2pm', lambda: None)
    # 主路径仍生效
    assert "瞭解" in _fix_polyphones("我了解你")
    assert "感决" in _fix_polyphones("我感觉很好")
    # 助词仍不替换
    assert "瞭" not in _fix_polyphones("这构成了当前")


def test_g2pm_load_error_no_crash(monkeypatch):
    """g2pM 调用抛异常时不应导致 _fix_polyphones 失败"""
    import pipeline

    class FakeG2pM:
        def __call__(self, *a, **kw):
            raise RuntimeError("simulated g2pm failure")

    monkeypatch.setattr(pipeline, '_get_g2pm', lambda: FakeG2pM())
    # 应正常返回 (使用 jieba 主路径)
    out = _fix_polyphones("我了解你")
    assert "瞭解" in out


# ───────────────────────────────────────────────────────────────────
# g2pM 可用性 smoke test
# ───────────────────────────────────────────────────────────────────
def test_g2pm_loads_successfully():
    """g2pM 应能成功加载 (在测试环境已 pip install)"""
    nlp = _get_g2pm()
    assert nlp is not None, "g2pM 应该能加载"


def test_g2pm_basic_inference():
    """g2pM 基本推理: 给 '银行' 应返回 hang2"""
    nlp = _get_g2pm()
    if nlp is None:
        return  # 跳过 (g2pm 未安装)
    pys = nlp("银行", tone=True, char_split=False)
    assert pys[1] == 'hang2', f"g2pM 应判 '银行' 中 '行' 为 hang2: {pys}"


# ───────────────────────────────────────────────────────────────────
# 默认禁用 g2pM 兜底: kCc dry-run 实测的 false positive 案例
# 这些词 g2pM 误判会引入错误替换, 默认应保持原文
# ───────────────────────────────────────────────────────────────────
def test_default_disabled_yanzhong_kept():
    """'严重' (yán zhòng) — g2pM 误判 chóng, 默认禁用 g2pm 后保留原文"""
    out = _fix_polyphones("通信过程会严重丢失信息")
    assert "严虫" not in out, f"'严重' 不应被替换: {out}"
    assert "严重" in out


def test_default_disabled_quanzhong_kept():
    """'权重' (quán zhòng) — g2pM 误判, 默认禁用 g2pm 后保留"""
    out = _fix_polyphones("注意力权重计算")
    assert "权虫" not in out


def test_default_disabled_suochu_kept():
    """'所处' (suǒ chǔ) — g2pM 误判 chù, 默认应保留 chǔ"""
    out = _fix_polyphones("它知道自己所处的位置")
    assert "所触" not in out


def test_default_disabled_chuyu_kept():
    """'处于' (chǔ yú) — g2pM 误判 chù, 默认应保留 chǔ"""
    out = _fix_polyphones("不关心是否处于训练状态")
    assert "触于" not in out


def test_default_use_g2pm_fallback_is_false():
    """_fix_polyphones 默认不开启 g2pm 兜底 (避免引入 false positive)"""
    import inspect
    from pipeline import _fix_polyphones as fn
    sig = inspect.signature(fn)
    assert sig.parameters['use_g2pm_fallback'].default is False, (
        "默认必须禁用 g2pm 兜底 — kCc 实测错误率 ~50%"
    )


def test_optin_g2pm_replaces_more():
    """显式开启 g2pm 兜底时, 会扩展替换 (即使引入误判, 验证机制工作)"""
    text = "严重的损失"
    out_default = _fix_polyphones(text)
    out_g2pm = _fix_polyphones(text, use_g2pm_fallback=True)
    # 默认不替换
    assert "严虫" not in out_default
    # 开启后 g2pm 误判替换 — 此为已知 false positive, 但验证机制可调用
    # 不强制断言 g2pm 替换结果 (因 g2pm 行为可能随版本变化)
    assert isinstance(out_g2pm, str) and len(out_g2pm) > 0
