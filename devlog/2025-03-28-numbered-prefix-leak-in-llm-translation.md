# LLM 翻译编号前缀泄漏 Bug 排查与修复

日期：2025-03-28
影响范围：`pipeline.py` — `_parse_numbered_translations()` 及其调用方
测试视频：`zjMuIxRvygQ`（3Blue1Brown 四元数讲解），输出目录 `output/32884a7ba3d/`
模型：阿里云百炼 qwen3-coder-next


## 用户反馈的现象

用户在审看最终生成的中文配音视频后报告：

> "最后的视频出现语句重复，连英文字幕都同一句话有重复……原文都被改了……翻译脚本有问题"

具体表现为中文字幕中出现 `[1] 它不会像其他方法……`、`[2] 但就计算机图形学……` 这种带编号前缀的文本。由于 SRT 字幕直接从 `segments_cache.json` 的 `text_zh` 字段生成，前缀泄漏导致观感上像是"语句重复"——多个字幕条目以 `[1]`、`[2]` 开头，看起来像在重复同一组编号列表。


## 排查过程

### 第一步：确认 text_en 是否被修改

用户怀疑"原文被改了"。首先验证 text_en（英文原文）的完整性——它是整个流程的校验基线，从 faster-whisper 转录出来后全程只读。

检查 `iter_0_segments.json` 中所有 text_en 字段：

```python
en_leaked = [i for i, seg in enumerate(segments) if re.match(r'^\[\d+\]', seg['text_en'])]
# 结果：0 条 → text_en 完好未被篡改
```

结论：text_en 没有问题。用户感知到的"原文重复"实际上是中文字幕行的编号前缀造成的视觉干扰（双语字幕中 text_zh 在 text_en 上方）。

### 第二步：定位 text_zh 中的污染数据

对 `iter_0_segments.json` 做全量扫描：

```python
leaked = [i for i, seg in enumerate(segments) if re.match(r'^\[\d+\]', seg.get('text_zh', ''))]
# 结果：14 条（#15 ~ #28），text_zh 分别以 [1] 到 [14] 开头
```

示例：

```
#15: "[1] 它不会像其他方法那样容易出现bug或边界情况。我的意思是，这些方法在数学上确实很有趣，"
#16: "[2] 但就计算机图形学、机器人学，"
...
#28: "[14] 导致系统丧失一个自由度。"
```

这 14 段恰好对应 LLM 批量翻译时的某一个 batch（batch_size=15，这是最后一个 batch 可能不足 15 条）。编号 `[1]` 到 `[14]` 连续出现，说明这一整批的解析全部失败了。

### 第三步：审查解析函数（首次尝试 — 方向正确但未触及根因）

原始 `_parse_numbered_translations()` 代码：

```python
def _parse_numbered_translations(content, expected_count):
    lines = content.strip().split("\n")
    translations = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^\[?\d+\]?\s*\.?\s*(.+)$", line)
        if match:
            translations.append(match.group(1).strip())
        elif translations:
            translations[-1] += line

    # 如果解析数量不对，按行分割
    if len(translations) != expected_count:
        translations = [l.strip() for l in content.strip().split("\n") if l.strip()]

    while len(translations) < expected_count:
        translations.append("")
    return translations[:expected_count]
```

初看正则 `r"^\[?\d+\]?\s*\.?\s*(.+)$"` 似乎能匹配 `[1] 翻译内容`，理论上应该正确提取 group(1)。于是最初怀疑是正则本身写错了，花了一些时间反复手动推演正则匹配过程，结论是——**正则本身对 `[N] 翻译内容` 格式是能正确匹配的**。

这条路走进了死胡同。

### 第四步：关注 fallback 路径（关键转折）

重新审视代码，注意到当 `len(translations) != expected_count` 时有一个 fallback 分支：

```python
if len(translations) != expected_count:
    translations = [l.strip() for l in content.strip().split("\n") if l.strip()]
```

这个 fallback 做了**纯粹的按行分割，完全不去除 `[N]` 前缀**。如果这条路被触发，所有 `[N]` 前缀都会原封不动地进入最终结果。

那么问题变成了：为什么正则解析得到的数量与 expected_count 不匹配？

### 第五步：发现 qwen3 的 `<think>` 推理块（根因定位）

qwen3-coder-next 是 Qwen3 系列模型，会在回复中输出 `<think>...</think>` 推理块。LLM 实际返回的 content 结构类似：

```
<think>
Let me translate these sentences.
1. First about quaternion stability
2. Second about computer graphics
...
</think>

[1] 它不会像其他方法那样容易出现bug
[2] 但就计算机图形学、机器人学
...
```

写了一段模拟代码验证这个假设：

```python
content_with_think = """<think>
Let me translate these sentences.
1. First about quaternion stability
2. Second about computer graphics
</think>

[1] 它不会像其他方法那样容易出现bug
[2] 但就计算机图形学
[3] 以及虚拟现实等"""

# 用旧解析逻辑跑一遍
```

结果确认了完整的故障链：

1. `<think>` 块内的 `1. First about...`、`2. Second about...` 等推理行**能被旧正则 `r"^\[?\d+\]?\s*\.?\s*(.+)$"` 命中**——因为 `\[?` 和 `\]?` 都是可选的，`\d+` 匹配数字，`\.?` 匹配句点
2. 这些推理行被错误计入 translations 列表，导致 `len(translations) = 推理行数 + 实际翻译数 ≠ expected_count`
3. 触发 fallback：`translations = [l.strip() for l in content.strip().split("\n") if l.strip()]`
4. fallback 返回所有非空行（包括 `<think>` 标签、推理内容、`</think>` 标签和带 `[N]` 前缀的翻译）
5. 最后 `return translations[:expected_count]` 截断到期望长度——但截到的内容是从头开始的混合行
6. 最终结果中翻译文本携带 `[N]` 前缀泄漏到 text_zh

验证输出：

```
SKIPPED (no prev): "<think>"
SKIPPED (no prev): "Let me translate these 14 sentences one by one."
MATCHED: "1. The first one is about quaternion stability" -> "The first..."  ← 推理行被误匹配
MATCHED: "2. The second is about computer graphics" -> "The second..."      ← 推理行被误匹配
APPENDED to prev: "</think>"
MATCHED: "[1] 它不会像..." -> "它不会像..."
...
Parsed 5 translations (expected 3) → 触发 fallback → [N] 前缀泄漏
```

**根因确认：qwen3 的 `<think>` 推理块中的编号行干扰了翻译解析计数，触发 fallback 路径，而 fallback 不做任何前缀清理。**


## 修复方案

### 修改 1：新增 `_strip_think_block()` 工具函数

在解析前先剥离整个 `<think>...</think>` 块，从源头消除干扰：

```python
def _strip_think_block(content: str) -> str:
    """去除 Qwen3 等模型返回的 <think>...</think> 推理块"""
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
```

### 修改 2：新增 `_strip_numbered_prefix()` 工具函数

统一的编号前缀剥离逻辑，供多处复用：

```python
def _strip_numbered_prefix(line: str) -> str:
    """去除行首的 [N] 或 N. 编号前缀"""
    cleaned = re.sub(r"^\[?\d+\]?\s*\.?\s*", "", line.strip())
    return cleaned.strip()
```

### 修改 3：重写 `_parse_numbered_translations()`，三层防护

```python
def _parse_numbered_translations(content, expected_count):
    # 第一层：去除 <think> 推理块
    content = _strip_think_block(content)

    lines = content.strip().split("\n")
    translations = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 用更精确的正则，分开匹配 [N] 和 N. 两种格式
        match = re.match(r"^\[(\d+)\]\s*(.+)$", line)
        if not match:
            match = re.match(r"^(\d+)\.\s*(.+)$", line)
        if match:
            translations.append(match.group(2).strip())
        elif translations:
            translations[-1] += line

    # 第二层：fallback 也去除编号前缀
    if len(translations) != expected_count:
        raw_lines = [l.strip() for l in content.strip().split("\n") if l.strip()]
        translations = [_strip_numbered_prefix(l) for l in raw_lines]

    # 第三层：最终安全检查
    translations = [_strip_numbered_prefix(t) if re.match(r"^\[\d+\]", t) else t
                    for t in translations]

    while len(translations) < expected_count:
        translations.append("")
    return translations[:expected_count]
```

关键改进点：

- 正则从宽松的 `^\[?\d+\]?\s*\.?\s*(.+)$` 改为分开的两个精确模式，`[N]` 和 `N.` 不再混为一谈
- fallback 路径不再裸分割，而是对每行调用 `_strip_numbered_prefix()`
- 末尾增加逐条安全检查，确保零泄漏

### 修改 4：调用方增加安全网

在 `_translate_llm()` 中赋值 text_zh 之前加最终检查：

```python
if re.match(r"^\[\d+\]", text_zh):
    text_zh = _strip_numbered_prefix(text_zh)
```

同样的检查也添加到了 `_refine_with_llm()` 中精简结果的赋值处。

### 修改 5：单条翻译降级路径

`_translate_llm_single()` 中也加了 `_strip_think_block()` 处理，防止逐条翻译时 `<think>` 块泄漏。


## 已有数据清洗

修复代码只影响后续运行。已生成的 `iter_0` ~ `iter_5` 和 `segments_cache.json` 中的历史脏数据需要单独清洗。

用 `_strip_numbered_prefix()` 对所有 `text_zh` 以 `[数字]` 开头的记录做了批量修正：

```
iter_0_segments.json: 修复 14 段
iter_1_segments.json: 修复 6 段
iter_2_segments.json: 修复 5 段
iter_3_segments.json: 修复 5 段
iter_4_segments.json: 修复 5 段
iter_5_segments.json: 修复 5 段
segments_cache.json:  修复 5 段
─────────────────────────────
总计: 45 条记录
```

从 iter_0 的 14 段到最终 cache 的 5 段，可以看到迭代精简循环在此过程中替换了部分翻译（但替换后的新翻译因为也经过了有 bug 的解析器，仍有 5 段残留）。


## 验证结果

### 单元测试（模拟 5 种场景）

| 场景 | 输入 | 结果 |
|------|------|------|
| qwen3 带 `<think>` 块 | 推理行 + 14 条翻译 | 全部干净提取 |
| 干净输出（无 `<think>`） | 标准 `[N] 翻译` 格式 | 正常解析 |
| 句点格式 `N. 翻译` | `1. 翻译\n2. 翻译` | 正常解析 |
| 数量不匹配触发 fallback | 期望 2 条但返回 3 条 | fallback 也正确去除前缀 |
| 完整复现原始 bug（14 段） | 带 `<think>` + `[1]`~`[14]` | 14 条全部干净 |

### 存量数据验证

清洗后对最终 `segments_cache.json` 的检查：

```
text_zh [N] 前缀泄漏: 0（应为 0）✅
text_en [N] 前缀泄漏: 0（应为 0）✅
```

之前泄漏的 #15 ~ #28 段全部恢复正常。


## 经验总结

1. **模型特性感知**：使用 OpenAI 兼容 API 接入不同模型时，必须考虑各模型的输出特性差异。qwen3 系列的 `<think>` 推理块是其独特行为，DeepSeek 等模型不一定有。解析层必须对此做防御性处理。

2. **fallback 路径同等重要**：主解析路径的正则本身没有错，bug 藏在 fallback 路径中。fallback 本意是"解析失败时的兜底"，但恰恰是它在不做任何清理的情况下把脏数据放行了。测试时容易忽略 fallback 路径。

3. **多层防护优于单点修复**：最终方案采用三层防护（源头剥离 → fallback 清理 → 末尾检查）+ 调用方安全网，即使某一层出问题，后续层仍能兜住。

4. **正则宽松匹配的副作用**：旧正则 `^\[?\d+\]?\s*\.?\s*(.+)$` 试图用一个模式同时匹配 `[N]` 和 `N.` 两种格式，但 `\[?` 和 `\]?` 的可选性意味着纯数字行（如推理块中的 `1. ...`）也会被匹配。拆分为两个独立的精确模式更安全。
