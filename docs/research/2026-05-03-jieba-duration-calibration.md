# Jieba TTS 时长估算器 Ridge 回归校准

**日期**: 2026-05-03
**状态**: 已实施
**关联 commit**: `abae2fd`

## 1. 问题背景

`_estimate_duration_jieba` 是管线中 TTS 时长的零成本预估器（无需生成音频），被 Phase 0 预检、等时翻译候选选择、refine 迭代、Phase 1 rate 预计算等多处使用。其精度直接决定 Phase 2 rate 反馈闭环的触发次数——每次触发意味着一次额外 TTS 调用。

**现状问题**:
- 8 个时长参数为人工硬编码经验值
- 全局 `* 1.3` 韵律乘数过于粗糙
- Phase 2 触发率 35%（72 段触发 25 段），存在优化空间

## 2. WebSearch 调研

### 2.1 搜索的方案

| 方案 | 原理 | 适用场景 |
|------|------|---------|
| **Ridge 回归** | 线性模型 + L2 正则化，拟合特征权重 | 特征与目标线性相关、样本量中等 |
| **Lasso 回归** | 线性模型 + L1 正则化，自动特征选择 | 高维稀疏特征，需剔除无关特征 |
| **经验贝叶斯** | 先验分布 + 观测数据后验更新 | 小样本、有先验知识 |
| **Gradient Boosted Trees** | 非线性集成方法 (XGBoost/LightGBM) | 大规模数据、复杂非线性关系 |
| **Neural TTS Duration Model** | 如 FastSpeech 的 duration predictor | 端到端深度学习 TTS 系统内部 |

### 2.2 方案选择理由

选择 **Ridge 回归**，原因：
1. **特征结构匹配**: `_estimate_duration_jieba` 本身就是线性加权模型（8 个特征 × 权重 + 截距），Ridge 直接拟合其参数
2. **可解释性**: 校准后的参数可以直接替换硬编码值，无需引入模型推理
3. **零运行时依赖**: 校准是离线过程，运行时仍是简单乘加
4. **样本量适中**: 1587 样本足够 Ridge 收敛，不需要深度学习
5. **无 sklearn 依赖**: 手动实现 `(X^T X + αI)^{-1} X^T y`，只需 numpy

### 2.3 排除的方案

- **Lasso**: 8 个特征都有明确语言学意义，不需要自动剔除
- **GBT**: 非线性模型拟合后无法直接写回线性估算器
- **Neural Duration Model**: 过度工程化，且需要 GPU 推理

## 3. 算法设计

### 3.1 特征工程

将 `_estimate_duration_jieba` 的分词逻辑分解为 8 维特征向量：

```
x = [n_1char, n_2char, n_3char, n_4plus, n_letters, n_digits, n_url_chars, n_punct]
```

| 特征 | 含义 | 提取方式 |
|------|------|---------|
| `n_1char` | 单字中文词数 | jieba 分词后中文字符数=1 的词 |
| `n_2char` | 双字中文词数 | jieba 分词后中文字符数=2 的词 |
| `n_3char` | 三字中文词数 | jieba 分词后中文字符数=3 的词 |
| `n_4plus` | 四字及以上中文字符总数 | jieba 分词后中文字符数≥4 的词中字符累计 |
| `n_letters` | 英文字母数 | 非中文非数字的 alnum 字符 |
| `n_digits` | 数字字符数 | isdigit() 字符 |
| `n_url_chars` | URL 中的 alnum/符号数 | 正则匹配 URL 后提取 |
| `n_punct` | 标点/停顿数 | Unicode P/Z/C 类别的词 |

### 3.2 Rate 去混淆（关键设计点）

**问题**: TTS 生成时已应用了 rate 参数调速：
```
applied_rate = clamp(estimated_ms / target_ms, 0.80, 1.35)
```
因此 `actual_ms`（mp3 实际时长）不等于 `natural_ms`（rate=1.0 下的自然时长）。直接用 `actual_ms` 做回归目标会学到被 rate 扭曲的参数。

**解决**: 反推自然时长：
```
natural_ms = actual_ms × applied_rate
```

**rate 来源**:
- 经过 Phase 2 反馈的段: 从 `tts_feedback_log.json` 读取 `corrected_rate`
- 未经反馈的段: 用旧公式 `_estimate_duration_jieba(text) * 1.3 / target_ms` + clamp 重算

**首次实验教训**: 初次校准直接用 `actual_ms` 做目标（未去混淆），R²=0.55，拟合很差。加入 rate 去混淆后 R²=0.84。

### 3.3 Ridge 回归实现

手动实现，无 sklearn 依赖：

```python
# 超参数搜索: α ∈ {0.1, 1.0, 10.0, 100.0}
for α in alphas:
    w = (X^T X + αI)^{-1} X^T y      # 无截距，选最优 α
    R² = 1 - SS_res / SS_tot

# 用最优 α 带截距重新拟合
X_bias = [X | 1]                       # 增广矩阵
w = (X_bias^T X_bias + αI')^{-1} X_bias^T y
# 截距列正则化权重为 α × 0.01（少惩罚截距）
```

### 3.4 校准参数自动写入

`calibrate_tts_duration.py --apply` 通过正则替换将参数写入 `pipeline.py`：
- 替换 `total_ms += 200` → `total_ms += 212` 等 8 个数值
- 替换 docstring 中的经验值注释
- 添加截距 `return max(0, total_ms - 63)`
- 移除所有 `* 1.3` 和 `* 1.1` 乘数（6 处）

## 4. 实验过程

### 4.1 数据收集

从 3 个已处理视频收集训练数据:

| 视频 ID | 段数 | 有效样本 |
|---------|------|---------|
| d4EgbgTm0Bg | ~400 | ~380 |
| kCc8FmEb1nY | ~600 | ~560 |
| zjMuIxRvygQ | ~700 | ~647 |
| **总计** | | **1587** |

过滤条件: text_zh ≥ 2 字符、mp3 ≥ 100 bytes、actual_ms ≥ 200ms

### 4.2 实验 1: 无 rate 去混淆（失败）

```
R² = 0.55  ← 太低
原因: actual_ms 被 rate 扭曲，回归学到的是 rate-adjusted 时长参数
```

### 4.3 实验 2: 加入 rate 去混淆（成功）

```
R² = 0.84
最优 α = 0.1
```

### 4.4 校准结果

| 特征 | 原值 | 校准值 | 变化 | 解读 |
|------|------|--------|------|------|
| 单字词 | 200ms | 212ms | +6% | 基本准确 |
| 双字词 | 380ms | 479ms | +26% | 原值偏低 |
| 三字词 | 530ms | 679ms | +28% | 原值偏低 |
| 四字+ | 150ms/字 | 240ms/字 | +60% | 原值严重偏低 |
| 英文字母 | 150ms | 116ms | -23% | 原值偏高 |
| 数字 | 120ms | 255ms | +113% | TTS 念数字很慢（"一二三四"） |
| URL字符 | 280ms | 155ms | -45% | 原值偏高 |
| 标点停顿 | 50ms | 164ms | +228% | 停顿比预期长 |
| 截距 | 0ms | -63ms | — | 整体负偏移补偿 |

**语言学解读**:
- 多字词被低估最多（双/三/四字），说明 edge-tts 对连续中文词有更长的韵律延伸
- 数字被严重低估，TTS 倾向于将 "123" 读作 "一百二十三" 而非 "一二三"
- 英文字母和 URL 被高估，可能因为 edge-tts 对英文部分语速较快
- 标点停顿从 50ms 到 164ms，说明 edge-tts 在标点处插入了比预期更长的静音

### 4.5 精度对比

| 指标 | 校准前 (硬编码×1.3) | 校准后 (Ridge) | 改善 |
|------|-------------------|---------------|------|
| MAE (ms) | 558.9 | 468.2 | -16% |
| MAPE (%) | 10.4% | 8.7% | -16% |
| 偏差<15% | 78.9% | 84.6% | +5.7pp |
| Phase2 触发 | 335/1587 | 245/1587 | **-27%** |

### 4.6 端到端验证

`test_pipeline.sh --fast` 对 zjMuIxRvygQ 视频:

| 指标 | 校准前 | 校准后 |
|------|--------|--------|
| Phase 2 触发 | 25/72 (35%) | 20/72 (28%) |
| ratio 均值 | 1.011 | 1.008 |
| ±15% 内 | 90% | 94.4% |
| outliers >1.4 | 0 | 0 |

批量预测 -27% vs 端到端 -20%，方向一致，差异来自端到端包含 Phase 0 预检和 isometric 翻译的交互效应。

## 5. 附带修复

### Fix #2: supports_rate 引擎能力标记

在 `TTSEngine` 基类添加 `supports_rate = False`，不支持 rate 的引擎跳过 Phase 2 反馈：
- `edge-tts`: `supports_rate = True`
- `pyttsx3`: `supports_rate = True`（修复后）
- `gtts/piper/sherpa-onnx`: `supports_rate = False` → 跳过 Phase 2，由 Phase 3 LLM 补偿

### Fix #3: pyttsx3 rate 参数遮蔽修复

```python
# 修复前: rate 参数被 self.rate 覆盖
rate = self.rate  # ← 永远 180 WPM

# 修复后: rate 作为基础语速的倍率
wpm = int(self.base_rate * rate)  # rate=1.2 → 216 WPM
```

## 6. 相关文件

| 文件 | 说明 |
|------|------|
| `calibrate_tts_duration.py` | 校准脚本（特征提取 + Ridge 回归 + 自动写入） |
| `calibration_result.json` | 校准输出（参数 + 指标） |
| `pipeline.py:3049-3108` | `_estimate_duration_jieba` 函数（校准后参数） |
| `pipeline.py:2000` | `TTSEngine.supports_rate` 属性 |
| `pipeline.py:2637` | Phase 2 跳过逻辑 |
| `tests/test_tts_engines.py` | `test_supports_rate_property()` |
| `devlog/2026-05-03-two-stage-tts-evaluation.md` | 两阶段方案评估（否决）+ 替代优化总结 |

## 7. 未来改进方向

- **增量校准**: 每次管线运行后自动收集新样本，定期重新拟合
- **per-voice 校准**: 不同 voice（如 YunxiNeural vs XiaoxiaoNeural）可能有不同韵律特征
- **非线性特征**: 如词与词之间的交互效应（连续短词 vs 长词混合）
- **更多训练数据**: 当前 1587 样本来自 3 个视频，覆盖领域有限
