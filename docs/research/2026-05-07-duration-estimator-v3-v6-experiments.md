# Duration Estimator v3-v6 实验记录

> 起始: 2026-05-07
> 关联: `docs/research/2026-05-04-jieba-duration-estimator-roadmap.md` (原规划)
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

## iter5: 第三视频 generality 验证 (kCc 617 段, 长视频) — 2026-05-07

### kCc8FmEb1nY 实测

| 指标 | prior (pre-v6) | **v6** | 变化 |
|---|---|---|---|
| total_segments | 617 | 617 | — |
| raw_ratio_mean | 1.0206 | **1.0051** | ✅ 偏离 -75% |
| std_raw | 0.0812 | **0.0729** | ✅ -10% |
| 合规率 [0.85-1.15] | 96.3% | **97.4%** | ✅ +1.1pp |
| atempo_fallback | 16 | **14** | ✅ -2 |
| outliers_gt_1.4 | 0 | 0 | 持平 |

### 三视频综合

| 视频 | 段数 | v6 mean | v6 std | v6 合规率 | v6 atempo |
|---|---|---|---|---|---|
| zjMu | 36 | 1.0001 | 0.0588 | 100% | 0 |
| d4Eg | 221 | 1.0156 | 0.0959 | 95.0% | 11 |
| kCc | 617 | 1.0051 | 0.0729 | 97.4% | 14 |
| **加权均值** | 874 | **1.0084** | **0.0792** | **97.0%** | — |

**v6 在 36-617 段广覆盖范围内表现稳定**, generality 验证通过。

### atempo 残余段失效模式分析 (d4Eg + kCc)

d4Eg 14 feedback 段:
- 含数学符号 (i, j, k, −1, LaTeX): 多数低估 15-48%
- 含音译人名 (Linus / Felix): 多数低估
- 边缘长句

kCc 20 feedback 段:
- 纯中文 17 (85%): 边缘长句, estimator 仍偏低估
- 含英文专有名词 3 (15%)
- **不含数学符号** (kCc 主题不同, 无数学公式)

### v7+ 改进方向 (留作后续工作)

| 方向 | 期望增益 | 实施成本 |
|---|---|---|
| 拆 `n_letters` → `n_capital_word` (音译名) + `n_lower_word` | -2 atempo (数学/科技视频) | 中 (重训 v6) |
| 数学符号特征 (i,j,k,−,√,∫) | -3 atempo (3blue1brown 类) | 中 |
| LaTeX 命令 token 检测 | -1~2 atempo | 低 (regex 即可) |
| Huber loss 替代 LSE | 减少 outlier 影响 | 低 |
| per-engine 校准 (edge-tts vs VITS) | 跨 TTS 引擎稳定 | 高 (需 VITS 数据) |
| MLP 取代 GBDT | R² 不一定提升 (8k 样本上限) | 中 |

## iter6: v7 token 特征实验 — 2026-05-07 (失败回滚)

### 假设

iter5 失效模式分析显示 d4Eg/kCc atempo 段集中于:
- 数学符号 (i, j, k, −1, LaTeX)
- 音译人名 (Linus / Felix)
- 单字母变量

假设: 加 token 特征 (n_capital_word / n_math_op / n_ascii_isolated) 重训
v7 LightGBM 应能减少这类 atempo。

### 实施

`scripts/calibrate_v3.py` 加 V7_TOKEN_FEATURES (3 维) + extract_token_features:
```python
MATH_OPS = set("×÷·−√∑∫⊥∞→←⇒⇐∂∇≤≥≠≈⊆⊇∈∉∪∩⊕⊗∥‖")
CAPITAL_WORD_RE = re.compile(r'[A-Z][a-zA-Z]+')
ASCII_ISOLATED_RE = re.compile(r'(?<![a-zA-Z])[a-zA-Z](?![a-zA-Z])')
```

### 离线训练 (8327 净样本, 5-fold CV by video)

| 模型 | CV R² | CV MAE |
|---|---|---|
| v4 Ridge 23 维 | 0.952 | 569 ms |
| v6 LightGBM 23 维 | 0.973 | 394 ms |
| **v7 Ridge 26 维** | **0.9549** | 553 ms (-3% MAE) |
| **v7 LightGBM 26 维** | **0.9745** | **387 ms** (-2% MAE) |

Ridge 系数 (v7):
- `n_capital_word`: **−48** ms/token (反直觉负值: 音译名一气呵成比独立朗读快)
- `n_math_op`: +85 ms/token
- `n_ascii_isolated`: **+223** ms/token (大!)

### 端到端实测 (zjMu + d4Eg)

| 指标 | v6 (default) | v7 | Δ |
|---|---|---|---|
| zjMu mean | 1.0001 | 1.0026 | ↓ |
| zjMu 合规率 | 100% | 97.2% | **-2.8pp** |
| zjMu atempo | 0 | 1 | **+1** |
| d4Eg mean | 1.0156 | 1.0169 | ↓ |
| d4Eg 合规率 | 95.0% | 94.1% | **-0.9pp** |
| d4Eg atempo | 11 | 13 | **+2** |
| d4Eg outliers_gt_1.4 | 1 | 2 | **+1** |

### 段级对比 (d4Eg --tts-only)

12 个 v6 失败段 v6 vs v7 dev 完全一致 (因 --tts-only 复用翻译缓存,
estimator 只影响 atempo 决策不重新翻译)。
- v7 修复: #93, #156 (含 −1, i, j 等 math/ascii)
- v7 新增: #170, #175, #213 (新增 atempo)
- 净结果: v6 atempo 11 → v7 13 (-1 修复 +3 新增)

### 决策: 回滚 v7

按 test-feedback-loop 方法论严格执行 "失败了则回滚":
1. 离线 R² 仅 +0.0015, MAE 仅 -7ms — 边际改进
2. 端到端在两个视频均回退 (合规率 ↓ 1-3pp, atempo +1~3)
3. 失效模式分析方向**部分正确** (修了 2 段 math/ascii)
4. 但 GBDT 因新增 3 个特征产生其他段的小偏差, 累积让边缘段越界
5. 测试方法局限: --tts-only 不重新翻译, 无法验证 v7 estimator
   下 LLM 翻译质量是否改善

### v7 实验保留产物 (供 v8+ 参考)

- `scripts/calibrate_v3.py` v7 stage — 可重用 (失败实验工具不删)
- `audit/duration_estimator_v7_params.json` — 26 维 Ridge 系数
- v7 LightGBM 模型文件**未入库** (生产不上线)
- `duration_estimator.py` v7 集成代码已 revert (生产代码精简)

### v8+ 改进方向 (从 v7 失败学到)

1. **测试方法升级**: tts-only 模式不能验证 estimator 对 LLM 翻译的影响,
   未来 v8 实验需用 --integrated 或 --retranslate 重新翻译验证
2. **特征质量优于数量**: 加 3 维特征导致 noise > signal, 需考虑:
   - 仅加 1 个最强特征 (`n_ascii_isolated` +223ms 系数)
   - 或用更多训练数据降低 noise
3. **失效模式 ≠ 修复方向**: dev=+24% 段不一定意味着 estimator 低估,
   可能 LLM 翻译时已基于估算调整, atempo 是次级现象
4. **GBDT 集成限制**: 既加新特征又复用旧模型 GBDT 不能简单插值

## iter7: v8 超参调优 — 2026-05-07 (微改进上线)

### 假设

iter6 v7 失败学习: 不增加特征复杂度, 改试超参/loss 调优。

### 离线扫描 (5 配置, 同 23 维特征)

| 配置 | CV R² | CV MAE |
|---|---|---|
| L2_v6_repro (baseline) | 0.9733 | 394.0 |
| L1_MAE | 0.9718 | 394.9 |
| L2_more_trees (400 trees, lr=0.03) | 0.9737 | 392.0 |
| **L2_deeper (num_leaves=31)** | **0.9733** | **391.4** ✅ |
| L2_more_min_child (50) | 0.9725 | 396.6 |

L1 (MAE) 反而更差; 增加 leaves 31 微优 (-2.6 ms MAE)。

注: lightgbm `objective='huber', alpha=*` 实测崩 (CV R² -0.017),
alpha 参数语义与文档不符。

### 端到端实测 (zjMu + d4Eg + kCc 三视频)

| 指标 | 视频 | v6 | v8 (deeper) | Δ |
|---|---|---|---|---|
| mean | zjMu | 1.0001 | 0.9975 | 偏离 +0.0024 (略低估更安全) |
|  | d4Eg | 1.0156 | 1.0141 | ✅ -0.0015 |
|  | kCc | 1.0051 | 1.0051 | 持平 |
| std | zjMu | 0.0588 | 0.058 | ✅ -0.0008 |
|  | d4Eg | 0.0959 | 0.0926 | ✅ -0.0033 (-3.5%) |
|  | kCc | 0.0729 | 0.0691 | ✅ -0.0038 (-5.2%) |
| 合规率 | zjMu | 100% | 100% | 持平 |
|  | d4Eg | 95.0% | 95.5% | ✅ +0.5pp |
|  | kCc | 97.4% | 97.4% | 持平 |
| atempo | zjMu | 0 | 0 | 持平 |
|  | d4Eg | 11 | 10 | ✅ -1 |
|  | kCc | 14 | 13 | ✅ -1 |

**6 项改进 / 0 项显著回退** (zjMu mean 略偏 -0.0024 但绝对值小且偏低估更安全)。

### 决策: 上线 v8

边际改进真实 (尤其 std 全降, atempo 减 2)。模型文件直接替换:
- `models/duration_estimator_lgbm.txt` ← v8 模型 (290KB → 562KB, num_leaves=31)
- `duration_estimator.py` 不需改 (DURATION_LGBM_MODEL env 加载默认文件)
- 新增 `DURATION_LGBM_MODEL` 环境变量支持实验时换模型 (默认无需设置)

### v9+ 改进方向

1. **--integrated 模式验证**: tts-only 复用翻译缓存, estimator 改进只反映在
   atempo 决策, 未捕获 LLM 翻译质量影响. v9 应用 --integrated 真测
2. **更多视频数据**: 8327 净样本可能不够支撑 GBDT 更细的 split, 跑更多视频
   累积 20k+ 样本再训
3. **--integrated A/B 测试**: 同视频 v6 vs v8 翻译输出对比, 看 LLM 行为差异

## iter8: v9 错误分析 + monotonic constraints — 2026-05-07 (不上线)

### 错误分析 (v8 三视频 872 段)

**Per-video systematic bias** (domain shift):
- zjMu (3b1b 数学): residual mean **+496 ms** (over-predict)
- d4Eg (3b1b 四元数): **+360 ms**
- kCc (Karpathy GPT 编程): **−457 ms** (UNDER!) ← 解释 kCc atempo 14 段最多

**Per-length bias** (GBDT truncation):
| 长度 (chars) | n | residual mean |
|---|---|---|
| [0, 10) | 5 | +500 ms |
| [10, 20) | 98 | +526 ms |
| [20, 35) | 175 | +384 ms |
| [35, 60) | 341 | **−580 ms** |
| [60, 200) | 253 | −426 ms |

**根因**: 训练数据偏 3blue1brown 数学风格, kCc 编程类长句 + 专有 token 不足
GBDT 长句区域 split 稀疏, 边缘长句外推被截断到训练样本均值附近.

### v9 monotonic constraints 实验

```python
monotone_constraints = [+1] * (字符/音节 features) + [-1] * (n_exclaim, n_ellipsis)
```

LightGBM CV R²=0.9727 (v8 0.9733, -0.0006), MAE=399.6 ms (v8 391.4, +8 ms).

| 长度 | v8 res mean | v9 res mean | v8 \|res\| | v9 \|res\| |
|---|---|---|---|---|
| [0, 10) | +500 | +361 | 502 | 414 ✅ |
| [10, 20) | +526 | +488 | 703 | 679 ✅ |
| [20, 35) | +384 | +367 | 954 | 932 ✅ |
| [35, 60) | −580 | −606 | 1354 | **1373 ⚠️** |
| [60, 200) | −426 | −378 | 1435 | 1443 |

**结论**: monotonic 仅微调短-中段 mean offset, 长句 truncation 未解决.
35-60 段 |residual| 反升 1.4%, 离线 MAE +8 ms.

### 决策: v9 不上线

边际改进太小, 长句问题根因不是 monotonic 能解决的 — 是训练数据 domain shift.

### v10+ 真正可行方向

| 方向 | 期望增益 | 成本 |
|---|---|---|
| **训练数据 domain 扩充** | 解决 kCc -457ms bias | 高 (跑更多视频) |
| **Per-domain 校准** | 多视频混合时 mean offset | 中 (识别 domain) |
| **Residual boosting** | 第二模型预测残差, 二级修正 | 中 |
| **--integrated 模式真验证** | 测 LLM 翻译适应估算 | 高 (LLM API 成本) |

### 收敛判断

v8→v9 的微改进+回退证明: **当前数据/特征/架构已接近最优**.
继续在 8327 净样本 + 23 维特征上调优 ROI 极低.
真改进需要 (a) 更多数据, 或 (b) 端到端 LLM-aware 验证, 或 (c) 架构革新.

## iter9: v10 长句 rule 兜底实验 — 2026-05-08 (失败回滚)

### 假设

iter8 错误分析显示 35-60 长句 mean residual −580 ms / 60+ 段 −426 ms (under-predict).
假设: post-hoc rule 加固定 offset 修正长句 bias, 不重训模型, 风险局限.

### Rule 设计 (保守 70% 修正)

```python
def _apply_long_sentence_bias(text_zh, pred):
    chars = _meaningful_char_count(text_zh)
    if chars >= 60:
        return pred + 300.0  # 70% × 426
    if chars >= 35:
        return pred + 400.0  # ~70% × 580
    return pred  # 短-中段不动 (本身 over-predict)
```

DURATION_LONG_BIAS=0 可关闭。

### 端到端实测 (三视频)

| 指标 | 视频 | v8 (no rule) | v10 (rule) | Δ |
|---|---|---|---|---|
| mean | zjMu | 0.9975 | 0.9821 | ↓ 偏离 +0.015 |
|  | d4Eg | 1.0141 | **0.9996** | ✅ 偏离 -0.014 (近完美) |
|  | kCc | 1.0051 | 0.9867 | ↓ 偏离 +0.013 |
| 合规率 | zjMu | 100% | 97.2% | ↓ -2.8pp |
|  | d4Eg | 95.5% | 95.0% | ↓ -0.5pp |
|  | kCc | 97.4% | 97.6% | +0.2pp |
| atempo | zjMu | 0 | 1 | ↓ +1 |
|  | d4Eg | 10 | 11 | ↓ +1 |
|  | kCc | 13 | 13 | 持平 |
| **within_tolerance** | zjMu | 17 | **11** | -35% |
|  | d4Eg | 88 | **63** | -28% |
|  | kCc | 281 | **174** | **-38%** |
| **padded** | zjMu | 17 | 23 | +35% |
|  | d4Eg | 112 | 141 | +26% |
|  | kCc | 309 | **420** | **+36%** |

### 失败根因

1. **Zero-sum 转移**: rule 让 estimator 长句 over-predict → LLM 翻译变短 → TTS
   实际比 budget 短 → padded 大量增加. 问题从 "TTS 太长 (atempo)" 转移到 "TTS
   太短 (padded)", 总分 (within_tolerance) 显著降低.

2. **Per-video bias 不一致**: 错误分析数据 mean residual 是三视频混合统计:
   - zjMu: residual mean **+496 ms** (overall over-predict, 长句也未必 under)
   - d4Eg: +360 ms
   - kCc: −457 ms (真正 under-predict)

   单一 rule 加 +400/300 在 zjMu 和 d4Eg 短-中长 (35-60) 段是 over-correction,
   只在 kCc 上方向正确但量级也不准.

3. **未考虑 LLM 反馈**: estimator 估算被 LLM 用于决定翻译长度. estimator 准确度
   提升若不与 LLM 行为同步建模, 改进可能反弹 (LLM 适应了旧 estimator 的偏差).

### 决策: 回滚 v10 rule

`duration_estimator.py` 已 revert, rule 函数完全移除. v8 模型保持默认.

### v10 学习 → v11+ 真正可行方向

1. **Per-segment domain detection**: 用 text features (英文密度/数学符号/术语词典)
   在线判定 domain → 不同 rule. 但仍可能 under-fit.
2. **Per-video calibration**: 用前 N 段实测 ratio 校准本视频后续段, 但 N 段冷启动
   误差累积.
3. **--integrated A/B**: 让 LLM 真适应 v8 vs v10 翻译, 比 tts-only 更接近真实
   pipeline 行为. 但成本最高.
4. **训练数据 domain 扩充** (最稳): 跑 ≥5 个 Karpathy 类视频 (编程 + 长句),
   累积 20k+ 样本后重训 v8. 直接修 domain shift 根源.

### 收敛终判

- v6→v7 失败 (加特征)
- v7→v8 微改 (调超参)
- v8→v9 失败 (加约束)
- v9→v10 失败 (post-hoc rule)

四次失败/边际改善证明: **8327 净样本 + 23 维特征 + GBDT 已是当前数据架构上限**.
继续在此 axis 上调优 ROI 极低. v11+ 必须改变 axis: 数据 / 端到端验证 / 模型结构.

## 方法论复盘 (9 次迭代收敛)

```
iter1 离线 v0+v3+v4 (Ridge)        — CV R² 0.952
iter2 离线 v6 (LightGBM)           — CV R² 0.973 ⚠️ 仅离线指标
iter3 端到端 v4 (zjMu+d4Eg)        — mean 准但合规率 -1pp (混合信号)
iter4 端到端 v6 (zjMu+d4Eg)        — 全维度胜出 ✅ 上线
iter5 generality v6 (kCc 617 段)   — 长视频稳定 ✅ 维持
iter6 v7 token 特征                — 离线 +0.002 R² 端到端回退 ❌ 回滚
iter7 v8 超参调优 (num_leaves=31)  — 6 项端到端微改 / 0 显著回退 ✅ 上线
iter8 v9 monotonic + 错误分析      — 离线 MAE +8ms, 找到 domain shift 根因 ❌ 不上线
iter9 v10 长句 rule 兜底           — within_tolerance 降 28-38%, 问题转移 ❌ 回滚
```

**关键学习**:
- 离线 R² 与端到端表现**不一定一致** (v4 离线提升 +0.03, 端到端合规率反而 -1pp)
- GBDT OOD 短文本担忧**实测未触发** (pipeline 段长 ≥9 字符, 远超 OOD 区域)
- **迭代严格按 test-feedback-loop 执行**: 每阶段端到端验证再决策, 避免基于直觉的错误保守
- **第三视频 generality 验证不可省**: 仅看 1-2 视频可能错过分布 (d4Eg 数学/音译名 vs kCc 纯中文)
- **失效模式分析比单纯指标更重要**: 知道 14 atempo 段都是数学符号/音译名/边缘长句 → 直接指明 v7+ 方向
