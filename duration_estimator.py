#!/usr/bin/env python3
"""中文 TTS 时长估算器 — jieba 分词驱动

从 pipeline.py 抽取，供 pipeline.py、phase2_translate.py、calibrate_tts_duration.py 共用。

当前模型: Ridge 回归 v2 (6 视频, 3009 样本, R²=0.92)
"""
import re
import unicodedata

import jieba

# ── 校准参数（Ridge v2） ──────────────────────────────────────────
# 单位: 毫秒
DURATION_1CHAR = 138      # 单字词（的/是/了）
DURATION_2CHAR = 361      # 双字词（今天/学习）
DURATION_3CHAR = 506      # 三字词（计算机/互联网）
DURATION_NCHAR = 223      # 四字及以上: 每字 ms
DURATION_LETTER = 31      # 英文字母: 每字符 ms
DURATION_DIGIT = 311      # 数字: 每字符 ms（TTS 朗读"三百一十一"等）
DURATION_URL_CHAR = 16    # URL/域名逐字母拼读: 每字符 ms
DURATION_PUNCT = 197      # 标点停顿 ms
INTERCEPT = 1210          # 回归截距 ms

_URL_PATTERN = re.compile(
    r'(?:https?://)?(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}'
    r'(?:/[^\s]*)?'
)


def estimate_duration(text_zh: str) -> float:
    """用 jieba 分词后按词粒度估算朗读时长（毫秒）。

    Ridge 回归校准值（6 视频 3009 样本, alpha=50, R²=0.92）：
      单字词（如"的""是"）: ~138ms
      双字词（如"今天""学习"）: ~361ms
      三字词（如"计算机""互联网"）: ~506ms
      四字及以上（如"人工智能"）: ~223ms/字
      英文单词: ~31ms/字符
      URL/域名（逐字母朗读）: ~16ms/字符
      数字: ~311ms/字符
      截距: +1210ms
    """
    # 预处理：找出 URL 并计算其独立时长，然后从文本中移除
    url_ms = 0.0
    clean_text = text_zh
    for m in _URL_PATTERN.finditer(text_zh):
        url_str = m.group()
        url_chars = sum(1 for c in url_str if c.isalnum() or c in './-_:')
        url_ms += url_chars * DURATION_URL_CHAR
        clean_text = clean_text.replace(url_str, '', 1)

    words = jieba.lcut(clean_text)
    total_ms = url_ms
    for word in words:
        # 跳过纯标点/空白
        meaningful = [c for c in word if not unicodedata.category(c).startswith(('P', 'Z', 'C'))]
        if not meaningful:
            total_ms += DURATION_PUNCT
            continue

        zh_count = sum(1 for c in meaningful if '\u4e00' <= c <= '\u9fff')
        other_count = len(meaningful) - zh_count

        if zh_count > 0:
            if zh_count == 1:
                total_ms += DURATION_1CHAR
            elif zh_count == 2:
                total_ms += DURATION_2CHAR
            elif zh_count == 3:
                total_ms += DURATION_3CHAR
            else:
                total_ms += zh_count * DURATION_NCHAR
        if other_count > 0:
            digits = sum(1 for c in meaningful if c.isdigit())
            letters = other_count - digits
            total_ms += letters * DURATION_LETTER + digits * DURATION_DIGIT

    return max(0, total_ms + INTERCEPT)
