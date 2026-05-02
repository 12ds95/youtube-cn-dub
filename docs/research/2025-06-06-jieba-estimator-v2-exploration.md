# Jieba TTS 时长估算器 v2 校准

**日期**: 2025-06-06
**状态**: 已实施
**前置**: `docs/research/2026-05-03-jieba-duration-calibration.md` (Ridge v1)

## 1. 目标

以 v1 `_estimate_duration_jieba` (Ridge 线性 8 特征, 3 视频 1587 样本) 为基线，扩大训练数据到 6+ 视频，探索非线性特征，减少 Phase 2 feedback loop 触发。

## 2. 数据扩展

### 2.1 v1 训练数据: 3 视频 1587 样本

| 视频 ID | 段数 |
|---------|------|
| d4EgbgTm0Bg | 364 |
| kCc8FmEb1nY | 1153 |
| zjMuIxRvygQ | 72 |

### 2.2 v2 训练数据: 6 视频 3009 样本

新增 3 个视频（从嵌套系列目录中选取并运行 `test_two_videos.sh`）:

| 视频 ID | 领域 | 段数 |
|---------|------|------|
| d4EgbgTm0Bg | — | 364 |
| kCc8FmEb1nY | — | 1153 |
| zjMuIxRvygQ | — | 72 |
| Calculus/WUvTyaaNkzM | 微积分 | 312 |
| Computer Science/03_But_how_does_bitcoin... | 计算机 | 403 |
| Probability/02_The_medical_test_paradox... | 概率 | 705 |
| **总计** | | **3009** |

`test_two_videos.sh` 已更新为包含 10 个视频（7 个新增），后台运行中（3/7 完成），剩余视频完成后可进一步扩大训练集。

## 3. Bug 修复: rate 去混淆

### 问题

`calibrate_tts_duration.py` 的 `collect_samples()` 中，对未经 Phase 2 反馈的段用旧公式反推 applied_rate:
```python
est_ms = _estimate_duration_jieba(text_zh) * 1.3  # ← BUG
```

v1 校准后 pipeline.py 已移除 `* 1.3` 韵律乘数，但 calibrate 脚本中残留了这个旧乘数。导致 natural_ms 反推偏高，回归学到的参数也偏高（v1 的 intercept=-63 实际应该更大）。

### 修复

```python
est_ms = _estimate_duration_jieba(text_zh)  # 无乘数，与 pipeline 一致
```

修复后重新校准，R² 从 0.80 提升到 0.92，说明 rate 去混淆更准确了。

## 4. 模型探索

### 4.1 候选方案

| 方法 | 原理 | 适用性 |
|------|------|--------|
| **Ridge (baseline)** | 8 线性特征 | 现有方案 |
| **Ridge + 二次项** | +n_1char², n_2char² | 捕捉非线性 |
| **Ridge + 交互项** | +n_1char×n_2char 等 | 捕捉词类交互 |
| **Ridge + 全 degree-2** | 44 维多项式特征 | 完整非线性 |
| **Huber 回归** | 鲁棒损失函数 | 抗异常值 |
| **log 变换** | +log(1+total_zh) | 压缩长尾 |

### 4.2 Leave-One-Video-Out 交叉验证结果

评估指标: Phase 2 触发率（偏差 ≥15% 的段占比）。CV 确保对未见视频的泛化能力。

| 模型 | alpha | 特征数 | In-sample P2 | **CV P2** |
|------|-------|--------|-------------|-----------|
| Ridge 8-base | 0.1 | 8 | 13.9% | 18.7% |
| Ridge 8-base | **50** | 8 | 14.3% | **18.2%** |
| Ridge 8-base | 100 | 8 | 14.2% | 18.3% |
| Ridge +sq | 0.1 | 10 | 16.7% | 25.4% |
| Ridge +sq | 10 | 10 | 16.6% | 25.6% |
| Huber 8-base | 1.0 | 8 | — | 24.1% |
| Ridge +log | 0.1 | 9 | — | 23.4% |
| Ridge +1c²+2c² | 10 | 10 | — | 23.1% |

### 4.3 关键发现

1. **非线性特征过拟合**: 二次项、交互项在 in-sample 改善但 CV 变差（25.4% vs 18.2%）
2. **Huber 回归无优势**: 对异常值的鲁棒性未转化为更好的 CV 表现
3. **更多数据 > 更多特征**: 从 3 视频扩展到 6 视频的提升远大于特征工程
4. **正则化很重要**: alpha=50 比 alpha=0.1 好（18.2% vs 18.7%），高正则化防止过拟合新视频
5. **Rate 去混淆 bug 是最大改善源**: 修复后 R² 从 0.80 升到 0.92

### 4.4 Per-Video CV 分析

| 视频 | 段数 | v2 CV Phase 2 |
|------|------|--------------|
| zjMuIxRvygQ | 72 | 12.5% |
| Probability/02... | 705 | 13.3% |
| d4EgbgTm0Bg | 364 | 14.6% |
| CS/03_bitcoin... | 403 | 17.4% |
| Calculus/WUvTyaaNkzM | 312 | 19.2% |
| kCc8FmEb1nY | 1153 | 22.8% |

kCc8FmEb1nY 最难预测（最大的视频，可能内容多样性高）。

## 5. 最终校准参数 (v2)

**模型**: Ridge, 8 特征, alpha=50
**数据**: 6 视频, 3009 样本

| 特征 | v1 值 | v2 值 | 变化 | 解读 |
|------|-------|-------|------|------|
| 单字词 | 212ms | 138ms | -35% | v1 高估（受 *1.3 bug 影响） |
| 双字词 | 479ms | 361ms | -25% | 同上 |
| 三字词 | 679ms | 506ms | -25% | 同上 |
| 四字+/字 | 240ms | 223ms | -7% | 较稳定 |
| 英文字母 | 116ms | 31ms | -73% | 大幅下降（截距补偿） |
| 数字 | 255ms | 311ms | +22% | TTS 念数字确实慢 |
| URL字符 | 155ms | 16ms | -90% | 被截距吸收 |
| 标点停顿 | 164ms | 197ms | +20% | 停顿略长 |
| 截距 | -63ms | +1210ms | — | 正截距：基础句时长 |

**参数变化解读**: v1 的 intercept=-63 + 高特征权重 ≈ v2 的 intercept=+1210 + 低特征权重。两种参数化等价但 v2 更准确，因为:
- 修复了 rate 去混淆 bug
- 更大训练集（3009 vs 1587）
- 更高正则化（alpha=50 vs 0.1）

## 6. 修改文件

| 文件 | 变更 |
|------|------|
| `pipeline.py:3054-3118` | v2 校准参数写入 `_estimate_duration_jieba` |
| `calibrate_tts_duration.py` | 修复 rate 去混淆 bug、支持嵌套目录扫描、更新基线参数 |
| `test_two_videos.sh` | 从 3 → 10 视频，支持嵌套路径 |
| `docs/research/2025-06-06-jieba-estimator-v2-exploration.md` | 本文档 |

## 7. 后续

- **增量校准**: 后台管线完成后（7/7 新视频），重新运行 `calibrate_tts_duration.py --apply` 更新参数
- **端到端验证**: 用新参数对 zjMuIxRvygQ 跑端到端测试，对比 v1 的 Phase 2 触发率 (28%)
- **per-voice 校准**: 不同 voice 可能需要不同参数（当前仅 YunxiNeural）
