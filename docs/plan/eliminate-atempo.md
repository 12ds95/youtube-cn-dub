# 消除 atempo 调速 — TTS 原生速率 + 闭环等时翻译 + 自动化验证器

## 问题本质

当前 pipeline 用 ffmpeg `atempo`（WSOLA 信号处理）强行拉伸/压缩 TTS 音频对齐时间轴。atempo 不理解语言——在音素中间拉长、在停顿处压缩，导致：
- 语速忽快忽慢、断句生硬割裂
- 刻意拖音或压缩语速
- 节奏违和、韵律混乱、充满机械拼接的怪异感

**理想目标**：TTS 合成出来的原始音频（不做任何调速）就自然地匹配时间窗口。

## 研究结论

| 策略 | 原理 | 代表 | 本项目适用性 |
|------|------|------|-------------|
| TTS 原生 rate 控制 | edge-tts `Communicate(rate="+N%")` 在服务器端重新合成 | Softcatala open-dubbing | ✅ 已有基础，但仅用于弱调节 |
| 等时翻译（shrink/expand） | 控制译文长度使 TTS 自然匹配时长 | IsometricMT、IWSLT 2025 | ✅ 已有(isometric=3)，但精度不够 |
| Duration-conditioned TTS | TTS 模型接受目标时长参数 | F5-TTS fork、DubWise | ❌ edge-tts 不支持 |
| 后处理 atempo | ffmpeg 信号级拉伸 | 当前实现 | ❌ 正是要消除的 |

**核心发现**：edge-tts 的 `rate` 参数是**服务器端神经网络重合成**（缩短停顿、压缩非重读音节），比 atempo 自然得多。范围 -50% 到 +100%，在 ±20% 内几乎无感。

## 方案架构：三层递进消除 atempo

```
翻译阶段          TTS 生成阶段           时间线对齐阶段
┌──────────┐     ┌──────────────┐      ┌──────────────┐
│ 等时翻译  │────▶│ TTS 原生 rate │────▶│ 静音填充/截断 │
│ (长度控制) │     │ (精细调节)    │      │ (无 atempo!) │
└──────────┘     └──────────────┘      └──────────────┘
     ▲                  ▲                      │
     │                  │                      │
     └──── 闭环反馈 ◀──TTS 实测时长 ◀──────────┘
```

**第一层：等时翻译**（已有） → 让 CPS 尽量落在 [3.5-6.0]

**第二层：TTS 原生 rate** → 对偏离的段，用 edge-tts `rate` 参数补偿（替代 atempo）

**第三层：兜底处理** → 对 rate 仍无法覆盖的极端段（需要 >20% 调整），使用截断/静音填充（不用 atempo）

## 关键文件

- `pipeline.py` — 核心变更（TTS 生成 + 时间线对齐）
- `score_videos.py` — 新增验证指标
- `config.example.json` — 新增 alignment 配置节

## 实现步骤

### Phase 1: TTS 原生速率替代 atempo（核心变更） ✅ 已完成

#### 步骤 1: 重构 `_generate_tts_segments` — 精准 rate 计算 ✅

将 rate 钳制区间从 `[0.85, 1.20]` 扩大到 `[0.80, 1.35]`（edge-tts 安全范围内），TTS 在生成时就能补偿更大的时长偏差。

#### 步骤 2: "试发-反馈-重生" 闭环 — `_tts_with_duration_feedback` ✅

新函数，对 rate 偏差大的段（|rate - 1.0| > 0.20）：
1. 先以估算 rate 生成 TTS
2. 测量实际时长
3. 如果实际时长偏离目标 > 15%: 计算精确的 `corrected_rate = actual_dur / target_dur`，重新生成
4. 记录最终 speed_ratio

#### 步骤 3: 重构 `_align_tts_to_timeline` — 消除 atempo ✅

核心变更：**删除 atempo 调速流程**，替换为直接 overlay：
- raw_ratio < 1.0 (TTS 比目标短): 居中放置，静音填充
- raw_ratio 1.0~1.15: 轻微超时允许（不截断，或借用间隙）
- raw_ratio > 1.15: 截断到 target_dur + 借用间隙

#### 步骤 4: 容忍区间设计 ✅

| TTS 时长 vs 目标 | 处理方式 | 用户感知 |
|-----------------|---------|---------|
| < 目标时长 (TTS 偏短) | 居中填充静音 | 自然——像说话后的停顿 |
| 100%-110% 目标 | 不截断，允许轻微溢出 | 几乎不可察觉 |
| 110%-115% 目标 | 尝试 Gap Borrowing | 自然 |
| >115% 目标 | 截断到目标+借用 | 可能丢尾，但不失真 |

### Phase 2: 闭环等时翻译增强 ✅ 已完成

#### 步骤 5: LLM 时长闭环反馈 — `_llm_duration_feedback` ✅

在闭环 TTS 后，对仍偏差 > 20% 的段，用**实测 TTS 时长**（非 jieba 估算）给 LLM 精确的字数目标：
```
"当前翻译 '{text_zh}' 合成语音时长为 {actual_ms}ms，目标为 {target_ms}ms。
请调整翻译使其更{长|短}约 {delta_pct}%，同时保持含义准确。"
```
这比字符数估算精确得多（AppTek 论文证明此法可达 90%+ 合规率）。

#### 步骤 6: 配置项 ✅

```json
{
  "alignment": {
    "atempo_disabled": true,
    "tts_rate_range": [0.80, 1.35],
    "overflow_tolerance": 0.10,
    "feedback_loop": true,
    "feedback_tolerance": 0.15,
    "gap_borrowing": false,
    "max_borrow_ms": 300,
    "video_slowdown": false,
    "max_slowdown_factor": 0.85
  }
}
```

### Phase 3: 自动化验证器 (Verifier) ✅ 已完成

#### 步骤 7: `score_videos.py` — "Speed Naturalness" 评分维度 ✅

指标：
- `no_atempo_compliance_pct`: 无需调速就匹配时间窗的段占比
- `rate_variance`: TTS 生成 rate 参数的方差
- `overflow_pct`: 超时被截断的段占比
- `mean_raw_ratio` / `raw_ratio_std`: 原始时长比均值和标准差
- `atempo_fallback`: per-segment atempo 降级次数

#### 步骤 8: UTMOS 对比测试（可选，待实现）

在测试流程中加入 before/after 对比，需安装 `utmos` 包。框架已在 `score_videos.py` 中预留。

#### 步骤 9: `test_pipeline.sh` 验证增强 ✅

集成测试脚本已支持验证 speed_report.json 中的各项指标。

### Phase 4: 渐进迁移（安全保障） ✅ 已完成

#### 步骤 10: 开关控制 + 分级降级策略 ✅

- `alignment.atempo_disabled = true` → 新方案（默认）
- `alignment.atempo_disabled = false` → 旧方案（降级回退）
- Per-segment atempo 降级: 当所有软策略（gap borrowing、video slowdown）都无法解决时，对单段尝试 ≤1.35x 的 atempo（保留内容），仍不行才截断
- 降级链: gap borrowing → video slowdown → per-segment atempo(≤1.35x) → 截断（最后手段）

## 预期效果

| 指标 | 当前(atempo) | 目标(无atempo) | 改善 |
|------|-------------|---------------|------|
| atempo_mean | ~1.08 | 1.00 (不调速) | 消除 |
| atempo_std | ~0.05 | 0.00 (不调速) | 消除 |
| 语速一致性 | 忽快忽慢 | 段间均匀 | 根本性改善 |
| 韵律自然度 | 机械拼接感 | TTS 原始韵律 | 根本性改善 |
| CPS 合规率 | ~62% | ≥62% (不恶化) | 等时翻译保底 |
| 超时截断率 | ~5% | <10% | 可接受 |

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| TTS rate 扩大后部分段语速太快 | 钳制上限 1.35，超过则截断 |
| 闭环增加 TTS API 调用 | 仅对偏差 >20% 的段启用（约 20-30%） |
| 截断丢信息 | Gap Borrowing + 等时翻译 + per-segment atempo 降级三重保障 |
| edge-tts rate 对中文效果未知 | 现有代码已使用，范围安全 |
| 回退复杂度 | 开关控制，一个配置项切回旧方案 |
