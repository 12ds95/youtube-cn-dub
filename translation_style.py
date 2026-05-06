#!/usr/bin/env python3
"""翻译风格识别 — 共享工具模块

从 pipeline.py 抽取，供 phase2_translate.py / phase2_iterative.py /
phase2_iterative_split.py 复用。

主要功能:
  detect_translation_style()  — LLM 扫描全文，返回 (guide_str, term_rules_list)
  default_translation_rules() — 通用翻译保护规则
  parse_term_rules()          — 解析 "英文 → 中文" 规则为双语字典
"""

import json
import re
from pathlib import Path
from typing import Optional


def default_translation_rules() -> str:
    """通用翻译保护规则，无论主题识别是否成功都会注入"""
    return (
        "\n通用翻译规则（始终遵守）:"
        "\n  - 禁止使用 Markdown 格式（不用 **加粗**、`反引号`、# 标题等标记）"
        "\n  - 数学符号（i, e, π, θ 等）在数学/科学语境中保持专业含义，不译为日常用语"
        "\n  - 负号'-'在数学语境中翻译为'负'，如'-3'读作'负三'"
        "\n  - 译文用于语音朗读，必须通顺自然，适合听觉理解"
        "\n  - 宁可译文稍长，也不要为省字数而删减原文信息"
    )


def parse_term_rules(term_rules: list[str]) -> dict[str, str]:
    """解析 term_rules 列表为 {英文: 中文} 双语字典。

    输入格式: ["quaternion → 四元数", "gimbal lock → 万向节锁", ...]
    返回: {"quaternion": "四元数", "gimbal lock": "万向节锁", ...}
    """
    bilingual = {}
    for rule in term_rules:
        # 支持 → / -> / — 作为分隔符
        parts = re.split(r'\s*(?:→|->|—)\s*', rule, maxsplit=1)
        if len(parts) == 2:
            en, zh = parts[0].strip(), parts[1].strip()
            if en and zh:
                bilingual[en] = zh
    return bilingual


def _build_detect_prompt(sample_text: str, video_title: str) -> str:
    """构造主题识别 prompt, 含 proper_nouns 显式提取要求。"""
    return f"""你是翻译领域专家。请仔细阅读以下视频的完整英文内容，分析其核心主题、专业领域和翻译注意事项。

注意：
- 视频开头可能有广告、赞助商口播、寒暄等与主题无关的内容，请忽略这些，聚焦于视频的核心主题
- 请综合头、中、尾部内容做整体判断，不要只看开头

视频标题: {video_title or '(无标题)'}

英文原文:
{sample_text}

请用以下JSON格式返回（只返回JSON，不要其他内容）:
{{
  "topic": "视频核心主题（如: 线性代数/量子力学/React前端开发/宏观经济学/日常Vlog 等，尽量具体）",
  "style": "建议的翻译风格（如: 学术严谨/口语化教学/新闻播报/技术文档/轻松聊天）",
  "proper_nouns": [
    "严格限定四类: (a) 真实人名/姓氏 (b) 已注册的品牌/公司/产品名 (c) 知名作品名 (电影/书籍/游戏/学术论文) (d) 地名机构名",
    "判定标准: 该名词在维基百科或公众认知中作为独立实体存在 → 列入; 否则不列入",
    "对多词人名,如果原文中出现该人名的简写形式(单独 first name 或 last name),也单独列出该简写。如视频中既有 'Steve Jobs' 又只用 'Steve' 或 'Jobs',则三者都列",
    "格式: 原文 (有约定中文译法时加括号), 通用示例: 'Steve Jobs', 'Tesla', 'iPhone', 'Inception', 'MIT', 'Tokyo'"
  ],
  "term_rules": [
    "列出本视频中出现的专业术语翻译规则，每条格式: 英文 → 中文",
    "包含: 学科术语、技术概念、视频作者自创/借用的多词短语 (即使首字母大写, 只要不是真实品牌/人名,都按术语翻译)",
    "只列出真正在原文中出现过的术语，不要凭空臆造",
    "不要把人名/品牌/公司列在这里 (它们属于 proper_nouns)",
    "通用示例: 'eigenvalue → 特征值', 'machine learning → 机器学习', 'pull request → 拉取请求'"
  ],
  "warnings": [
    "列出本视频翻译中需要特别注意的陷阱",
    "如: 某个常见词在本视频的专业语境中有特殊含义",
    "如: 容易被误译的符号、缩写、双关语等"
  ]
}}"""


def _detect_once(sample_text: str, video_title: str, endpoint: str,
                 headers: dict, model: str, temperature: float) -> Optional[dict]:
    """单次主题识别 LLM 调用; 返回解析后的 dict 或 None。"""
    import httpx
    prompt = _build_detect_prompt(sample_text, video_title)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是翻译领域专家。请基于视频完整内容做全局分析，不要仅凭开头几段下结论。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": 1500,
    }
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()
        m = re.search(r'\{[\s\S]*\}', raw)
        if not m:
            return None
        return json.loads(m.group())
    except Exception:
        return None


def _score_detection(detected: dict, sample_text: str) -> float:
    """评分: term_rules + proper_nouns 中"英文 anchor"在原文中匹配的比例 + 总数量加权。
    返回 [0, +∞), 越大越好。
    """
    sample_lower = sample_text.lower()
    matched = 0
    total = 0
    for rule in detected.get("term_rules", []):
        if not isinstance(rule, str):
            continue
        parts = re.split(r'\s*(?:→|->|—)\s*', rule, maxsplit=1)
        if not parts:
            continue
        en = parts[0].strip().lower()
        if not en:
            continue
        total += 1
        if en in sample_lower:
            matched += 1
    for noun in detected.get("proper_nouns", []):
        if not isinstance(noun, str):
            continue
        # noun 形如 "Ben Eater" 或 "Apple (苹果)"; 取首段作 anchor
        en = re.split(r'\s*\(', noun, maxsplit=1)[0].strip().lower()
        if not en:
            continue
        total += 1
        if en in sample_lower:
            matched += 1
    if total == 0:
        return 0.0
    coverage = matched / total
    # 数量越多越好: 加 0.05*total 作为加权
    return coverage + 0.05 * total


def detect_translation_style(
    segments: list[dict],
    video_title: str,
    endpoint: str,
    headers: dict,
    model: str,
    temperature: float = 0.1,
    output_dir: Optional[Path] = None,
    text_key: str = "text",
    n_attempts: int = 6,
) -> tuple[str, list[str]]:
    """扫描完整视频内容，识别主题和翻译风格。

    n_attempts > 1 时多次调用 LLM 取最佳结果 (anchor 覆盖率 + 数量加权)。

    返回:
        (guide_string, term_rules_list)
    """
    if not segments:
        return default_translation_rules(), []

    # ── 构建完整文本，必要时均匀采样 ──
    all_texts = [s[text_key] for s in segments if s.get(text_key, "").strip()]
    full_text = "\n".join(all_texts)

    MAX_CHARS = 8000
    if len(full_text) > MAX_CHARS:
        n = len(all_texts)
        head_end = int(n * 0.3)
        mid_start = int(n * 0.3)
        mid_end = int(n * 0.7)
        tail_start = int(n * 0.7)

        head = all_texts[:head_end]
        mid = all_texts[mid_start:mid_end]
        tail = all_texts[tail_start:]

        sample_parts = []
        sample_parts.append(f"=== 视频前段 (第1~{head_end}段，共{n}段) ===")
        sample_parts.append("\n".join(head))
        sample_parts.append(f"\n=== 视频中段 (第{mid_start+1}~{mid_end}段) ===")
        sample_parts.append("\n".join(mid))
        sample_parts.append(f"\n=== 视频后段 (第{tail_start+1}~{n}段) ===")
        sample_parts.append("\n".join(tail))
        sample_text = "\n".join(sample_parts)

        if len(sample_text) > MAX_CHARS:
            budget_per_part = MAX_CHARS // 3
            head_text = "\n".join(head)[:budget_per_part]
            mid_text = "\n".join(mid)[:budget_per_part]
            tail_text = "\n".join(tail)[:budget_per_part]
            sample_text = (
                f"=== 视频前段 ===\n{head_text}\n"
                f"=== 视频中段 ===\n{mid_text}\n"
                f"=== 视频后段 ===\n{tail_text}"
            )
        content_desc = f"均匀采样 {n} 段（头/中/尾各约 30%/40%/30%）"
    else:
        sample_text = full_text
        content_desc = f"完整内容 {len(all_texts)} 段"

    print(f"     🔍 主题识别中（{content_desc}, n_attempts={n_attempts}）...")

    # ── 多次调用并选最佳 ──
    # 6 候选, 温度 [0.2, 0.2, 0.2, 0.4, 0.4, 0.4] — 同温度下重复采样保留 LLM 内在随机性
    temperatures = [0.2, 0.2, 0.2, 0.4, 0.4, 0.4]
    candidates = []
    for i in range(max(1, n_attempts)):
        t = temperatures[i] if i < len(temperatures) else temperature
        det = _detect_once(sample_text, video_title, endpoint, headers, model, t)
        if det is None:
            continue
        score = _score_detection(det, sample_text)
        candidates.append((score, t, det))

    if not candidates:
        print(f"     ⚠️  主题识别返回格式异常，使用通用规则")
        return default_translation_rules(), []

    candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, best_t, detected = candidates[0]
    print(f"     ✅ 主题识别 best score={best_score:.2f} (T={best_t}); "
          f"候选: {[round(c[0], 2) for c in candidates]}")

    guide_parts = []
    topic = detected.get("topic", "")
    style = detected.get("style", "")
    if topic:
        guide_parts.append(f"\n本视频主题: {topic}")
    if style:
        guide_parts.append(f"翻译风格: {style}")

    proper_nouns = [n for n in detected.get("proper_nouns", []) if isinstance(n, str)]
    if proper_nouns:
        guide_parts.append("专有名词 (人名/作品名/品牌名/产品名 — 必须直接保留原文不译, 不要意译):")
        for noun in proper_nouns[:25]:
            guide_parts.append(f"  - {noun}")

    term_rules = [r for r in detected.get("term_rules", []) if isinstance(r, str)]
    if term_rules:
        guide_parts.append("专业术语翻译规则（必须遵守）:")
        for rule in term_rules[:15]:
            guide_parts.append(f"  - {rule}")

    warnings = [w for w in detected.get("warnings", []) if isinstance(w, str)]
    if warnings:
        guide_parts.append("翻译注意事项:")
        for w in warnings[:8]:
            guide_parts.append(f"  - {w}")

    guide_parts.append(default_translation_rules())

    result = "\n".join(guide_parts)
    print(f"        · 主题: {topic} | 风格: {style}")
    print(f"        · 专有名词: {len(proper_nouns)} 个 | 术语规则: {len(term_rules)} 条")
    for noun in proper_nouns[:5]:
        print(f"          - {noun}")
    if len(proper_nouns) > 5:
        print(f"          ... 共 {len(proper_nouns)} 个")

    if output_dir:
        audit_dir = output_dir / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        style_path = audit_dir / "style_detection.json"
        style_data = {
            "topic": topic, "style": style,
            "proper_nouns": proper_nouns,
            "term_rules": term_rules, "warnings": warnings,
            "score": best_score,
            "n_attempts": len(candidates),
        }
        with open(style_path, "w", encoding="utf-8") as _f:
            json.dump(style_data, _f, ensure_ascii=False, indent=2)

    return result, term_rules


def load_cached_style(output_dir: Path) -> tuple[str, list[str]]:
    """从缓存的 style_detection.json 加载风格指导，避免重复调用 LLM。

    返回 (guide_string, term_rules_list)。文件不存在返回 ("", [])。
    """
    style_path = output_dir / "audit" / "style_detection.json"
    if not style_path.exists():
        return "", []

    try:
        with open(style_path, encoding="utf-8") as f:
            data = json.load(f)

        guide_parts = []
        topic = data.get("topic", "")
        style = data.get("style", "")
        if topic:
            guide_parts.append(f"\n本视频主题: {topic}")
        if style:
            guide_parts.append(f"翻译风格: {style}")

        proper_nouns = data.get("proper_nouns", [])
        if proper_nouns:
            guide_parts.append("专有名词 (人名/作品名/品牌名/产品名 — 必须直接保留原文不译, 不要意译):")
            for noun in proper_nouns[:25]:
                guide_parts.append(f"  - {noun}")

        term_rules = data.get("term_rules", [])
        if term_rules:
            guide_parts.append("专业术语翻译规则（必须遵守）:")
            for rule in term_rules[:15]:
                guide_parts.append(f"  - {rule}")

        warnings = data.get("warnings", [])
        if warnings:
            guide_parts.append("翻译注意事项:")
            for w in warnings[:8]:
                guide_parts.append(f"  - {w}")

        guide_parts.append(default_translation_rules())

        print(f"     📋 已加载缓存的风格指导: {topic} | "
              f"专有名词 {len(proper_nouns)} 个 | 术语 {len(term_rules)} 条")
        return "\n".join(guide_parts), term_rules

    except Exception as e:
        print(f"     ⚠️  加载风格缓存失败 ({e})")
        return "", []


def parse_proper_noun(noun_str: str) -> tuple[str, Optional[str]]:
    """解析 proper_noun 形式 "Ben Eater" 或 "Apple (苹果)" 为 (en, zh_or_None)。"""
    m = re.match(r"^\s*([^(（]+?)\s*[(（]\s*([^)）]+?)\s*[)）]\s*$", noun_str)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return noun_str.strip(), None


def _chinese_runs_pinyin(text_zh: str) -> list[str]:
    """提取 text 中每段连续中文(含·)的拼音串(无声调)。"""
    try:
        from pypinyin import pinyin, Style
    except Exception:
        return []
    runs = re.findall(r'[一-鿿·]+', text_zh)
    out = []
    for r in runs:
        py = pinyin(r, style=Style.NORMAL)
        s = ''.join(p[0] for p in py if p[0] and p[0] != '·')
        if s:
            out.append(s)
    return out


def _lev_ratio(a: str, b: str) -> float:
    """Levenshtein 相似度 = 1 - dist / max_len。"""
    if not a or not b:
        return 0.0
    if len(a) < len(b):
        a, b = b, a
    n, m = len(a), len(b)
    cur = list(range(m + 1))
    for i, ca in enumerate(a, 1):
        prv, cur = cur, [i] + [0] * m
        for j, cb in enumerate(b, 1):
            cur[j] = min(prv[j] + 1, cur[j - 1] + 1, prv[j - 1] + (ca != cb))
    return 1 - cur[m] / max(n, 1)


def _is_plausible_transliteration(en: str, text_zh: str,
                                  threshold: float = 0.45) -> bool:
    """判断 text_zh 中是否含 en 的合理音译。

    在每个中文连续段拼音串内做滑窗,长度 ≈ len(en_simple) ± 4,
    找最大 Levenshtein 比;>= threshold 视为音译成功。
    """
    en_simple = re.sub(r'[^a-z]', '', en.lower())
    if len(en_simple) < 3:
        return False
    L = len(en_simple)
    runs = _chinese_runs_pinyin(text_zh)
    for r in runs:
        if len(r) < 3:
            continue
        # 滑窗扫描: 窗口大小 [L-3, L+3], 步长 1
        best = 0.0
        for win in range(max(3, L - 3), L + 4):
            if win > len(r):
                continue
            for start in range(0, len(r) - win + 1):
                seg = r[start:start + win]
                ratio = _lev_ratio(en_simple, seg)
                if ratio > best:
                    best = ratio
                    if best >= threshold:
                        return True
    return False


def verify_proper_nouns(segments: list[dict], proper_nouns: list[str],
                        text_zh_key: str = "text_zh",
                        text_en_key: str = "text_en") -> list[dict]:
    """rule-base 校验: proper_noun 在英文段中出现时, 中文段必须保留原文或一致译法。

    保留判定 (任一即可):
      - 原文 (大小写不敏感) 在 text_zh 中
      - zh_alt 在 text_zh 中
      - text_zh 含合理拼音音译 (Levenshtein >= 0.45)

    返回 issues 列表 [{idx, noun, text_en, text_zh, kind}, ...]。
    kind:
      - "noun_lost": 中文译文既无原文也无指定译法/音译 (高危, 可能误译)
    """
    issues = []
    if not proper_nouns or not segments:
        return issues
    for noun_str in proper_nouns:
        if not isinstance(noun_str, str) or not noun_str.strip():
            continue
        en, zh_alt = parse_proper_noun(noun_str)
        if not en or len(en) < 2:
            continue
        en_lower = en.lower()
        for i, seg in enumerate(segments):
            text_en = (seg.get(text_en_key) or "").lower()
            if en_lower not in text_en:
                continue
            text_zh = seg.get(text_zh_key) or ""
            preserved_en = en_lower in text_zh.lower()
            preserved_zh = bool(zh_alt) and zh_alt in text_zh
            preserved_translit = (
                not preserved_en and not preserved_zh
                and _is_plausible_transliteration(en, text_zh)
            )
            if not (preserved_en or preserved_zh or preserved_translit):
                issues.append({
                    "idx": i,
                    "noun": noun_str,
                    "text_en": seg.get(text_en_key, "")[:80],
                    "text_zh": text_zh,
                    "kind": "noun_lost",
                })
    return issues


def detect_chinglish_issues(segments: list[dict],
                            proper_nouns: list[str] = None,
                            text_zh_key: str = "text_zh",
                            min_word_len: int = 4) -> list[dict]:
    """检测中英混杂: text_zh 中残留 ≥min_word_len 字符英文 (排除白名单)。

    白名单 (不算 chinglish):
      - proper_nouns 列表的任一英文单词 (含多词人名的拆分)
      - URL/域名/路径 (含 . / : 的 token)
      - 数学符号 / 单字母变量

    返回 issues = [{idx, text_zh, leftover: [...], kind: 'chinglish'}, ...]
    """
    issues: list[dict] = []
    if not segments:
        return issues
    # 构建白名单 (lowercase): proper_nouns 拆词后所有单词
    allow: set[str] = set()
    for noun_str in proper_nouns or []:
        if not isinstance(noun_str, str):
            continue
        en, _zh = parse_proper_noun(noun_str)
        if not en:
            continue
        # 拆词: 'Ben Eater' → {'ben', 'eater', 'ben eater'}
        allow.add(en.lower())
        for w in en.split():
            if len(w) >= 2:
                allow.add(w.lower())

    for i, seg in enumerate(segments):
        text_zh = (seg.get(text_zh_key) or "").strip()
        if not text_zh:
            continue
        # 提取连续英文 token (字母/连字符)
        candidates = re.findall(r"[A-Za-z][A-Za-z'\-]{%d,}" % (min_word_len - 1), text_zh)
        leftover: list[str] = []
        for tok in candidates:
            tl = tok.lower()
            # 白名单
            if tl in allow:
                continue
            # URL/域名: token 紧邻的字符是否含 . / :
            tok_pos = text_zh.find(tok)
            if tok_pos >= 0:
                surrounding = text_zh[max(0, tok_pos - 1):tok_pos + len(tok) + 1]
                if any(c in surrounding for c in ('.', '/', ':')):
                    continue
            leftover.append(tok)
        if leftover:
            issues.append({
                "idx": i,
                "text_zh": text_zh,
                "leftover": leftover,
                "kind": "chinglish",
            })
    return issues


def load_proper_nouns(output_dir: Path) -> list[str]:
    """从 audit/style_detection.json 读 proper_nouns。"""
    style_path = output_dir / "audit" / "style_detection.json"
    if not style_path.exists():
        return []
    try:
        with open(style_path, encoding="utf-8") as f:
            data = json.load(f)
        return [n for n in data.get("proper_nouns", []) if isinstance(n, str)]
    except Exception:
        return []
