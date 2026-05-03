# 语速估算修复 — 测试反馈循环记录

日期: 2026-05-01 → 2025-06-06 (持续迭代)
方法: test→feedback→fix→test 循环

## 迭代 1: 问题发现

### 测试命令
```bash
bash test_pipeline.sh --fast
```

### 发现的问题

**核心问题: TTS 时长估算严重低估英文/URL 内容**

实际 TTS 测量发现 17/73 段超过 1.25x 速度阈值（最大 2.04x），但迭代优化循环检测到 0 段超速。

根因分析:
1. `_estimate_duration_jieba` 使用 100ms/英文字符，但 edge-tts 读 URL 时逐字母朗读，实际约 280ms/字符
2. TTS 预检 (`_generate_tts_segments`) 的 `raw_ratio` 只计算中文字符数，完全忽略英文/URL
3. 整体韵律因子仅 1.1x（10%），实测 TTS 比字符级估算一致慢 30%

**典型案例:**
- #68 "屏幕与简介附 eater.net/slash-quaternions 链接": 估算 0.98x → 实际 2.04x
- #42 "取cos30°+sin30°·i，再与原复数相乘": 估算 0.70x → 实际 1.50x

### 修复内容

1. **`_estimate_duration_jieba` 增加 URL 检测** — 域名/路径按 280ms/字符估算（逐字母朗读）
2. **英文字符从 100ms 提高到 150ms** — 更接近 TTS 实际发音速度
3. **TTS 预检改用 `_estimate_duration_jieba`** — 替代原先只计数中文字符的简单公式
4. **韵律修正因子从 1.1 提高到 1.3** — 覆盖标点停顿、语句边界延长、特殊符号朗读
5. **URL 段跳过 LLM 精简** — 含 URL 的超速段无法通过缩短文字解决，跳过无效的 LLM 调用

## 迭代 2: 验证修复

### 结果对比

| 指标 | 修复前 | 修复后 | 改善 |
|------|--------|--------|------|
| 超速段 (>1.25x) | 17 | 7 | -59% |
| 最大 ratio | 2.04x | 1.44x | -0.60x |
| 中位数 ratio | 1.12x | 1.04x | 更接近 1.0 |
| 平均 ratio | 1.17x | 1.06x | -0.11 |
| 限速段 (hard clamp) | 5 | 0 | 消除 |
| 迭代优化检出超速 | 0 | 15 | 修复了检测盲区 |

### 翻译质量检查
- 重复翻译: 0
- 标注泄漏: 0
- 空翻译: 0
- 过短翻译 (<5字, target>3s): 0
- 上下文连贯性: 正常

### 剩余问题 (7段, 不可代码修复)

剩余 7 段 >1.25x (max 1.44x) 均为 **转录分句边界问题**:
- Whisper 在长句中间切分，导致英文跨段而中文翻译无法完整覆盖
- 这些段由 atempo 调速处理（非硬截断），听感可接受

## 迭代 3: Ridge 回归校准 (v1)

> 详见 `docs/research/2026-05-03-jieba-duration-calibration.md`

**方法**: 从 3 个视频收集 1587 样本，用 Ridge 回归 (alpha=0.1) 拟合 8 维特征权重 + 截距，替代手动硬编码参数。关键设计: rate 去混淆（反推 natural_ms = actual_ms × applied_rate）。

**结果**:
- R² = 0.84
- MAE: 558.9 → 468.2 ms (-16%)
- Phase 2 触发: 335/1587 → 245/1587 (-27%)
- 韵律乘数 `* 1.3` 被校准参数吸收，从代码中移除

## 迭代 4: Ridge v2 校准 + rate 去混淆 bug 修复

> 详见 `docs/research/2025-06-06-jieba-estimator-v2-exploration.md`

**改进**:
1. 修复 `calibrate_tts_duration.py` 中残留的 `* 1.3` 旧乘数（rate 去混淆 bug）
2. 训练数据从 3 视频 1587 样本扩展到 6 视频 3009 样本
3. 正则化 alpha 从 0.1 提高到 50（防止过拟合新视频）
4. 非线性特征探索（二次项/交互项/Huber 回归）→ CV 验证均过拟合，放弃

**当前校准参数** (`pipeline.py:_estimate_duration_jieba`):

| 特征 | 初始手动值 | v1 校准 | v2 校准 (当前) |
|------|-----------|---------|---------------|
| 单字词 | 200ms | 212ms | **138ms** |
| 双字词 | 380ms | 479ms | **361ms** |
| 三字词 | 530ms | 679ms | **506ms** |
| 四字+/字 | 150ms | 240ms | **223ms** |
| 英文字母 | 150ms | 116ms | **31ms** |
| 数字 | 120ms | 255ms | **311ms** |
| URL字符 | 280ms | 155ms | **16ms** |
| 标点停顿 | 50ms | 164ms | **197ms** |
| 截距 | 0ms | -63ms | **+1210ms** |
| 韵律乘数 | ×1.3 | 移除 | 移除 |

**R² = 0.92**, Phase 2 触发率 CV 最优 18.2%

## 迭代 5: speed_threshold 1.25→1.5 + 句子碎片标注

> 详见 `docs/research/2026-05-04-translation-quality-optimization.md`

**speed_threshold 调整**:
- 1.25 过于激进，导致 LLM 过度压缩译文为电报体
- 调整到 1.5，只精简真正超速的段，翻译质量显著恢复

**句子碎片标注** (`_group_sentence_fragments`):
- 检测 Whisper 跨句切分的碎片段（< 6 词 AND < 2 秒）
- 在 LLM batch prompt 中用 `{续}` 标注续接关系
- 仅影响翻译质量，不改变段数（annotation-only，不做 merge+split）
- info_ratio 改善 20-37%（最差视频）

## 当前状态

**`_estimate_duration_jieba`**: Ridge v2 校准参数 (6 视频 3009 样本, alpha=50, R²=0.92)，无额外韵律乘数。

**`_estimate_speed_ratios`**: 直接用 `_estimate_duration_jieba` 计算 ratio，阈值 1.5（default），0.7 以下为 underslow。注释: "校准后的 jieba 参数已含韵律/停顿修正，无需额外乘数"。

**`_generate_tts_segments` Phase 3 预检**: ratio 超出 (0.70, 1.35) 时 LLM 调整译文。URL 段仍跳过 LLM 精简。

**`speed_threshold`**: 1.5（DEFAULT_CONFIG、test_pipeline.sh、test_two_videos.sh 均已对齐）。

**句子碎片**: `_group_sentence_fragments` 保守合并 + `{续}` 标注，已在生产 prompt 中启用。

## 修改文件总览

| 文件 | 变更 |
|------|------|
| `pipeline.py:_estimate_duration_jieba` | 迭代 1→4: URL 检测→Ridge v1→v2 校准参数 (138/361/506/223/31/311/16/197/+1210) |
| `pipeline.py:_estimate_speed_ratios` | 移除额外韵律乘数，threshold 默认 1.5 |
| `pipeline.py:_generate_tts_segments` | Phase 3 预检用 jieba 估算，URL 段跳过 LLM |
| `pipeline.py:_group_sentence_fragments` | 句子碎片检测 + `{续}` 标注 |
| `pipeline.py` DEFAULT_CONFIG | speed_threshold: 1.25→1.5 |
| `calibrate_tts_duration.py` | Ridge 校准脚本，修复 rate 去混淆 bug，支持嵌套目录 |
| `test_pipeline.sh` | speed_threshold 1.5，API key 从 config.json 读取 |
| `test_two_videos.sh` | 10 视频，API key 从 config.json 读取，sherpa-onnx 支持 |
