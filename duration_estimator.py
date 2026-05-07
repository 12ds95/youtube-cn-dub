#!/usr/bin/env python3
"""中文 TTS 时长估算器 — jieba 分词 + 韵律 + 音节特征

模型版本:
  v2 (legacy): Ridge 8 维 (词级 + 标点 + 截距) — R² 0.92, 已弃用
  v4 (当前):   Ridge 23 维 (+音节级 + 韵律级) — R² 0.952, MAE 569ms (8327 净样本)
  v6 (可选):   LightGBM 23 维 — R² 0.973, MAE 394ms (需 lightgbm + libomp)

接口保持不变: estimate_duration(text_zh) -> float (毫秒)
"""
import os
import re
import unicodedata
from typing import Dict, List

import jieba

# ── v4 校准参数 (8327 净样本, R²=0.952, 5-fold CV) ──────────────
# 来源: scripts/calibrate_v3.py (2026-05-07)
# 数据预处理: 过滤 applied_rate ∈ {0.80, 1.35} clamp 边界 + MAD outlier
V4_PARAMS: Dict[str, float] = {
    # 词级 (基础特征)
    "n_1char": 14.09, "n_2char": 32.51, "n_3char": 54.16, "n_4plus": 17.07,
    "n_letters": 97.18, "n_digits": 238.22, "n_url_chars": 48.44, "n_punct": 133.95,
    # 音节级 (pypinyin 声调/韵母/声母)
    "n_tone1": 64.07, "n_tone2": 85.18, "n_tone3": 70.64, "n_tone4": 63.68,
    "n_tone_neutral": -24.95,
    "n_final_simple": 124.05, "n_final_complex": 134.59,
    "n_no_initial": -12.97,
    # 韵律级 (标点/句末/语气词)
    "n_comma": 13.66, "n_period": 172.84, "n_question": 0.0,
    "n_exclaim": -231.09, "n_ellipsis": -95.57,
    "n_sentence_final": 182.80, "n_filler_word": 287.08,
    "intercept": -183.0,
}

# v2 legacy 参数 (向后兼容, 用于 pypinyin 不可用时降级)
V2_LEGACY_PARAMS = {
    "DURATION_1CHAR": 138, "DURATION_2CHAR": 361, "DURATION_3CHAR": 506,
    "DURATION_NCHAR": 223, "DURATION_LETTER": 31, "DURATION_DIGIT": 311,
    "DURATION_URL_CHAR": 16, "DURATION_PUNCT": 197, "INTERCEPT": 1210,
}

_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}'
    r'(?:/[^\s]*)?'
)

_FILLER_WORDS = {'吧', '呢', '啊', '嘛', '哦', '哎', '嗯', '哈', '呀'}
_SENTENCE_END = re.compile(r'[。.!?！？]')

# ── pypinyin 缓存 (避免重复加载) ─────────────────────────────────
_PYPINYIN_AVAILABLE = None


def _check_pypinyin():
    global _PYPINYIN_AVAILABLE
    if _PYPINYIN_AVAILABLE is None:
        try:
            from pypinyin import pinyin  # noqa: F401
            _PYPINYIN_AVAILABLE = True
        except Exception:
            _PYPINYIN_AVAILABLE = False
    return _PYPINYIN_AVAILABLE


# ── 特征提取 ────────────────────────────────────────────────────

def _extract_word_features(text_zh: str) -> Dict[str, int]:
    """词级特征 (jieba 分词后): 1/2/3/4字词 + 字母/数字/URL/标点."""
    feat = {
        "n_1char": 0, "n_2char": 0, "n_3char": 0, "n_4plus": 0,
        "n_letters": 0, "n_digits": 0, "n_url_chars": 0, "n_punct": 0,
    }
    clean_text = text_zh
    for m in _URL_PATTERN.finditer(text_zh):
        url_str = m.group()
        feat["n_url_chars"] += sum(1 for c in url_str if c.isalnum() or c in './-_:')
        clean_text = clean_text.replace(url_str, '', 1)

    words = jieba.lcut(clean_text)
    for word in words:
        meaningful = [c for c in word
                      if not unicodedata.category(c).startswith(('P', 'Z', 'C'))]
        if not meaningful:
            feat["n_punct"] += 1
            continue
        zh_count = sum(1 for c in meaningful if '一' <= c <= '鿿')
        other_count = len(meaningful) - zh_count
        if zh_count > 0:
            if zh_count == 1: feat["n_1char"] += 1
            elif zh_count == 2: feat["n_2char"] += 1
            elif zh_count == 3: feat["n_3char"] += 1
            else: feat["n_4plus"] += zh_count
        if other_count > 0:
            for c in meaningful:
                if c.isdigit():
                    feat["n_digits"] += 1
                elif not ('一' <= c <= '鿿'):
                    feat["n_letters"] += 1
    return feat


def _extract_syllable_features(text_zh: str) -> Dict[str, int]:
    """音节级特征 (pypinyin): 声调/韵母/声母. pypinyin 不可用时返回零向量."""
    feat = {
        "n_tone1": 0, "n_tone2": 0, "n_tone3": 0, "n_tone4": 0,
        "n_tone_neutral": 0,
        "n_final_simple": 0, "n_final_complex": 0,
        "n_no_initial": 0,
    }
    if not text_zh or not _check_pypinyin():
        return feat
    chinese_chars = [c for c in text_zh if '一' <= c <= '鿿']
    if not chinese_chars:
        return feat
    try:
        from pypinyin import pinyin, Style
        chinese_text = ''.join(chinese_chars)
        tones = pinyin(chinese_text, style=Style.TONE3, heteronym=False)
        finals = pinyin(chinese_text, style=Style.FINALS_TONE3, heteronym=False)
        initials = pinyin(chinese_text, style=Style.INITIALS, heteronym=False)
        for tp, fp, ip in zip(tones, finals, initials):
            py = tp[0] if tp else ""
            if py and py[-1].isdigit():
                t = int(py[-1])
                if t == 1: feat["n_tone1"] += 1
                elif t == 2: feat["n_tone2"] += 1
                elif t == 3: feat["n_tone3"] += 1
                elif t == 4: feat["n_tone4"] += 1
            else:
                feat["n_tone_neutral"] += 1
            final = fp[0] if fp else ""
            final_letters = re.sub(r'\d', '', final)
            if len(final_letters) >= 3:
                feat["n_final_complex"] += 1
            else:
                feat["n_final_simple"] += 1
            ini = ip[0] if ip else ""
            if not ini:
                feat["n_no_initial"] += 1
    except Exception:
        pass
    return feat


def _extract_prosody_features(text_zh: str) -> Dict[str, int]:
    """韵律级特征: 标点类型 + 句末位置 + 语气词."""
    feat = {
        "n_comma": 0, "n_period": 0, "n_question": 0, "n_exclaim": 0,
        "n_ellipsis": 0, "n_sentence_final": 0, "n_filler_word": 0,
    }
    if not text_zh:
        return feat
    feat["n_comma"] = (text_zh.count(',') + text_zh.count(',')
                       + text_zh.count(';') + text_zh.count(';'))
    feat["n_period"] = text_zh.count('.') + text_zh.count('。')
    feat["n_question"] = text_zh.count('?') + text_zh.count('?')
    feat["n_exclaim"] = text_zh.count('!') + text_zh.count('!')
    feat["n_ellipsis"] = text_zh.count('…') + text_zh.count('...')

    cnt = 0
    for m in _SENTENCE_END.finditer(text_zh):
        end_pos = m.start()
        for c in text_zh[max(0, end_pos - 2):end_pos]:
            if '一' <= c <= '鿿':
                cnt += 1
    feat["n_sentence_final"] = cnt
    feat["n_filler_word"] = sum(1 for c in text_zh if c in _FILLER_WORDS)
    return feat


# ── v4 主估算器 ─────────────────────────────────────────────────

def _estimate_v4(text_zh: str) -> float:
    """v4: 词级 + 音节级 + 韵律级 (23 维 Ridge)."""
    feat = _extract_word_features(text_zh)
    feat.update(_extract_syllable_features(text_zh))
    feat.update(_extract_prosody_features(text_zh))
    total = V4_PARAMS["intercept"]
    for name, val in feat.items():
        if name in V4_PARAMS:
            total += val * V4_PARAMS[name]
    return max(0.0, total)


def _estimate_v2_legacy(text_zh: str) -> float:
    """v2 legacy: 仅词级 (兼容 pypinyin 不可用时降级)."""
    p = V2_LEGACY_PARAMS
    url_ms = 0.0
    clean_text = text_zh
    for m in _URL_PATTERN.finditer(text_zh):
        url_str = m.group()
        url_chars = sum(1 for c in url_str if c.isalnum() or c in './-_:')
        url_ms += url_chars * p["DURATION_URL_CHAR"]
        clean_text = clean_text.replace(url_str, '', 1)
    words = jieba.lcut(clean_text)
    total_ms = url_ms
    for word in words:
        meaningful = [c for c in word
                      if not unicodedata.category(c).startswith(('P', 'Z', 'C'))]
        if not meaningful:
            total_ms += p["DURATION_PUNCT"]
            continue
        zh_count = sum(1 for c in meaningful if '一' <= c <= '鿿')
        other_count = len(meaningful) - zh_count
        if zh_count > 0:
            if zh_count == 1: total_ms += p["DURATION_1CHAR"]
            elif zh_count == 2: total_ms += p["DURATION_2CHAR"]
            elif zh_count == 3: total_ms += p["DURATION_3CHAR"]
            else: total_ms += zh_count * p["DURATION_NCHAR"]
        if other_count > 0:
            digits = sum(1 for c in meaningful if c.isdigit())
            letters = other_count - digits
            total_ms += letters * p["DURATION_LETTER"] + digits * p["DURATION_DIGIT"]
    return max(0, total_ms + p["INTERCEPT"])


# ── 主接口 ──────────────────────────────────────────────────────
# 通过 DURATION_ESTIMATOR_VERSION 环境变量切换版本: v4 (默认) / v2

_VERSION = os.environ.get("DURATION_ESTIMATOR_VERSION", "v4")


def estimate_duration(text_zh: str) -> float:
    """中文 TTS 时长估算 (毫秒)。

    默认 v4 (R²=0.952, MAE=569ms, 23 维 Ridge)。
    通过环境变量 DURATION_ESTIMATOR_VERSION=v2 切换到 legacy 8 维模型。
    """
    if not text_zh:
        return 0.0
    if _VERSION == "v2":
        return _estimate_v2_legacy(text_zh)
    return _estimate_v4(text_zh)
