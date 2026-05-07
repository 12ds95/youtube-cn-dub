"""测试: g2pW (BERT, CPP 99%) 兜底特定字 — 方案 D 实施。

设计:
  - 主路径 jieba 白名单 (词典级 ~100% 精度)
  - 兜底: g2pW (BERT, CPP 99.08%, 我们域实测 ~88%) — 始终启用
  - g2pW 模型 ~450MB, import 期 prewarm; 缺失时降级 jieba-only

依赖 g2pw + bert-base-chinese 模型。CI 环境若无模型则跳过相应测试 (fallback to None)。
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import _fix_polyphones, _get_g2pw, _align_g2pw_pinyins


# ───────────────────────────────────────────────────────────────────
# g2pW 兜底始终启用 (无 opt-in 参数)
# ───────────────────────────────────────────────────────────────────
def test_no_use_g2pw_fallback_param():
    """_fix_polyphones 已移除 use_g2pw_fallback 参数 (g2pW 始终启用)"""
    import inspect
    from pipeline import _fix_polyphones as fn
    sig = inspect.signature(fn)
    assert 'use_g2pw_fallback' not in sig.parameters


# ───────────────────────────────────────────────────────────────────
# g2pW 拼音对齐工具 (跳过非汉字)
# ───────────────────────────────────────────────────────────────────
def test_align_g2pw_pure_chinese():
    """纯中文: 输入输出长度一致"""
    out = _align_g2pw_pinyins("银行", ["yín", "háng"])
    assert out == ["yín", "háng"]


def test_align_g2pw_with_punctuation():
    """含标点: 标点位置补空串, 拼音按汉字顺序对齐"""
    out = _align_g2pw_pinyins("银行,办事", ["yín", "háng", "bàn", "shì"])
    assert out == ["yín", "háng", "", "bàn", "shì"]


def test_align_g2pw_with_english():
    """中英混合: 英文位置补空串"""
    out = _align_g2pw_pinyins("Hello了解", ["liǎo", "jiě"])
    # H/e/l/l/o → 都是空串, 然后了/解 → liǎo/jiě
    assert out == ["", "", "", "", "", "liǎo", "jiě"]


def test_align_g2pw_pinyins_overflow():
    """拼音多于汉字数 → 返回 None (异常情况安全降级)"""
    out = _align_g2pw_pinyins("银行", ["yín", "háng", "extra"])
    assert out is None


def test_align_g2pw_pinyins_underflow():
    """拼音少于汉字数 → 部分填充 (前几个汉字), 剩下空串"""
    out = _align_g2pw_pinyins("银行办事", ["yín", "háng"])
    assert out == ["yín", "háng", "", ""]


# ───────────────────────────────────────────────────────────────────
# 主路径优先 (g2pw 不影响白名单命中行为)
# ───────────────────────────────────────────────────────────────────
def test_jieba_whitelist_priority_over_g2pw():
    """白名单命中时不调 g2pW (即使 g2pW 也对, 词典级足够)"""
    # 主路径 "了解" 在白名单, 应替换为 "瞭解"
    out = _fix_polyphones("我了解你")
    assert "瞭解" in out


# ───────────────────────────────────────────────────────────────────
# g2pW 不可用时降级
# ───────────────────────────────────────────────────────────────────
def test_no_g2pw_falls_back_to_jieba_only(monkeypatch):
    """g2pW 加载失败时主路径仍工作"""
    import pipeline
    monkeypatch.setattr(pipeline, '_get_g2pw', lambda: None)
    out = _fix_polyphones("我了解你")
    assert "瞭解" in out  # 主路径仍生效


def test_g2pw_inference_error_no_crash(monkeypatch):
    """g2pW 推理抛异常时不应崩溃"""
    import pipeline

    class FakeG2PW:
        def lazy_pinyin(self, *a, **kw):
            raise RuntimeError("simulated")

    monkeypatch.setattr(pipeline, '_get_g2pw', lambda: FakeG2PW())
    out = _fix_polyphones("我了解你")
    assert "瞭解" in out


# ───────────────────────────────────────────────────────────────────
# g2pW 实战测试 (BERT 推理, 默认跳过)
# ───────────────────────────────────────────────────────────────────
# g2pW + pytest + multiprocessing 在 macOS 有死锁倾向 (BERT forward 在
# pytest 监督下挂起 5min+)。实战验证用独立 smoke 脚本完成, 见
# scripts/smoke_test_g2pw.py 或 tmp 一次性脚本。
#
# 实测对比 (17 case): g2pW 88.2% vs g2pM 64.7%, 修复 5 个域内 false
# positive (严重/权重/所处/处于/了解), 详见
# docs/plan/2026-05-07-polyphone-fix-plan.md 阶段 3 章节。
#
# 此测试文件只覆盖集成层 (mock-based) 与对齐工具单元测试。

@pytest.mark.skip(reason="g2pW BERT 在 pytest 监督下挂起, 用独立 smoke 验证")
def test_g2pw_real_inference():
    """实战推理 (跳过, 见 docs/plan/2026-05-07-polyphone-fix-plan.md)"""
    pass
