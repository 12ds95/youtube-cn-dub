# 2026-03-29 LLM 翻译重试回退 + 流程重构

## 问题 1: LLM 翻译失败静默保留英文原文

### 现象

32884a7ba3d 运行日志中出现：
```
⚠️  翻译异常（""），保留原文: "And this mostly works, but one"
⚠️  翻译异常（""），保留原文: "gimbal lock, where when two of"
```

LLM 返回空翻译时直接保留英文原文，没有任何重试或回退机制。

### 修复

1. `_translate_llm_single` 增加 `max_retries` 参数（默认 3 次），每次重试间隔递增
2. `_translate_llm` 批量翻译后收集 `failed_indices`（翻译为空或过短的段）
3. 对失败的段回退调用 `GoogleTranslator` 逐条翻译
4. Google 也失败时才保留英文原文（最后手段）

### 回退链

```
LLM 批量翻译 → LLM 逐条翻译（含重试）→ Google Translate → 保留原文
```

## 问题 2: 先生成 TTS 再迭代优化，造成浪费

### 现象

原流程：
```
Step 5: 生成 TTS（72 个文件）
Step 6: 迭代优化（第 1 轮就改了 36 个 → 重新生成 36 个 TTS → 浪费 50%）
```

### 分析

迭代优化依赖 `_measure_speed_ratios` 测量 TTS 实际时长来判断超速，所以必须先有 TTS 文件。但这导致首轮大量 TTS 白生成。

### 修复

1. 新增 `_estimate_speed_ratios`：基于字符数估算语速比（中文 ~250ms/字, 英文 ~100ms/字），不需要 TTS 文件
2. `run_refinement_loop` 改用 `_estimate_speed_ratios`，纯文本阶段完成所有迭代
3. 主流程重排：

```
旧: 翻译 → TTS → 迭代优化(改翻译+重生成TTS) → 字幕+对齐 → 合成
新: 翻译 → 迭代优化(纯文本,字符估算) → TTS(一次性) → 字幕+对齐 → 合成
```

4. TTS 生成前清除旧缓存，确保所有文件与定稿翻译一致

### 字符估算公式

```
estimated_ms = (中文字数 * 250 + 英文/数字字数 * 100) * 1.1(标点停顿)
ratio = estimated_ms / target_ms
```

基于 edge-tts `zh-CN-YunxiNeural` 实测大致匹配。虽不如 TTS 实测精确，但足够驱动迭代优化的精简判断。
