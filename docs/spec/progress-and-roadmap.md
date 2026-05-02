# 项目进展与后续规划

> 最后更新: 2025-07

## 项目概述

youtube-cn-dub 是一套端到端的自动化工具链，将 YouTube 英文视频转换为带中文配音和中英双语字幕的视频。

**Pipeline 完整流程**:
```
YouTube 视频 → 下载 → 音频提取 → (可选)人声/伴奏分离
  → 语音识别 → (可选)NLP分句 → LLM主题识别 + 翻译
  → (可选)迭代优化 → 等时翻译 → TTS配音(原生rate控制)
  → rate反馈闭环 → LLM时长闭环 → 时间线对齐 → 字幕生成 → 合成视频
```

## 已完成的里程碑

### v0.1 — 基础管线 (2025-03)

- YouTube 视频下载 (yt-dlp)
- 音频提取 + faster-whisper 语音识别
- Google Translate / LLM 翻译（多引擎支持）
- edge-tts 中文配音
- ffmpeg 时间对齐 + 字幕生成 + 视频合成
- 断点续跑机制

### v0.2 — 翻译质量增强 (2025-03)

- LLM 主题识别 + 专业术语保护（数学符号、变量名等）
- 批间上下文传递（6句上下文 + 3句前瞻）
- 翻译幻觉防御（批内去重 + 缩小批次 + 毒化检测）
- 两步翻译法 (`llm.two_pass`)
- Markdown 格式标记多层清洗
- 翻译重试回退链: 批量LLM → 逐条LLM → Google → 保留原文

### v0.3 — TTS 引擎架构 (2025-03)

- 7 引擎可插拔架构: edge-tts / siliconflow / gtts / pyttsx3 / piper / sherpa-onnx / cosyvoice
- TTS 链式回退 (`tts_chain`)，整体回退保证语音一致性
- 0 字节 TTS 文件自动重试 + 静音占位兜底
- 短段合并 (`merge_short_segments`) 减少 TTS 失败
- TTSFatalError 区分可恢复/不可恢复错误

### v0.4 — 配音质量优化 (2025-03 ~ 2025-05)

- 语速三级平滑: trimmed mean → 自适应混合 → 双向指数平滑
- 语速钳制 [1.00, 1.25] + 片段边界淡入淡出
- 迭代优化: 超速段 LLM 精简 + 过短段 LLM 扩展
- TTS 后校准 (`post_tts_calibration`)
- 全局语速控制 (`global_speed`)
- EBU R128 响度标准化

### v0.5 — 音频分离 + 工程化 (2025-05)

- demucs 人声/伴奏分离 (子进程运行，避免 libiomp5 冲突)
- CPU 线程限制防系统崩溃
- NLP 智能分句 (spaCy)
- 下载画质可选 (`download_quality`)
- 结构化错误反馈 + 执行日志

### v0.6 — 审计系统 + 自动评分 (2025-06 ~ 2025-07)

- 审计目录重组 (`audit/`)
- 操作日志增强
- 自动化质量评分系统 (`score_videos.py`):
  - CPS (字符/秒) 合规率
  - atempo 调速分布
  - Speed Naturalness 语速自然度
  - UTMOS 音质评估 (框架预留)
  - 韵律评估 (框架预留)
- 回归检测 + 基线对比
- 61 项单元测试

### v0.7 — 消除 atempo 调速 (2025-07) ✅ 刚完成

**核心变更**: 用 TTS 原生速率控制替代 ffmpeg atempo 后处理，消除机械调速感。

实现内容:
- **Phase 1**: TTS 原生 rate 替代 atempo
  - rate 钳制区间扩大到 [0.80, 1.35]
  - `_tts_with_duration_feedback` 试发-反馈-重生闭环
  - `_align_tts_to_timeline` 重构，消除 atempo 调速
  - 容忍区间设计 (填充/溢出/借用/截断)
- **Phase 2**: 闭环等时翻译增强
  - `_llm_duration_feedback` 用实测 TTS 时长驱动 LLM 译文调整
  - alignment 配置节 (`atempo_disabled`, `feedback_loop` 等)
- **Phase 3**: 自动化验证器
  - `score_videos.py` 新增 Speed Naturalness 评分维度
  - `test_pipeline.sh` 验证增强
- **Phase 4**: 渐进迁移安全保障
  - 开关控制 (`atempo_disabled`)
  - 分级降级: gap borrowing → video slowdown → per-segment atempo(≤1.35x) → 截断

**三层架构**:
```
等时翻译 (长度控制) → TTS 原生 rate (精细调节) → 静音填充/截断 (无 atempo)
        ▲                    ▲                           │
        └──── 闭环反馈 ◀── TTS 实测时长 ◀───────────────┘
```

**关键指标变化**:
| 指标 | 旧 (atempo) | 新 (无 atempo) |
|------|------------|---------------|
| atempo_mean | ~1.08 | 1.00 |
| atempo_std | ~0.05 | 0.00 |
| 语速一致性 | 忽快忽慢 | 段间均匀 |
| 韵律自然度 | 机械拼接感 | TTS 原始韵律 |

## 当前代码规模

- `pipeline.py`: ~4000+ 行（主管线）
- `score_videos.py`: ~550 行（质量评分）
- `tests/`: 16+ 测试文件，259 项测试
- `devlog/`: 14 篇开发日志

## 后续规划

### 近期 (Short-term)

#### UTMOS 音质对比验证
- 安装 utmos 包，对 atempo vs 无 atempo 方案做 before/after 音质评估
- 框架已在 `score_videos.py` 中预留
- 优先级: 中 | 难度: 低

#### 字幕输出模式可选
- `subtitle_mode`: external / embedded / both
- 内嵌字幕用 ffmpeg `-vf subtitles=xxx.srt`
- 优先级: 中 | 难度: 低

#### 跳过步骤日志优化
- skip_steps 跳过的步骤打印简要说明
- 优先级: 低 | 难度: 低

### 中期 (Mid-term)

#### 性能监控报告
- 生成 `performance.json`: 各阶段耗时、并发利用率、失败重试次数
- 优先级: 中 | 难度: 中

#### 跨段语序优化
- 检测跨段内容 swap（第 N 段译文与第 N+1 段原文更匹配时自动交换）
- 优先级: 中 | 难度: 中

#### edge-tts rate 效果量化
- 系统性测试不同 rate 值对中文 TTS 的音质影响
- 确定最优安全区间（当前 [0.80, 1.35] 基于经验值）
- 优先级: 中 | 难度: 中

### 远期 (Long-term)

#### 代码模块化重构
- 将 pipeline.py 按功能拆分: config / download / transcribe / translate / refine / tts / align / subtitle / merge
- 保持主入口 API 向后兼容
- 优先级: 低 | 难度: 高

#### 多角色配音
- pyannote 说话人分轨 + 多音色 TTS
- 优先级: 低 | 难度: 高

#### Duration-conditioned TTS 集成
- 等 F5-TTS / CosyVoice 等支持目标时长参数后集成
- 可彻底消除 rate 调节需求
- 优先级: 低 | 难度: 高（依赖外部进展）

## 技术债务

| 项目 | 说明 | 优先级 |
|------|------|-------|
| pipeline.py 单文件过大 | ~4000+ 行，不利于迭代 | 中 |
| jieba 时长估算不准 | 已用实测闭环缓解，但底层估算仍粗糙 | 低 |
| `_build_atempo_filter` 残留 | 保留作为降级后备，可考虑提取到独立模块 | 低 |
| 测试覆盖率不均 | 核心对齐逻辑的单元测试较少 | 中 |

## 配置参考

当前推荐的 alignment 配置（`config.json`）:

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
