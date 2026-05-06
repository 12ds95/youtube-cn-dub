"""测试: _retranslate_chinglish 采纳前的统一守卫 + 复跑 chinglish 检测。

LLM 重译后必须满足:
  1. _validate_text_adjustment(refine, fidelity 0.30, floor 0.50, ceiling 1.6)
  2. detect_chinglish_issues 复跑无残留

任一不通过则保留原 text_zh, 不采纳 LLM 输出。
"""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mock_post(new_zh: str):
    """构造一个 httpx.Client.post 返回, choices[0].message.content = new_zh."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "choices": [{"message": {"content": new_zh}}]
    })
    return resp


def _run_retranslate(segments, chinglish_issues, mocked_new_zh, proper_nouns=None):
    import pipeline
    cfg = {
        "api_url": "http://x/v1",
        "api_key": "k",
        "model": "m",
    }
    with patch("httpx.Client") as mock_cls:
        client_inst = MagicMock()
        client_inst.__enter__ = MagicMock(return_value=client_inst)
        client_inst.__exit__ = MagicMock(return_value=False)
        client_inst.post = MagicMock(return_value=_mock_post(mocked_new_zh))
        mock_cls.return_value = client_inst
        return pipeline._retranslate_chinglish(
            segments, chinglish_issues, cfg, proper_nouns or []
        )


def test_accepts_clean_translation():
    """LLM 修复了英文残留, 长度合理, fidelity OK → 采纳"""
    segs = [{"text_en": "host the site", "text_zh": "把网站 hosting 在云端"}]
    issues = [{"idx": 0, "text_zh": segs[0]["text_zh"], "leftover": ["hosting"]}]
    out = _run_retranslate(segs, issues, "把网站托管在云端")
    assert out[0]["text_zh"] == "把网站托管在云端"


def test_rejects_residual_english():
    """LLM 没修干净, 仍残留 ≥4 字英文 → 拒绝, 保留原 text_zh"""
    original = "把网站 hosting 在云端"
    segs = [{"text_en": "host the site", "text_zh": original}]
    issues = [{"idx": 0, "text_zh": original, "leftover": ["hosting"]}]
    out = _run_retranslate(segs, issues, "把网站 deploy 在云端")
    assert out[0]["text_zh"] == original  # 未变更


def test_rejects_drift_low_fidelity():
    """LLM 改写到完全不相关的内容 → fidelity 守卫拒绝"""
    original = "把网站 hosting 在云端"
    segs = [{"text_en": "host the site", "text_zh": original}]
    issues = [{"idx": 0, "text_zh": original, "leftover": ["hosting"]}]
    # 完全偏离原意 (无字符重叠)
    out = _run_retranslate(segs, issues, "今天天气特别好")
    assert out[0]["text_zh"] == original


def test_rejects_over_expanded():
    """LLM 输出长度 > 原 1.6x → expansion_ceiling 守卫拒绝"""
    original = "网站 hosting"
    segs = [{"text_en": "hosted", "text_zh": original}]
    issues = [{"idx": 0, "text_zh": original, "leftover": ["hosting"]}]
    # 8 字 > 5 * 1.6 = 8 → 触发 over_expanded (严格 >)
    out = _run_retranslate(
        segs, issues, "网站正在云端服务器集群上托管运行"
    )
    assert out[0]["text_zh"] == original


def test_rejects_too_short():
    """LLM 输出 < 2 字符 → 拒绝"""
    original = "把网站 hosting 在云端"
    segs = [{"text_en": "host", "text_zh": original}]
    issues = [{"idx": 0, "text_zh": original, "leftover": ["hosting"]}]
    out = _run_retranslate(segs, issues, "啊")
    assert out[0]["text_zh"] == original


def test_proper_noun_residual_allowed():
    """LLM 输出仍含 proper_noun 中的英文 → 不算残留, 应采纳"""
    original = "Ben Eater 在云端 hosting 网站"
    segs = [{"text_en": "Ben Eater hosts the site", "text_zh": original}]
    issues = [{"idx": 0, "text_zh": original, "leftover": ["hosting"]}]
    new_zh = "Ben Eater 在云端托管网站"
    out = _run_retranslate(
        segs, issues, new_zh, proper_nouns=["Ben Eater"]
    )
    assert out[0]["text_zh"] == new_zh
