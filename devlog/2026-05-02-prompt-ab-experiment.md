# 2026-05-02 翻译提示词 A/B 实验 — 领域术语保留 + 正确性优先规则评估

## 背景

commit 0f666d8 系统性地向 pipeline 中 7 个提示词位置注入了两条新规则：
1. "领域专业术语保留英文原词不翻译（如 API、SDK、HTTP、GPU、LLM、RAG、JSON 等）"
2. "翻译正确性优先于字数控制——宁可译文稍长，也不要为凑字数而曲解原意"

用户怀疑这些变更导致翻译风格变化。以 `output/zjMuIxRvygQ`（四元数与三维旋转，72段）为案例进行控制变量实验。

## 实验设计

对 segments 21-23, 50 进行控制变量 A/B 测试，同一 API (qwen3-coder-next)，同一 temperature (0.3)，每个变体跑 2 次检查一致性：

| 变体 | 提示词 |
|------|--------|
| A (post-0f666d8) | "领域专业术语保留英文原词不翻译" + "翻译正确性优先于字数控制" |
| B (pre-0f666d8) | "保持技术术语的专业性" |
| C (改进版) | "计算机缩写保留英文原词，不加括号注音" + "忠实原文语义，不要曲解也不要过度扩充" |

## 实验结果

### Seg 22: "that relies on Quaternions. The thing is, there are other ways..."

| 变体 | 译文 | 字数 |
|------|------|------|
| A | 这依赖于四元数（Quaternions）。但需要说明的是，计算旋转还有其他方法。 | 40 |
| B | 这依赖于四元数。但事实上，计算旋转还有其他方法。 | 24 |
| C | 这依赖于四元数。但事实上，计算旋转还有其他方法。 | 24 |

### Seg 23: "many of which are way simpler to think about than Quaternions."

| 变体 | 译文 | 字数 |
|------|------|------|
| A | 其中许多概念在思考时远比四元数（Quaternions）更为简单。 | 33 |
| B | 其中许多概念比四元数更容易理解。 | 16 |
| C | 其中许多概念比四元数更容易理解。 | 16 |

### Seg 50: "a unit vector, which we'll write as having i, j, and k components..."

| 变体 | 译文 | 字数 |
|------|------|------|
| A | 一个单位向量...进行归一化处理，使得各分量的平方和等于 1。 | 56 |
| B | 一个单位向量...并使其满足各分量平方和为 1。 | 43 |
| C | 一个单位向量...并归一化使其平方和为 1。 | 39 |

## 关键发现

### 1. Variant A 的两个问题

**括号注音**：LLM 对"领域专业术语保留英文原词"的理解是在中文翻译后用括号标注英文——写成 `四元数（Quaternions）`。这对配音有害：TTS 会把括号内容朗读出来，增加不必要的时长。

**译文过长**：Variant A 比 B 长 20-107%。"正确性优先于字数控制——宁可译文稍长" 鼓励 LLM 写更长的译文，加重了 CPS 压力。

### 2. 翻译质量问题的根因不是 prompt

cached 翻译中的语义错误（如 seg 21 "事实是，算旋转还有别的路子" ← 原文说的是手机里的软件；seg 23 "四元数不可替代" ← 原文说比四元数简单）来自 **refine 迭代多轮精简导致的语义偏离**（跨段内容混淆），不是 prompt 变更造成的。

A/B 两个变体在单独翻译时都能正确翻译这些段。

### 3. 领域术语规则对非 CS 视频无效

此视频是数学/图形学内容，域内术语是 "Quaternion"、"Euler angles"、"Gimbal Lock" 等——不属于 API/SDK/HTTP/GPU 这类计算机缩写。规则对此视频基本无效。

## 修复方案

优化所有 7 个提示词位置：

1. **缩小术语保留范围**："领域专业术语" → "计算机领域缩写"（更精确）
2. **禁止括号注音**：明确禁止 `四元数（Quaternions）` 这种写法
3. **平衡长度**：去掉"宁可稍长"的偏向，改为"忠实原文语义，不要曲解也不要过度扩充"
4. **配音导向**：增加"短句为主"

Variant C 实测结果与 B（旧版）长度一致，同时保留了术语保留规则的合理部分。

## 受影响的提示词位置

| # | 位置 | 已修复 |
|---|------|--------|
| 1 | DEFAULT_CONFIG system_prompt | ✅ |
| 2 | _translate_llm batch user prompt | ✅ |
| 3 | _translate_llm_two_pass Pass 1 | ✅ |
| 4 | _translate_llm_two_pass Pass 2 adapt_system | ✅ |
| 5 | _translate_llm_single user_content | ✅ |
| 6 | _refine_with_llm system prompt | ✅ |
| 7 | _isometric_translate_batch system prompt | ✅ |
| 8 | expand system prompt | ✅ |
