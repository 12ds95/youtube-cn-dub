# 翻译质量评审：批量重译候选清单

**日期**: 2026-05-07
**关联**: `docs/research/2026-05-03-nllb-translation-model.md`、`pipeline.py:2200-2400` 翻译批次修复闭环
**触发**: 2026-05-07 跑 `batch_process.py --integrated` 时发现 1 个视频在 LLM 限流（429）后降级 NLLB，且 Pass 2 改写未完全救回。事后扫描全部 69 个 segments_cache.json 评估质量。

---

## 1. 评审方法

### 1.1 直接 NLLB 残留检测（强信号）

针对 LLM Pass 2 改写救不回的 NLLB 痕迹设计正则：

| 信号 | 正则/规则 | 来源 |
|------|----------|------|
| 字面直译 | `transformer→变压器`、`chat GPT→聊天GPT` | NLLB 词典直翻 |
| 西式标点 | 中文字符后紧跟 `,` `.`（排除数字） | NLLB 输出标点未本地化 |
| 全西式标点段 | 段内只有 `,.;?!` 没有 `，。；？！` | 同上 |
| 代词复制 | `我们我们`/`我们的我们`/`我们把我们的` | NLLB 直译 "we... our" 不消解主语 |
| 量词缺失 | `^这视频`、`这同一个` | NLLB 不补量词 |
| 字符≥5 重复 | `(.)\1{4,}` | 解析错误堆叠 |

### 1.2 间接压力信号（弱信号）

LLM 翻译期审计日志 `audit/translation_fix_log.json` 记录三类自动修复：
- `misalign_fix` — `_check_batch_alignment` 检出跨段错位（pipeline.py:2286）
- `dedup_fix` — 相邻段译文近重复（pipeline.py:2333）
- `contamination_fix` — `_detect_cross_contamination` 滑窗 3 段、阈值 0.6（pipeline.py:5298）

**修复率 ≥ 10% = 翻译期压力大**，即便强信号未命中，残余瑕疵概率也较高。

---

## 2. 确诊视频（NLLB 残留，必须重译）

| 视频 | video_id | 命中段 | 关键问题 |
|------|----------|--------|---------|
| Neural Networks/09 — But how do AI images and videos actually work? \| Guest video by Welch Labs | `iv-5mZ_9CPY` | #12, #13, #14, #18 | `transformer→变压器`、`chat GPT→聊天GPT`、全西式标点 |
| Differential Equations/01 — Differential equations, studying the unsolvable | `p_di4Zn4wz4` | #199, #200, #201 | #199 中文里混入英文 `earlier`；#200/#201 全西式标点 |

### 触发原因
- AI images 视频：429 限流导致批次 2/3/4/43 降级 NLLB（终端日志确认）。Pass 2 LLM 改写有 8 段回退 Pass 1，最终落到 segments_cache.json 的就是 NLLB 译文。
- DE/01 视频：未在本次重跑批次中，可能是历史早期版本残留。

---

## 3. 疑似视频（修复率高，建议重译）

> 这 5 个视频 NLLB 强信号没命中，但 LLM 翻译期触发了大量自动修复，说明原始批次质量不稳定，残余瑕疵概率较高。下面逐个展开 audit 数据 + 抽样问题段。

### 3.1 Differential Equations/08 — e^(iπ) in 3.14 minutes, using dynamics

- **video_id**: `v0YEaeIClKY`
- **总段数**: 28（短视频）
- **修复**: 5 段（17.9%）— 全部 `contamination_fix`
- **诊断**: 段数少但污染率最高。短视频更易被滑窗污染检测命中。

抽样修复：
| # | reason | 旧译 | 新译 |
|---|--------|------|------|
| 15 | contamination | "若你的位置始终为e的i t次方，当时间t向前推进时，你将如何运动？" | "若位置始终为e的i t次方，时间t向前推进时你将如何运动？" |
| 18 | contamination | "因此，即便你尚不知如何计算e的i t次方..." | "即便尚未掌握如何计算e的i t次方..." |
| 20 | contamination | "当时间t等于0时...这就是我们的初始条件..." | "当时间t等于0时...这是初始条件..." |

修复模式：旧译普遍累赘（"你的"、"我们的"、"该"），新译更口语化。整体仍可读，但有被 Pass 2 漏改的近似重复段风险。

### 3.2 Probability/03 — The quick proof of Bayes' theorem

- **video_id**: `U_85TaXbeIo`
- **总段数**: 26（短视频）
- **修复**: 4 段（15.4%）— 全部 `contamination_fix`
- **诊断**: 与 3.1 类似，短视频污染率天然高。

抽样修复：
| # | reason | 旧译 | 新译 |
|---|--------|------|------|
| 4 | contamination | "你可以先考虑事件 a 的概率，即所有可能情形中 a 成立的占比..." | "可先考虑 a 的概率，即所有可能情形中 a 成立的占比..." |
| 6 | contamination | "我们也可将其理解为：所有情形中 b 成立的占比..." | "或许我们还可将其理解为：所有可能情形中 b 成立的占比..." |
| 22 | contamination | "请记住，许多概率入门例题都设定在高度游戏化的情境中..." | "请注意，许多入门概率示例采用高度游戏化情境..." |

### 3.3 Calculus/04 — Visualizing the chain rule and product rule

- **video_id**: `YG15m2VwSjA`
- **总段数**: 123
- **修复**: 17 段（13.0%）— 15 contamination + 2 dedup
- **额外问题**: 残留英文短语 `d sine of x`、`cosine of x` 出现在中文译文中（#21, #23, #88），见前序评审输出
- **诊断**: 大量数学符号 + 公式表达，LLM 在批次翻译时容易"跨段污染"——把前后段的术语错位粘贴。

抽样修复：
| # | reason | 旧译 | 新译 |
|---|--------|------|------|
| 9 | contamination | "那么问题来了：若已知两个函数各自的导数，它们的和、积..." | "问题是：若已知两个函数的导数，它们的和、积..." |
| 34 | dedup | "此后，当正弦x从1下降时，该边长度便开始减小" | "随后，当正弦函数值从一下降时，它开始减小。" |
| 72 | dedup | "第二条数轴表示x平方的取值" | "第二个将保留x平方的值。" |

值得注意：#34 用了"正弦x"而新译用"正弦函数值"，#72 用"数轴"而新译用"第二个"——这反映 chain rule 视频中 LLM 对几何量的命名摇摆，建议加入 proper_noun 约束。

### 3.4 Neural Networks/07 — Attention in transformers, step-by-step | Deep Learning Chapter 6

- **video_id**: `eMlx5fFNoYc`
- **总段数**: 183
- **修复**: 20 段（10.9%）— **12 misalign + 8 contamination**
- **诊断**: 这是 5 个里**唯一以 misalign 为主**的视频，说明跨段错位严重。Attention 机制讲解涉及 Query/Key/Value 三类矩阵反复出现，LLM 容易把不同段的术语贴错位置。

抽样修复：
| # | reason | 旧译 | 新译 |
|---|--------|------|------|
| 54 | misalign | "从概念上讲，你应把键向量理解为可能回应查询向量的候选。" | "概念上，键向量可视为对查询向量的潜在回答" |
| 55 | misalign | "这个键矩阵同样包含大量可调参数..." | "该键矩阵同样包含可调参数..." |
| 56 | misalign | "你应将键向量理解为：当它与查询向量高度对齐时，即与之匹配。" | "你将键向量视为与查询向量在高度匹配时建立关联" |

### 3.5 Computer Science/01 — Solving Wordle using information theory

- **video_id**: `v68zYyaEmEA`
- **总段数**: 178（基于 sentence-unit 合并；底层 transcribe 841 段）
- **修复**: 19 段（10.7%）— **13 misalign + 4 contamination + 2 dedup**
- **诊断**: 又一个 misalign 主导的视频。Wordle 视频涉及大量"概率/比特/信息量"反复对比的句式，LLM 容易跨段错位。
- **重译进度**: 2026-05-07 22:57 重跑时 pipeline 在 jieba 初始化阶段段错误（exit -11），需单独再跑一次。

抽样修复：
| # | reason | 旧译 | 新译 |
|---|--------|------|------|
| 92 | misalign | "我想请你注意：概率越高的模式...其信息量越低..." | "我想让你注意的是：随着概率升高，越接近那些更可能出现的模式，信息量就越低..." |
| 93 | misalign | "以Wordle为例，结果约为4.9比特..." | "接着乘以该猜测能提供的信息位数，以weary为例，结果为4.9位..." |
| 95 | misalign | "实际上通常会更高；经计算..." | "但这只是下限，通常你能获得更好的结果..." |

---

## 4. 改进建议

按优先级：

### 4.1 数据可观测性（高优先级，已部分完成）

- ✅ **`batch_process.py` 加 pipeline 输出 tee**（2026-05-07 已合入）
  pipeline 子进程的 stdout/stderr 同步写到 `logs/batch_<ts>.pipeline.log`，未来可直接 `grep "NLLB 本地翻译"` 找到所有降级事件。
- ⏸ **NLLB 触发事件落 audit JSON**
  当前 NLLB 仅 `print()`，不进 audit。建议在 `pipeline.py:2257` 处把 `(batch_idx, segment_idx, en_text)` 写入 `audit/nllb_fallback.json`，让事后审计无需翻终端日志。

### 4.2 防止 NLLB 残留留到 final（中优先级）

- **Pass 2 强制要求重写**：当前 Pass 2 LLM 改写时如评分不显著就回退 Pass 1（NLLB 译文）。对**已知由 NLLB 产出**的段落，应跳过"是否采纳 Pass 2"的评分门槛，强制采纳 Pass 2，因为 Pass 1 (NLLB) 本就质量低。
- **西式标点后处理**：在 segments_cache 写入前加一个简单替换 pass：中文字符紧跟 `,` `.` 时替换为 `，` `。`。这是确定性修复，不依赖 LLM。代码点位：`pipeline.py` Pass 2 完成后、`_segments_cache_dump` 之前。

### 4.3 翻译期压力信号写 audit（中优先级）

- 把每个视频的 `n_misalign / n_contamination / n_dedup / n_pass1_fallback / n_nllb_used` 汇总到 `audit/translation_pressure.json`。这样下次跑评审脚本时可直接读结构化数据，不必扫 segments_cache 重做正则匹配。

### 4.4 限流时换备用 LLM 端点（低优先级）

- 当前限流退避 2 次后直接降级 NLLB。可考虑加备用 endpoint（如 OpenAI 或本地大模型），避免落到质量天花板低的 NLLB。

---

## 5. 行动项

- [ ] 重译 7 个视频（已通过 `--retranslate-only` 触发，2 个确诊 + 5 个疑似）
  ```bash
  python3 batch_process.py --retranslate-only iv-5mZ_9CPY,p_di4Zn4wz4,v0YEaeIClKY,U_85TaXbeIo,YG15m2VwSjA,eMlx5fFNoYc,v68zYyaEmEA
  ```
- [ ] Wordle (`v68zYyaEmEA`) 因 jieba 段错误失败，需单独重跑
- [ ] 实现 4.1 的"NLLB 触发事件落 audit JSON"（小改动）
- [ ] 实现 4.2 的西式标点确定性后处理（小改动）

---

## 6. 评审脚本

扫描脚本临时存放在 `/tmp/review_translations.py`（未持久化）。可移入 `scripts/` 目录复用：

- 输入：`output/**/segments_cache.json`
- 信号：见 §1.1（强信号）
- 输出：按命中段数倒序的视频列表

如纳入工程，建议加 CI hook：每次 batch 跑完跑一遍，命中即 fail，强制人工 review。
