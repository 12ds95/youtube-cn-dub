# 两阶段 TTS 方案评估（结论：不可行）

**日期**: 2026-05-03
**状态**: 已否决

## 提议

将 TTS 生成拆为两阶段：
1. **Draft 阶段**: 用快速本地引擎（如 pyttsx3/piper）做闭环反馈，速度优先
2. **Final 阶段**: 用 config 指定的质量引擎（如 edge-tts），由于 Draft 已修正译文，re-generate 数量大幅减少

## 实验与分析过程

### 实验 1: 引擎真实合成验证

对 5 个引擎做了真实合成测试，输入文本 `"这是一段中文语音合成的冒烟测试。"`，验证产出有效：

| 引擎 | 类型 | 产出大小 | 结果 |
|------|------|---------|------|
| edge-tts | 远程 | 15,984 bytes | 通过 |
| gtts | 远程 | 23,040 bytes | 通过 |
| pyttsx3 | 本地 | 5,451 bytes | 通过（升级到 2.99 后） |
| piper | 本地 | 72,748 bytes | 通过 |
| sherpa-onnx | 本地 | 15,691 bytes | 通过 |

**观察**: 同一句话 5 个引擎产出大小从 5KB 到 73KB 不等，说明编码方式、采样率、时长差异巨大。piper 产出是 edge-tts 的 4.5 倍，pyttsx3 仅为 1/3。

### 实验 2: 源码审计 — 各引擎 rate 参数支持

逐行审查每个引擎的 `synthesize()` 实现，确认 rate 控制能力：

**edge-tts** (`pipeline.py:2055-2068`): 完整支持。`rate` 参数转为百分比字符串传给 `Communicate(rate="+15%")`。

**pyttsx3** (`pipeline.py:2182-2185`): **参数被遮蔽**。函数签名 `rate: float = 1.0` 在第 2185 行被 `rate = self.rate` 覆盖，闭环计算的 corrected_rate 被丢弃：
```python
async def synthesize(self, text: str, path: str, voice: str, rate: float = 1.0):
    ...
    rate = self.rate  # ← 覆盖了函数参数！永远是 180 WPM
```

**piper** (`pipeline.py:2101-2118`): **无 rate 参数**。通过 subprocess 调用 `piper` 二进制，命令行参数只有 `--model` 和 `--output_file`，没有速度控制。

**sherpa-onnx** (`pipeline.py:2131-2148`): **硬编码 1.0**。第 2148 行 `speed=1.0` 写死：
```python
audio = tts.generate(text, sid=int(cfg.get("speaker_id", 0)), speed=1.0)
```

**gtts** (`pipeline.py:2080-2087`): **无 rate 参数**。`gTTS` 库不支持语速控制。

**结论**: 5 个引擎中只有 edge-tts 支持 rate 控制。Draft 阶段用本地引擎测出的 corrected_rate 传给 pyttsx3/piper/sherpa-onnx 都会被忽略。

### 实验 3: 审计日志分析 — 实际反馈闭环数据

使用项目 `zjMuIxRvygQ` 的真实审计数据（`output/zjMuIxRvygQ/audit/`）分析现有闭环效果：

**speed_report.json — 总体统计**:
- 总段数: 72
- 原始 ratio 均值: 1.0108（接近 1.0，jieba 估算器整体偏差小）
- 原始 ratio 标准差: 0.1025
- 90% 的段 ratio 在 ±15% 以内
- outliers > 1.4: 0（Phase 0 预检已处理极端值）

**tts_feedback_log.json — Phase 2 rate 反馈**（25 段触发）:

| 偏差区间 | 段数 | 典型案例 |
|---------|------|---------|
| 15%-20% | 10 | idx=2 偏差 15.3%, idx=4 偏差 17.4% |
| 20%-35% | 8 | idx=3 偏差 22.0%, idx=71 偏差 34.8% |
| 35%-60% | 5 | idx=40 偏差 35.5%, idx=57 偏差 56.9% |
| >60% | 2 | idx=53 偏差 **87.7%**, idx=68 偏差 53.0% |

高偏差段的 corrected_rate 被 clamp 到 `[0.80, 1.35]`（5 段触及上限 1.35，3 段触及下限 0.80），说明纯 rate 调节无法完全补偿。

**llm_duration_feedback_log.json — Phase 3 LLM 反馈**（3 段触发）:
- idx=24: 精简（19 字→15 字），偏差 21.1%
- idx=28: 扩展（10 字→13 字），偏差 24.5%
- idx=53: 精简（10 字→7 字），偏差 32.4%（Phase 2 后仍偏 32%）

**实际调用量分析**:
```
Phase 0 预检:  jieba 纯计算        → 0 次 TTS 调用
Phase 1 初始:  72 段批量生成        → 72 次
Phase 2 rate:  25 段 rate 修正重生成 → 25 次
Phase 3 LLM:   3 段文本调整重生成   → 3 次
─────────────────────────────────
总计                               → 100 次 (edge-tts)
```

### 实验 4: 两阶段方案模拟计算

假设 Draft 用 pyttsx3，Final 用 edge-tts：

```
Draft 阶段:  72 段本地生成            → 72 次 (pyttsx3)
             测量时长 → 计算 rate     → pyttsx3 时长与 edge-tts 不相关
Final 阶段:  72 段正式生成            → 72 次 (edge-tts)
             Phase 2 rate 反馈仍需    → ~25 次 (edge-tts)
             Phase 3 LLM 反馈仍需     → ~3 次 (edge-tts)
─────────────────────────────────
总计                                  → 172 次 (+72%)
```

Draft 阶段的时长测量对 Final 引擎无用（韵律模型不同），Final 阶段**仍需完整反馈循环**。总工作量反增 72%。

### 实验 5: jieba 估算器精度分析

`_estimate_duration_jieba`（`pipeline.py:3043-3102`）是基于 jieba 分词的纯计算估算器：

**校准基准**: edge-tts `zh-CN-YunxiNeural` 实测值
- 单字词: ~200ms, 双字词: ~380ms, 三字词: ~530ms
- 英文: ~150ms/字符, URL: ~280ms/字符（逐字母朗读）

**从审计数据看精度**:
- 72 段中 47 段（65%）在 Phase 0+1 后偏差 <15%，无需反馈
- 25 段（35%）需 Phase 2 rate 修正
- 仅 3 段（4%）需 Phase 3 LLM 修正

**关键发现**: 韵律修正乘数 `* 1.3`（`pipeline.py:2318`）是全局常量。高偏差段（如 idx=53 偏差 87.7%）通常包含 URL 或混合语言内容，这些场景下 1.3 不够准确。内容感知的变量乘数可能将 Phase 2 触发率从 35% 降到 ~15%。

## 否决原因总结

### 1. 跨引擎时长不可迁移

不同引擎韵律模型完全不相关。jieba 估算器已校准 edge-tts，仍有段偏差达 87.7%（idx=53）。跨引擎预测只会更差。

### 2. 本地引擎不支持 rate 控制

5 个引擎中仅 edge-tts 支持 rate。Draft 阶段算出的 corrected_rate 传给本地引擎直接被忽略（pyttsx3 参数遮蔽、piper 无参数、sherpa-onnx 硬编码）。

### 3. 总调用量反增 72%

Draft 时长对 Final 引擎无用，Final 阶段仍需完整反馈循环。72 + 100 = 172 次，比现有 100 次多 72%。

### 4. 零成本探针已存在

`_estimate_duration_jieba` 执行时间 ~0ms/段，无需生成音频，已被 Phase 0 预检、isometric 候选选择、refine 迭代使用。等效于免费的 draft 探针。

## 现有架构流程

```
Phase 0: 预检（jieba 估算，ratio 超标 → LLM 调文本）    [零成本, 0 次 TTS]
Phase 1: 初始 TTS 批量生成（预计算 rate）               [N 次, 并发=5]
Phase 2: Rate 反馈闭环（偏差 >15% → 精确 rate 重生成）  [~0.35N 次]
Phase 3: LLM 反馈闭环（偏差 >20% → 调文本重生成）       [~0.04N 次]
```

## 替代优化方向（已实施）

### Fix #1: Ridge 回归校准 jieba 估算器

**问题**: `_estimate_duration_jieba` 使用硬编码经验值 + 全局 `* 1.3` 韵律乘数，精度不足。

**方案**: 从已有 TTS 音频中收集 (text_zh, actual_duration) 样本对，用 Ridge 回归拟合 8 个时长参数 + 1 个截距。

**关键技术点 — rate 去混淆**:
TTS 生成时应用了 rate 参数调速（`rate = estimated_ms / target_ms`），导致 actual_ms 包含 rate 效果。必须反推自然时长：`natural_ms = actual_ms * applied_rate`。对经过反馈闭环的段，使用 `tts_feedback_log.json` 中的 `corrected_rate`。

**校准结果** (1587 样本, 3 个视频, Ridge alpha=0.1):

| 指标 | 校准前 (硬编码*1.3) | 校准后 (Ridge) | 改善 |
|------|-------------------|---------------|------|
| R² | — | 0.84 | — |
| MAE (ms) | 558.9 | 468.2 | -16% |
| MAPE (%) | 10.4% | 8.7% | -16% |
| 偏差<15% 比例 | 78.9% | 84.6% | +5.7pp |
| Phase2 触发数 | 335 | 245 | **-27%** |

**校准参数对比**:

| 特征 | 原值 | 校准值 | 变化 | 解读 |
|------|------|--------|------|------|
| 单字词 | 200ms | 213ms | +6% | 基本准确 |
| 双字词 | 380ms | 480ms | +26% | 原值偏低 |
| 三字词 | 530ms | 680ms | +28% | 原值偏低 |
| 四字+ | 150ms/字 | 241ms/字 | +61% | 原值严重偏低 |
| 英文字母 | 150ms | 117ms | -22% | 原值偏高 |
| 数字 | 120ms | 256ms | +113% | TTS 念数字很慢 |
| URL字符 | 280ms | 156ms | -44% | 原值偏高 |
| 标点停顿 | 50ms | 164ms | +228% | 停顿比想象的长 |
| 截距 | 0ms | -63ms | — | 整体负偏移 |

**实施**: 
- 校准参数直接替换 `pipeline.py:3049-3108` 中的硬编码值
- 移除所有 `* 1.3` 和 `* 1.1` 乘数（6 处调用点）
- 新增 `calibrate_tts_duration.py` 校准脚本，支持 `--apply` 自动写入

### Fix #2: 跳过不支持 rate 的引擎的 rate 反馈

**问题**: pyttsx3/piper/sherpa-onnx/gtts 不支持 rate 参数调速，Phase 2 rate 反馈对这些引擎做无效重生成。

**方案**: 
- 在 `TTSEngine` 基类添加 `supports_rate = False` 属性
- `EdgeTTSEngine` 和 `Pyttsx3Engine`（修复后）设为 `supports_rate = True`
- `_tts_with_duration_feedback` 检查 `engine.supports_rate`，不支持则跳过并打印提示，由 Phase 3 LLM 闭环补偿

**代码**: `pipeline.py:2000` (基类), `pipeline.py:2053` (edge-tts), `pipeline.py:2637` (跳过逻辑)

### Fix #3: 修复 pyttsx3 rate 参数遮蔽 bug

**问题**: `Pyttsx3Engine.synthesize()` 第 2185 行 `rate = self.rate` 覆盖了函数参数 `rate: float = 1.0`，导致反馈闭环的 `corrected_rate` 被丢弃，pyttsx3 永远以固定 180 WPM 合成。

**修复**: 
- `self.rate` 重命名为 `self.base_rate`
- 新增 `wpm = int(self.base_rate * rate)` 将反馈 rate 作为基础语速的倍率
- 设 `supports_rate = True`，使 pyttsx3 也能参与 Phase 2 rate 反馈闭环

**代码**: `pipeline.py:2174-2193`

## 端到端验证

`test_pipeline.sh --fast` 对 zjMuIxRvygQ 视频完成端到端管线测试（跳过 transcribe+translate，复用缓存）：

**结果**: 全部通过

| 指标 | 值 |
|------|-----|
| 总段数 | 72 |
| Phase 2 rate 反馈触发 | 20 段 (27.8%) |
| Phase 3 LLM 反馈触发 | 0 段 |
| ratio 均值 | 1.008 |
| ratio 标准差 | 0.0904 |
| ±15% 内 | 94.4% |
| outliers >1.4 | 0 |
| CPS 均值 | 3.76 |
| CPS p95 | 5.08 |
| 等时合规率 | 72.2% |

**对比校准前** (旧 25 段 → 新 20 段 Phase 2 触发，-20%)。calibrate 脚本在 1587 样本上的批量预测为 335→245 (-27%)，实际单视频端到端验证为 25→20 (-20%)，方向一致。

**单元测试**: 312 项全部通过
