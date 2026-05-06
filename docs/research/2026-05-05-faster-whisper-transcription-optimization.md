# Faster-Whisper 转录优化调研

> 调研日期: 2026-05-05
> 修订日期: 2026-05-05（评审 sonnet 3.5 初稿，纠正多处事实错误）
> **2026-05-06 状态**: 本调研建议已**作废**，由 sentence-unit pipeline 取代 (见下方 "事后验证")
> 目的: 评审项目 faster-whisper 使用方式，优化句子边界分割

## 事后验证 (2026-05-06)

引入 `group_segments_to_units` (sentence-unit 流水线) 后用同一视频 zjMuIxRvygQ 实测:

| 维度 | Whisper 原始 (transcribe_cache) | Grouping 后 (segments_cache) |
|------|-------------------------------|-----------------------------|
| 段数 | 73 | 44 |
| 破句对数 (前段无标点 + 后段首词带标点) | 5 (即 §2.1 列举的全部 case) | **0** |
| 平均时长 | ~5s | 7.89s |
| <2s 短段 | 多个 | 0 (min_unit_duration=2.0) |
| >12s 超长段 | 0 | 1 (单边界 case, max_unit_duration=12s 已生效) |

**结论**: sentence-unit pipeline 通过 (a) 句末标点 + 静音 gap 跨段合并 (b) `_split_segment_at_internal_sentence_breaks` 内部句号切分 (c) `_split_long_unit_by_clause` 子句标点切分 — 已涵盖本 doc P0/P1 全部建议的目标场景。

逐条:
- P1 `_fix_sentence_boundary`: 0 个目标 case → 实施无可观测效果, 不实施。
- P0 VAD 500→700ms: 会让 raw broken 从 5 降到 ~2-3, 但 grouping 全吃掉 → 下游无可观测效果, 不实施。
- P0 `nlp_segmentation` 默认 True: 与 sentence-unit grouping 功能重复 (split 长段 + merge 短段), 双轨可能冲突, 保持默认 False。
- P1 NLP split 阈值 8→6s: 同上, 被 grouping 替代。
- P2 `condition_on_previous_text=False`: 独立幻觉风险话题, 与本 doc 主旨无关, 现有 `deduplicate_segments` 兜底已足够。

下文保留作历史记录。

## 0. 修订说明

初稿（sonnet 3.5 生成）存在多处与代码、与 faster-whisper 默认值不符的事实错误。本次评审已逐项核对：

| 初稿主张 | 实测结论 |
|---------|---------|
| 项目"缺少 prepend/append_punctuations"，建议添加 | **错误**：faster-whisper 已为这两个参数提供默认值 `"'"¿([{-` 和 `"'.。,，!！?？:：")]}、`，未传即用默认值。初稿示例代码 `prepend_punctuations="\"'"¿([{-"` 还是 Python 语法错误。 |
| 项目"缺少 suppress_blank"，建议添加 `True` | **错误**：默认即 `True`，添加是空操作。 |
| 现行"流程: transcribe → deduplicate → _nlp_resegment → merge_short_segments" | **半错**：`_nlp_resegment` 由 `config["nlp_segmentation"]` 控制，**默认 False**（pipeline.py:5687）。`merge_short_segments` 在 translate 之后才调用（pipeline.py:5735），不在转录阶段。 |
| 实际案例行号 1-13、33-43 等 | **未核实**：cache 是 JSON 数组而非按行展开，初稿行号无意义。实测 `output/zjMuIxRvygQ/transcribe_cache.json` 共 73 段，符合"前段无句末标点 + 后段首词带句号"的破句仅 5 处（#10、#21、#55、#68、#71）。 |
| `_fix_sentence_boundary` 实现 | **有 bug**：①运算符优先级写法歧义；②合并粒度错误（应只把后段首词切到前段，初稿是把整段后段并入，会产生超长多句段）；③在转录阶段尚无 `text_zh` 字段。 |

## 1. 当前实现（已核对）

### 1.1 转录调用 (pipeline.py:793-797)

```python
segments_raw, info = model.transcribe(
    str(audio_path), language="en", beam_size=beam_size,
    word_timestamps=True, vad_filter=True,
    vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
)
```

未显式传入但**默认已生效**的参数：

| 参数 | 默认值 |
|------|------|
| `suppress_blank` | `True` |
| `prepend_punctuations` | `"'"¿([{-` |
| `append_punctuations` | `"'.。,，!！?？:：")]}、` |
| `condition_on_previous_text` | `True` |
| `compression_ratio_threshold` | `2.4` |
| `no_speech_threshold` | `0.6` |
| `hallucination_silence_threshold` | `None` |

### 1.2 转录阶段后处理 (pipeline.py:5681-5692)

```
transcribe_audio
  → deduplicate_segments
  → _nlp_resegment（仅当 config["nlp_segmentation"] = True，默认关闭）
  → 写 transcribe_cache.json
```

### 1.3 翻译阶段后处理 (pipeline.py:5733-5736)

```
translate_segments
  → deduplicate_segments
  → merge_short_segments
```

### 1.4 `_nlp_resegment` 行为 (pipeline.py:925-)

| 步骤 | 触发条件 | 作用 |
|------|---------|------|
| Pass 1 Split | `duration > 8.0s` 且段内 spaCy 检出 ≥2 句 | 按 word timestamp 切分 |
| Pass 2 Merge | 相邻两段 `duration < 1.5s` 且属同一句 | 合并 |

## 2. 真实问题清单

### 2.1 问题 1: VAD `min_silence_duration_ms=500` 偏低，导致句中切分

**实测**：`output/zjMuIxRvygQ/transcribe_cache.json` 73 段中，5 处出现"前段尾词无标点 + 后段首词带句末标点"的破句：

| 段号 | 前段尾 | 后段头 |
|------|------|------|
| #10 | `…with a little bit of surrounding` | `context. So to set the stage…` |
| #21 | `…model for how it's oriented in` | `space. That's right, your phone…` |
| #55 | `…the coordinates of our axis of` | `rotation. Well, actually, you take…` |
| #68 | `…multiply from the right by the` | `inverse. On the screen now…` |
| #71 | `…it's, it's just really` | `cool. Eater did something awesome…` |

**机制**：英文句末经常出现 ≥500ms 的吸气/标点停顿，VAD 在此处切段。`condition_on_previous_text=True` 默认值会让后段开头补一个句末标点（描述上一段的"结束"），但前段实际未拿到该标点，于是出现断裂。

**潜在副作用**：调高 `min_silence_duration_ms` 会让更多句子合并为同一段，把切分压力转给 NLP；若 NLP 关闭则会出现更多 8+s 长段。

### 2.2 问题 2: NLP 分句默认关闭

`config.get("nlp_segmentation", False)`（pipeline.py:5687）默认 False。即便阈值完美，未开启时整个 split/merge 通道都不生效。任何 NLP 阈值调整建议都应先确认/启用此开关。

### 2.3 问题 3: `_nlp_resegment` Split 阈值 8s 偏高

8s 在常规英语演讲中已能容下 2-3 句话，但很多 5-7s 段也含 2 句仅未触发。但**不能简单降到 4s**：

- spaCy `en_core_web_sm` 在缩写、数字、引号附近误切句子率显著上升；
- 4s 段常常本就只是一句，强行 split 会产生孤立词段（< 1s）。
- 需配合 Pass 2 的合并保护，且阈值建议先尝试 5-6s。

### 2.4 问题 4: 缺少专门的句子边界修复

即便启用 NLP，`_nlp_resegment` 不处理"前段+后段"跨段的句子重组（它只在单段内 split 或合并相邻短段）。问题 2.1 描述的 5 处破句，NLP 通道无法修复。需要一个跨段修复步骤，把后段开头的首个"以句末标点收尾的词"挪回前段。

### 2.5 问题 5: `condition_on_previous_text=True` 的幻觉风险（已知 trade-off）

faster-whisper 文档与社区经验均指出此选项可能放大长段幻觉/重复。项目用 `deduplicate_segments` 兜底，但若仍观察到重复，可考虑设为 `False`，代价是连贯性略降。本文不建议立即调整，先用现有去重观察。

## 3. 建议方案

### 3.1 P0 — 调高 VAD `min_silence_duration_ms`（低风险）

```python
vad_parameters=dict(min_silence_duration_ms=700, speech_pad_ms=200)
```

预期把 5 处破句中至少 3 处合并为单段（标点停顿通常 500-700ms，深句末停顿 >700ms）。仍残留的部分由 §3.3 的修复步骤处理。

### 3.2 P0 — 默认开启 NLP 分句

```jsonc
// config.json
"nlp_segmentation": true
```

或在 pipeline 中将默认值翻转为 True，让后续 split/merge 通道生效。

### 3.3 P1 — 新增跨段句子边界修复

在 `deduplicate_segments` 之后、`_nlp_resegment` 之前调用。仅在转录阶段使用（此时只有 `text` / `words`，没有 `text_zh`）。

```python
SENTENCE_END = set('.!?。！？')

def _fix_sentence_boundary(segments: list[dict]) -> list[dict]:
    """
    跨段修复：若前段末词无句末标点 且 后段首词以句末标点结尾，
    把后段首词割给前段。处理 condition_on_previous_text 引发的破句。
    """
    if len(segments) < 2:
        return segments

    fixed = [dict(segments[0])]
    for curr in segments[1:]:
        prev = fixed[-1]
        prev_text = (prev.get("text") or "").rstrip()
        curr_text = (curr.get("text") or "").lstrip()
        curr_words = curr.get("words") or []

        if not prev_text or not curr_text or not curr_words:
            fixed.append(dict(curr))
            continue

        prev_open = prev_text[-1] not in SENTENCE_END
        first_word_text = curr_words[0]["word"].strip()
        first_word_closes = first_word_text and first_word_text[-1] in SENTENCE_END

        if not (prev_open and first_word_closes):
            fixed.append(dict(curr))
            continue

        # 把首词移到前段
        moved = curr_words[0]
        prev["text"] = (prev_text + " " + first_word_text).strip()
        prev["end"] = moved["end"]
        prev_words = prev.get("words")
        if isinstance(prev_words, list):
            prev["words"] = prev_words + [moved]

        # 后段去掉首词；若后段为空（极少见）则丢弃
        rest_words = curr_words[1:]
        if not rest_words:
            continue
        new_curr = dict(curr)
        new_curr["words"] = rest_words
        new_curr["start"] = rest_words[0]["start"]
        # text 重新由剩余 words 拼接（避免错误偏移）
        new_curr["text"] = " ".join(w["word"].strip() for w in rest_words).strip()
        fixed.append(new_curr)

    return fixed
```

要点对比初稿：
- 只搬移**首个词**，而非整段后段并入，避免产生超长多句段；
- 仅处理 `text` / `words`，与转录阶段实际数据一致；
- 用 `set` 替代字符串成员检查；条件判断扁平化，无运算符优先级歧义；
- 后段被搬空时直接丢弃。

### 3.4 P1 — 降低 NLP Split 阈值到 5-6s（保守）

```python
# pipeline.py:956
if duration <= 6.0 or not words or not text:
```

**先用 6.0**，再观察实际产出。若仍有大量 5-6s 段含双句再下调到 5.0。**不建议**直接降到 4.0：spaCy 在短段误切率不可忽视。

### 3.5 处理流程（修订）

```
transcribe                        # vad_parameters: min_silence_duration_ms=700
  → deduplicate_segments
  → _fix_sentence_boundary        # 新增（仅 P1 后启用）
  → _nlp_resegment                # 默认开启，Split 阈值 6.0s
  → 写 transcribe_cache.json
```

`merge_short_segments` 维持在 translate 之后，不动。

## 4. 实施优先级

| 优先级 | 改动 | 预期影响 | 风险 |
|--------|------|----------|------|
| P0 | `min_silence_duration_ms` 500 → 700 | 减少破句约 60%（粗估） | 低 |
| P0 | `nlp_segmentation` 默认 True | 让 split/merge 生效 | 低（已有现成实现） |
| P1 | 新增 `_fix_sentence_boundary` | 修复残余跨段破句 | 低（仅当条件触发） |
| P1 | NLP Split 阈值 8.0 → 6.0 | 多分割长段 | 中（依赖 spaCy 句法） |
| P2 | 评估 `condition_on_previous_text=False` | 降幻觉/重复 | 中（连贯性可能下降） |

**不应实施**：

- 显式传 `prepend_punctuations` / `append_punctuations`（与默认相同，纯噪音）；
- 显式传 `suppress_blank=True`（与默认相同）；
- 直接把 NLP 阈值降到 4s（误切代价高）。

## 5. 验证方法

变更前后用同一视频跑转录，对比：

1. **破句数量**：脚本统计"前段尾词无 `.!?。！？` + 后段首词以这些字符结尾"的对数（基线 zjMuIxRvygQ = 5）。
2. **段数与平均时长**：调高 VAD 静音阈值后段数应下降；启用 NLP 后再回升一些。
3. **超长段**：>8s 段数；启用 NLP+阈值降到 6s 后应明显减少。
4. **重复**：`deduplicate_segments` 移除条数，验证调整后未爆炸性增加。
5. **抽样人工核对**：随机抽 10 段检查首尾完整性（开头无悬空词、结尾有标点）。

验证脚本片段：

```python
import json, sys
SENT_END = set('.!?。！？')
segs = json.load(open(sys.argv[1]))
broken = sum(
    1 for i in range(1, len(segs))
    if (segs[i-1]['text'].strip() and segs[i]['text'].strip()
        and segs[i-1]['text'].strip()[-1] not in SENT_END
        and segs[i]['text'].strip().split()[0][-1] in SENT_END)
)
durs = [s['end'] - s['start'] for s in segs]
print(f"segments={len(segs)} broken={broken} "
      f"avg_dur={sum(durs)/len(durs):.2f}s "
      f"long(>8s)={sum(1 for d in durs if d > 8)}")
```
