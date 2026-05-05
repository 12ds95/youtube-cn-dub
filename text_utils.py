#!/usr/bin/env python3
"""共享文本清理工具 — LLM 输出后处理

从 pipeline.py 抽取，供 pipeline.py 和 phase2_translate.py 共用。
"""
import re
from functools import lru_cache


def _strip_think_block(content: str) -> str:
    """去除 Qwen3 等模型返回的 <think>...</think> 推理块"""
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def _strip_markdown(text: str, original: str = "") -> str:
    """去除翻译文本中 LLM 额外添加的 Markdown 格式标记。

    只清除原文中不存在的 markdown 符号，保留原文本身就有的字符。
    例如原文 "3 * 4 = 12" 中的 * 是乘号，翻译后应保留。

    参数:
        text:     翻译后的中文文本
        original: 对应的英文原文（用于判断哪些符号是原文自带的）
    """
    if not text:
        return text
    # ── 兜底：清除 LLM 回显的字数提示和翻译指令泄漏 ──
    # 英文原文不可能包含中文字数提示，无需像 markdown 那样检查 original
    # 1. 括号包裹的字数提示（各种变体）:
    #    (≈26字) （约26个字） [≈26字] (目标约26字左右) (约20-30字) 等
    text = re.sub(
        r'[(\uff08\[]\s*(?:目标)?(?:约|≈)\s*\d+[\s\-~～]*(?:\d+)?\s*(?:个)?(?:中文)?字\s*(?:左右|以内)?\s*[)\uff09\]]',
        '', text)
    # 2. 行尾裸露的字数提示（无括号）: ...译文≈26字 / 约26字
    text = re.sub(r'\s*(?:约|≈)\s*\d+\s*(?:个)?字\s*$', '', text)
    # 3. 完整翻译指令句泄漏: （请将译文控制在约N字，不要在译文中输出字数标注）
    text = re.sub(r'[(\uff08]\s*请将译文控制在[^)\uff09]*[)\uff09]', '', text)
    # 4. 批量提示元数据泄漏: 各句参考字数：[1]≈26字, [2]≈8字 ...
    text = re.sub(r'各句参考字数[：:][^\n]*', '', text)
    # 5. 零散指令片段泄漏
    text = re.sub(r'[,，]?\s*不要在译文中输出字数标注[。.，,]?', '', text)
    # 反引号包裹的行内代码 `xxx` → xxx
    if '`' not in original:
        text = re.sub(r'`([^`]+)`', r'\1', text)
    # 加粗 **xxx** 或 __xxx__
    if '**' not in original:
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    if '__' not in original:
        text = re.sub(r'__(.+?)__', r'\1', text)
    # 斜体 *xxx*（但不匹配单独的 * 或乘号前后有空格的情况）
    if '*' not in original:
        text = re.sub(r'(?<!\*)\*([^\s*][^*]*[^\s*])\*(?!\*)', r'\1', text)
    # 斜体 _xxx_（仅匹配前后有空格或行首行尾的，避免破坏 snake_case 变量名）
    if '_' not in original:
        text = re.sub(r'(?<=\s)_([^_]+)_(?=\s|$)', r'\1', text)
        text = re.sub(r'^_([^_]+)_(?=\s|$)', r'\1', text)
    # 删除线 ~~xxx~~
    if '~' not in original:
        text = re.sub(r'~~(.+?)~~', r'\1', text)
    # 行首 # 标题标记
    if '#' not in original:
        text = re.sub(r'^#{1,6}\s+', '', text)
    return text.strip()


def _strip_numbered_prefix(line: str) -> str:
    """去除行首的 [N] 或 N. 编号前缀"""
    cleaned = re.sub(r"^\[?\d+\]?\s*\.?\s*", "", line.strip())
    return cleaned.strip()


def _clean_refine_artifacts(text: str) -> str:
    """清理翻译文本中残留的 refine 格式标签。

    处理 LLM 输出中可能泄漏的标签格式：
      **[轻]** xxx → xxx
      - [中] xxx   → xxx
      [V3] xxx     → xxx
    以及 LLM 回显的系统指令文本。
    """
    if not text:
        return text
    # 去除行首的 markdown/列表标记 + [轻]/[中]/[短]/[轻扩]/[中扩]/[重扩]/[V1]-[V10] 标签
    text = re.sub(r"^[-*]*\s*\*{0,2}\[(轻|中|短|轻扩|中扩|重扩)\]\*{0,2}\s*", "", text.strip())
    text = re.sub(r"^[-*]*\s*\*{0,2}\[V\d+\]\*{0,2}\s*", "", text.strip(), flags=re.IGNORECASE)
    # 如果整行都是系统指令回显（如"以下为每段翻译的三个精简版本..."），返回空
    if re.search(r"(轻扩?|中扩?|短|重扩).*[/／].*(轻扩?|中扩?|短|重扩)", text):
        return ""
    if re.search(r"\[V\d+\].*[/／].*\[V\d+\]", text, re.IGNORECASE):
        return ""
    return text.strip()


def normalize_llm_output(text: str, original: str = "", strip_refine: bool = False) -> str:
    """LLM 输出标准清理链：think块 → markdown → 换行 → refine标签"""
    if not text:
        return text
    text = _strip_think_block(text)
    text = _strip_markdown(text, original)
    text = re.sub(r'\n+', '', text)
    if strip_refine:
        text = _clean_refine_artifacts(text)
    return text.strip()


# ─── 括号注解语义去重 ────────────────────────────────────────────────

# 全角+半角括号匹配
_PAREN_PATTERN = re.compile(r'[（(]([^）)]*)[）)]')

# 数学/代码排除：含运算符或数学符号
_MATH_CHARS = set('+-*/=°×÷∑∫∂√≈≠≤≥<>^')

# 延迟加载的 embedding 模型
_annotation_model = None
_annotation_model_loaded = False


def _load_annotation_model():
    """延迟加载 sentence-transformers 模型用于括号注解语义判断"""
    global _annotation_model, _annotation_model_loaded
    if _annotation_model_loaded:
        return _annotation_model
    _annotation_model_loaded = True
    try:
        from sentence_transformers import SentenceTransformer
        _annotation_model = SentenceTransformer(
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        )
    except Exception:
        _annotation_model = None
    return _annotation_model


@lru_cache(maxsize=512)
def _semantic_similarity(text_a: str, text_b: str) -> float:
    """计算两段文本的 cosine similarity（缓存结果）"""
    import numpy as np
    model = _load_annotation_model()
    if model is None:
        return -1.0  # 模型不可用，返回特殊值
    embs = model.encode([text_a, text_b], normalize_embeddings=True)
    return float(np.dot(embs[0], embs[1]))


def _is_math_content(inner: str) -> bool:
    """判断括号内容是否为数学/代码表达式"""
    if not inner:
        return False
    # 含数学运算符
    if any(c in _MATH_CHARS for c in inner):
        return True
    # 纯数字+逗号+空格 (坐标/元组): (4, 1), (0, 0, 1)
    if re.fullmatch(r'[\d,.\s\-]+', inner):
        return True
    # 单字母变量列表 (数学符号): (i,j,k), (x, y, z), (a,b)
    if re.fullmatch(r'[a-zA-Z](?:\s*[,，]\s*[a-zA-Z])+', inner.strip()):
        return True
    return False


def _is_pure_cjk_explanation(inner: str) -> bool:
    """判断括号内容是否为纯中文解释（非注音）"""
    if not inner:
        return False
    cjk = sum(1 for c in inner if '\u4e00' <= c <= '\u9fff')
    # >90% 是 CJK 字符 → 中文解释，保留
    return len(inner) > 0 and cjk / len(inner) > 0.9


def strip_parenthetical_annotations(text_zh: str) -> str:
    """去除 LLM 自发添加的冗余括号注音。

    使用 sentence-transformers 模型判断括号内容是否与前文语义重复：
      - 数学/坐标表达式 → 保留
      - 纯中文解释 → 保留
      - 内容 <2 字符 → 保留
      - 其余用 embedding cosine similarity 判断：>0.5 为冗余，去除

    支持全角（）和半角() 括号。模型延迟加载，加载失败时回退启发式规则。
    """
    if not text_zh:
        return text_zh
    if '（' not in text_zh and '(' not in text_zh:
        return text_zh
    # 排除不含拉丁字母的文本（纯中文括号内容无需检查）
    if not re.search(r'[（(][^）)]*[a-zA-Z][^）)]*[）)]', text_zh):
        return text_zh

    def _should_strip(match):
        inner = match.group(1)
        if not inner or len(inner) < 2:
            return match.group(0)

        # 快速排除：无拉丁字母
        if not re.search(r'[a-zA-Z]', inner):
            return match.group(0)

        # 快速排除：数学/代码内容
        if _is_math_content(inner):
            return match.group(0)

        # 快速排除：纯中文解释
        if _is_pure_cjk_explanation(inner):
            return match.group(0)

        # 提取括号前的 context（前面最近的有意义文本，最多 15 字符）
        start = match.start()
        context_before = text_zh[max(0, start - 15):start].strip()
        # 去掉 context 中的标点
        context_before = re.sub(r'[，。！？、；：""''…—\s]+', '', context_before)

        if len(context_before) < 2:
            # context 太短，回退启发式
            cjk = sum(1 for c in inner if '\u4e00' <= c <= '\u9fff')
            if len(inner) > 0 and (len(inner) - cjk) / len(inner) > 0.5:
                return ''
            return match.group(0)

        # 使用 embedding 判断语义相似度
        sim = _semantic_similarity(context_before, inner)
        if sim < 0:
            # 模型不可用，回退启发式
            cjk = sum(1 for c in inner if '\u4e00' <= c <= '\u9fff')
            if len(inner) > 0 and (len(inner) - cjk) / len(inner) > 0.5:
                return ''
            return match.group(0)

        if sim > 0.5:
            return ''  # 冗余，去除
        return match.group(0)  # 不冗余，保留

    return _PAREN_PATTERN.sub(_should_strip, text_zh)


# ─── 统一消费接口 ────────────────────────────────────────────────────

def text_for_duration(text_zh: str) -> str:
    """为时长/budget 估算准备文本：去除冗余括号注解。

    所有 _estimate_duration_jieba / count_hanzi / budget 计算前应调用此函数，
    确保括号注音不膨胀时长估算。
    """
    return strip_parenthetical_annotations(text_zh)


def text_for_tts(text_zh: str) -> str:
    """为 TTS 合成准备文本：去除冗余括号注解。

    TTS 引擎不应朗读冗余的英文注音。
    注：多音字修复 (_fix_polyphones) 由 pipeline.py 在此之后单独处理。
    """
    return strip_parenthetical_annotations(text_zh)
