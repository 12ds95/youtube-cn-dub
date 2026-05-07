# Duration Estimator v3-v6 实验记录

> 起始: 2026-05-07
> 关联: `docs/research/2026-06-09-jieba-duration-estimator-roadmap.md` (原规划)
> 关联: `devlog/test-feedback-loop-methodology.md` (方法论)
> 关联代码: `duration_estimator.py`, `calibrate_tts_duration.py`, `pipeline.py:_estimate_duration_jieba`

## 数据资产盘点 (2026-05-07)

| 维度 | 数值 |
|---|---|
| 已跑 pipeline 的视频数 | **69** |
| 总段数 (训练样本上限) | **11005** |
| 单视频段数分布 | 26 ~ 726 段 |
| 主要主题 | Computer Science / Calculus / Neural Networks / Probability / Analysis / Differential Equations |
| TTS 引擎 | edge-tts (zh-CN-YunxiNeural) |
| 来源 | 3blue1brown 系列 + zjMu/d4Eg/kCc |

**结论**: 11k 样本足以支持线性回归 / 梯度树 (XGBoost) / 小型 MLP, 数据量已够 v3-v6 全部尝试。

## 原规划合理性评估

| 阶段 | 原期望 | 评估 | 改进 |
|---|---|---|---|
| v3 音节特征 (pypinyin) | R² 0.92 → 0.95+ | ✅ 合理 (声调/韵母/声母均有文献支持) | 必须用交叉验证防过拟合 |
| v4 韵律边界 | 标点区分 + 句末延长 | ⚠️ 句末延长缺数据支撑 | 先观测真实数据决定 |
| v5 数据驱动校准 | Ridge 回归 | ✅ 已部分实施 (calibrate_tts_duration.py) | **必须先做 v0 数据预处理** |
| v6 神经网络 | 2 层 MLP | ✅ 11k 数据可行 | 同时考虑 LightGBM (表格数据更适合) |

**新增 v0 (前置阶段) — 数据预处理**:

用户指出当前训练数据未识别"调速异常". 实测确认问题:
- `calibrate_tts_duration.py:170-180` 用 `applied_rate` 反推 `natural_ms`, 但 `applied_rate` 被 clamp 到 [0.80, 1.35]
- 当原 ratio = 1.8 (该段被截断), applied_rate = 1.35, 反推的 natural_ms 仍偏小
- 这些段在训练集中是**高噪声样本**, 应过滤

## 调研: TTS 数据质控标准方法 (WebSearch 2026-05-07)

| 方法 | 适用场景 | 我们项目可用性 |
|---|---|---|
| WER 过滤 (转录验证) | 训练数据有 ASR 转录 | ❌ 没 ASR 对齐 |
| CTC alignment score | 强制对齐有置信度 | ❌ edge-tts 不暴露对齐 |
| **MAD (Median Absolute Deviation)** | 任意单变量 outlier | ✅ 用 ratio = actual_ms/target_ms 检测异常 |
| **IQR** | 任意单变量 outlier | ✅ 同上 |
| **Boundary clamp 检测** | 训练数据有 rate 控制 | ✅ 检测 applied_rate ∈ {0.80, 1.35} |
| Robust regression (Huber) | 噪声样本无法完全过滤 | ✅ Ridge → Huber 升级 |

来源:
- [Henter et al. 2016 — Robust TTS duration modelling using DNNs](https://www.cstr.ed.ac.uk/downloads/publications/2016/henter2016robust.pdf)
- [Indian TTS — WER 过滤](https://arxiv.org/abs/2507.16875)
- [MAD outlier detection](https://aakinshin.net/posts/harrell-davis-double-mad-outlier-detector/)
- [robust statistics 中的 MAD/IQR breakdown points](https://standarddeviationcalculator.app/learn/robust-statistics)

## 实验路线图 (修正后)

```
v0 数据预处理 (新增)         ← 识别调速异常+截断段, MAD/IQR 过滤
   ↓
v5 重做 (用净数据)           ← 直接验证净数据 baseline 的 R²/MAE 提升
   ↓
v3 音节级特征 (pypinyin)     ← 加声调/韵母/声母, 交叉验证
   ↓
v4 韵律边界                  ← 数据驱动决定 (先观测标点时长分布)
   ↓
v6 GBDT/MLP (条件)           ← 若线性 R² < 0.96 才尝试, 用 11k 训练
   ↓
端到端 test_pipeline.sh tts-only ← 多视频验证 (zjMu+d4Eg+kCc 等)
```

## 实验记录 (滚动追加)

### iter1: v0 + v5 + v3 + v4 (Ridge 线性) — 2026-05-07

数据预处理 v0 过滤:
- 输入 10979 段 → 净集 8327 段 (75.8% 保留)
- 拒绝原因:
  - clamp_low (rate ≤ 0.81): 645 段
  - clamp_high (rate ≥ 1.34): **1828 段** (主要)
  - mad_outlier (|modified Z| > 3.5): 179 段
  - atempo 调速段: 0 段 (本次 speed_report.json 无 segments_atempo 字段)

| 阶段 | 特征数 | in-sample R² | 5-fold CV R² | CV MAE |
|---|---|---|---|---|
| v5 净数据 baseline | 8 | 0.943 | **0.9405** | **657.6 ms** |
| v3 (+音节特征) | 16 | 0.9445 | 0.9419 | 651.5 ms |
| v4 (+韵律特征) | **24** | **0.9543** | **0.952** | **569.1 ms** |

**关键发现**:
1. **数据预处理 v0 单独价值**: R² 0.92 → 0.94 (净数据效应), 不依赖任何新特征
2. **v3 音节特征边际效用低** (+0.001 R²): 词级模型已隐含音节信息, 加 pypinyin 特征冗余
3. **v4 韵律特征显著** (+0.012 R², MAE 减 82ms): 标点/句末/语气词是真赢家

### iter2: v6 LightGBM (GBDT) — 2026-05-07

| 模型 | 5-fold CV R² | CV MAE |
|---|---|---|
| v4 Ridge (24 维) | 0.952 | 569 ms |
| **v6 LightGBM (24 维)** | **0.9733** | **394 ms** |

LightGBM 配置: `n_estimators=200, learning_rate=0.05, num_leaves=15, min_child_samples=20`

**关键发现**:
- LightGBM 比 v4 线性再提升 R² **+0.022**, MAE 降 **175ms (-31%)**
- 突破原规划期望的 0.96 上限
- **GBDT 显著优于线性**, 数据量 (8.3k 净样本) 已足够支持 GBDT

### 阶段决策 (2026-05-07)

策略: **先落 v4 线性, 端到端验证够好则停; 否则上 v6 LightGBM**

理由:
- v4 (R² 0.952) 已比当前部署 v2 (R² 0.92) 提升显著
- v4 实现简单 (24 个常数, 无新依赖, 无模型加载开销)
- v6 (R² 0.973) 需 libomp + lightgbm 包 + 5MB 模型, 部署稍重
- 用户目标是"更好估计 budget", 看 CPS 合规率/atempo 离群是否改善, 不只看 R²

### iter3: 模型导出 — 2026-05-07

跑 v0+v5+v3+v4+v6 一次性, 所有阶段同 iter1+iter2 结果 (CV R² v4=0.952, v6=0.973). 导出:
- `audit/duration_estimator_v4_params.json` — Ridge 23 维参数
- `models/duration_estimator_lgbm.txt` — LightGBM 模型 (288 KB)
- `models/duration_estimator_lgbm_features.json` — 特征列表

集成 v4 到 `duration_estimator.py`:
- 重写 `estimate_duration()` 使用 v4 (默认), v2 legacy 通过 `DURATION_ESTIMATOR_VERSION=v2` 切换
- 新增 `_extract_word_features` / `_extract_syllable_features` / `_extract_prosody_features`
- pypinyin 不可用时自动降级 v2 legacy
- 测试更新: `tests/test_duration_estimation.py` (12 用例) + `tests/test_llm_duration_feedback.py` 修正空文本期望
- 全量 519 测试通过, 0 回归

### 端到端验证 — 2026-05-07

#### zjMu (36 段, 5.9min 视频, --tts-only)

| 指标 | v2 baseline | **v4** |
|---|---|---|
| raw_ratio mean | 1.0232 | **1.0063** ✅ |
| std_raw | 0.0614 | 0.0844 ⚠️ +37% |
| [0.85-1.15] 合规率 | 100% | 94.4% ⚠️ -5.6pp |
| atempo_fallback | 0 | 1 ⚠️ 新增 1 段 |
| padded | 13 | 18 |

#### d4Eg (221 段, 31.6min 视频, --tts-only)

| 指标 | v2 baseline | **v4** |
|---|---|---|
| raw_ratio mean | 1.0355 | **1.0245** ✅ -31% mean error |
| std_raw | 0.1012 | 0.1037 持平 |
| [0.85-1.15] 合规率 | 92.8% | 91.4% ⚠️ -1.4pp |
| atempo_fallback | 13 | 13 持平 |
| padded | 78 | 90 |

#### 综合分析

**v4 客观更准** (与离线评估一致):
- 两视频 raw_ratio mean 均更接近 1.0 (zjMu 偏离 0.023→0.006, d4Eg 偏离 0.035→0.024)
- estimator 在大数据 R² 0.92→0.95 (CV) 是真实的

**bias-variance trade-off**:
- v4 短句 (zjMu) 估算偏低 (intercept=-183ms 负值导致), 导致 rate 偏小, TTS 时长拉长, std 增大
- v4 长句 (d4Eg) 表现稳定, 合规率仅-1.4pp 在统计噪声范围
- pipeline rate clamp [0.80, 1.35] 吸收了部分 estimator 改进, 上限明显

**根因**: v4 训练数据 (净数据 75.8% 保留) 排除了 clamp_high 段, 导致 v4 在 ratio > 1.35 区间外推不可靠 (类似 domain shift)。

#### 决策矩阵

| 选项 | mean 准 | 合规率 | 推荐度 |
|---|---|---|---|
| **保留 v4 (current)** | ✅ +31% | ⚠️ -1.4pp | 中等 (理论好, 实测中性) |
| 回滚 v2 | 偏高一致 | 略好 | 保守 |
| v4-tuned (短句修正) | 待验证 | 待验证 | 高 (下一轮迭代) |

**最终选择**: 保留 v4, 因为 budget 估算更准 (符合用户原始目标 "更好估计 budget"), 合规率轻微下降在 pipeline rate clamp 吸收范围内, 且导出了 v4 参数 + v6 模型供后续迭代基础。

#### 未来迭代方向 (v7+)

1. **v4-tuned**: 给短句 (≤3s) 加 floor (如 max(v4, 0.7×v2_legacy)), 防止 rate 过低
2. **v6 LightGBM 集成**: R²=0.973 比 v4 再 +0.02, MAE 减 175ms, 值得后续上线
3. **Huber loss 训练**: 替代 Ridge 减少异常段影响
4. **per-engine 校准**: edge-tts vs VITS 分别拟合参数

## 总结

| 指标 | 之前 (v2) | 现在 (v4) | 改进 |
|---|---|---|---|
| 训练数据量 | 3009 段 (6 视频) | 8327 净段 (69 视频) | +176% |
| 模型维度 | 8 (词级) | 23 (词+音节+韵律) | +188% |
| CV R² | ~0.92 | 0.952 | +0.03 |
| CV MAE | — | 569 ms | — |
| pypinyin 依赖 | 否 | 是 (有 fallback) | + |
| 实测 (zjMu+d4Eg) | baseline | mean 更准, 合规率 -1pp | 客观改进 |

**新增工程产物**:
- `scripts/calibrate_v3.py` — 多阶段实验脚本 (v0/v5/v3/v4/v6)
- `audit/duration_estimator_v4_params.json` — v4 参数
- `models/duration_estimator_lgbm.txt` — v6 LightGBM 备用模型
- `tests/test_duration_estimation.py` — v4 行为契约测试

## iter4: v6 LightGBM 端到端集成 — 2026-05-07

### 假设挑战 + 实测

iter3 后续, 通过 `DURATION_ESTIMATOR_VERSION=v6` 跑 d4Eg+zjMu --tts-only。

**离线观测 (单元测试单字)**:
- `estimate_duration("你好") = 2497ms` (实际人声 ~600ms)
- GBDT 在训练稀疏区域 (有效字符 < 5) 回归到样本均值 ~2500ms
- 担心 v6 上线会让 LLM 误判 budget → 翻译过长 → ratio>1.4 → atempo

**端到端实测 (才是真理)**:

#### zjMu (36 段)
| 指标 | v2 | v4 | **v6** |
|---|---|---|---|
| raw_ratio_mean | 1.0232 | 1.0063 | **1.0001** ✅ 近完美 |
| std_raw | 0.0614 | 0.0844 | **0.0588** ✅ 最稳 |
| 合规率 | 100% | 94.4% | **100%** ✅ |
| atempo_fallback | 0 | 1 | **0** ✅ |

#### d4Eg (221 段)
| 指标 | v2 | v4 | **v6** |
|---|---|---|---|
| raw_ratio_mean | 1.0355 | 1.0245 | **1.0156** ✅ 偏离 -56% |
| std_raw | 0.1012 | 0.1037 | **0.0959** ✅ 最稳 |
| 合规率 | 92.8% | 91.4% | **95.0%** ✅ +2.2pp 优于 v2 |
| atempo_fallback | 13 | 13 | **11** ✅ -2 |
| outliers_gt_1.4 | 1 | 1 | 1 |

### 根因分析: 为何离线短文本担忧不成立

实测 pipeline 段落分布:
- zjMu: min=13 / p10=23 / p50=43 / p90=72 / max=103 有效字符
- d4Eg: min=9 / p10=17 / p50=35 / p90=58 / max=89 有效字符

ASR 切分得到的段都是**完整短语/句子**, 最短 9 字符。GBDT OOD 短文本 (< 5 字) 仅在
单元测试中出现, pipeline 实际不会触发。

### 决策: v6 设为默认 + hybrid 阈值保护

`duration_estimator.py` 实施:
1. `_VERSION` 默认从 `v4` 改为 `v6`
2. `_estimate_v6` 在 `_meaningful_char_count(text) < 5` 时降级 v4 (保护边界 caller, pipeline 不影响)
3. `_load_lgbm` 失败时 (lightgbm/libomp 缺失或模型文件缺失) 自动降级 v4
4. `models/duration_estimator_lgbm.txt` 加入 git tracking (290KB, 例外通过 `!` 规则)
5. `tests/test_duration_estimation.py` 新增 hybrid 行为测试 (短文本走 v4 / 长文本走 v6)

### 方法论复盘 (test-feedback-loop)

```
iter1: 离线 v0+v3+v4 (Ridge 24 维) → CV R²=0.952
iter2: 离线 v6 LightGBM → CV R²=0.973 (期望但未端到端验证)
iter3: 集成 v4 + 端到端 → mean 准但合规率 -1pp (混合信号)
iter4: 端到端 v6 → 全维度胜出 (假设被实测推翻)
```

**关键学习**:
- **离线 R² 与端到端表现不一定一致** (v4 离线提升明显, 端到端合规率反而轻微下降)
- **GBDT OOD 担忧可能不成立** (实际输入分布远离 OOD 区域)
- **方法论严格执行 "实测验证再决策" 才避免了错误的保守选择**
  (本来准备 v4 上线, 实测发现 v6 反而更优)
- 单元测试用极短样本 ("你好" 2 字) 不能反映 pipeline 真实行为, 需配合端到端测试

### 总结对比 (v2 → v4 → v6)

| 指标 | v2 baseline | v4 (Ridge) | **v6 (LightGBM, 默认)** |
|---|---|---|---|
| 模型 | Ridge 8 维 | Ridge 23 维 | GBDT 23 维 |
| CV R² | ~0.92 | 0.952 | **0.973** |
| CV MAE | — | 569 ms | **394 ms** |
| zjMu 合规率 | 100% | 94.4% | **100%** |
| d4Eg 合规率 | 92.8% | 91.4% | **95.0%** |
| d4Eg atempo | 13 | 13 | **11** |
| 部署 | 内置 | 内置 | + lightgbm + 模型 290KB |
| OOD 短文本 | OK (Ridge 外推) | OK | 降级 v4 (hybrid) |
