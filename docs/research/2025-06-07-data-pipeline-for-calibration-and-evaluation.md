# 数据采集与产出路径：jieba 校准 + 翻译质量评估

**用途**: `test_two_videos.sh` 10 视频全管线运行产出的数据，同时服务两个 research 任务的迭代需求。

## 数据产出总览

```
test_two_videos.sh (10 视频)
  │
  ├── 每个视频 output/<id>/ 产出:
  │     ├── segments_cache.json        ← 翻译质量评估 + jieba 校准
  │     ├── tts_segments/seg_*.mp3     ← jieba 校准 (actual_ms)
  │     ├── audit/tts_feedback_log.json ← jieba 校准 (corrected_rate)
  │     ├── audit/speed_report.json    ← 两者都用 (ratio 分布)
  │     ├── audit/quality_scores.json  ← 翻译质量 (CPS/Atempo/UTMOS)
  │     ├── subtitle_*.srt             ← 人工审听参考
  │     └── final.mp4                  ← 端到端验证
  │
  ├── 汇总脚本:
  │     ├── calibrate_tts_duration.py  → calibration_result.json + pipeline.py 参数写入
  │     ├── test_translate_only.py     → GT 对比评估 (info_ratio/jieba_ratio)
  │     └── score_videos.py            → 质量评分门禁 (CPS/Atempo/UTMOS/Jitter)
  │
  └── 产出文档:
        ├── docs/research/2026-05-03-jieba-duration-calibration.md  (v1, 已被 v2 取代)
        ├── docs/research/2025-06-06-jieba-estimator-v2-exploration.md  (v2, 当前)
        └── docs/research/2026-05-04-translation-quality-optimization.md
```

## 任务 1: jieba 时长校准

### 数据需求

| 字段 | 来源文件 | 用途 |
|------|---------|------|
| `text_zh` | `segments_cache.json` → 每段 `.text_zh` | 8 维特征提取 (jieba 分词) |
| `start`, `end` | `segments_cache.json` → 每段时间戳 | target_ms 计算 |
| `actual_ms` | `tts_segments/seg_*.mp3` (pydub 读取时长) | 回归目标 (经 rate 去混淆) |
| `corrected_rate` | `audit/tts_feedback_log.json` | rate 去混淆: natural_ms = actual_ms * rate |

### 运行命令

```bash
# 全量校准 (扫描 output/*/ 和 output/*/*/)
./venv/bin/python3 calibrate_tts_duration.py

# 校准 + 自动写入 pipeline.py
./venv/bin/python3 calibrate_tts_duration.py --apply
```

### 产出

| 文件 | 内容 |
|------|------|
| `calibration_result.json` | 校准参数、R²、MAE、MAPE、Phase 2 触发对比 |
| `pipeline.py:_estimate_duration_jieba` | 自动替换 8 个特征权重 + 截距 (--apply) |

### 当前状态

v2 校准: 6 视频 3009 样本, alpha=50, R²=0.92。`test_two_videos.sh` 跑完剩余 4 个视频后可扩大到 10 视频重新校准。

## 任务 2: 翻译质量评估

### 数据需求

| 字段 | 来源 | 用途 |
|------|------|------|
| `text_zh` | `segments_cache.json` → 系统翻译 | 评估对象 |
| `text` / `text_en` | `segments_cache.json` → 英文原文 | 参考 |
| GT 中文字幕 | YouTube 社区字幕 (手动下载 `.srt`) | Ground Truth |
| `start`, `end` | `segments_cache.json` | 时间对齐 + jieba_ratio 计算 |

### 运行命令

```bash
# 对单个视频 GT 对比
./venv/bin/python3 test_translate_only.py output/<id>/ --compare-only

# 全量对比 (需要各视频 GT 字幕)
./venv/bin/python3 test_translate_only.py --compare-only --all
```

### 评估指标

| 指标 | 含义 | 理想值 |
|------|------|--------|
| `info_ratio` | 逐段 vs GT 信息覆盖率 | > 0.6 |
| `win_info` | 3 段滑窗信息覆盖率 | > 0.8 |
| `jieba_ratio` | jieba 估算时长 / 目标时长 | 0.8-1.2 |
| `ellipsis` | 省略号段数 (翻译过度省略) | < 5% |
| `telegram` | 电报体段数 (过度压缩) | < 5% |
| `dup` | 重复翻译段数 | < 2% |

### 产出

评估结果输出到终端。关键数据记录到 `docs/research/2026-05-04-translation-quality-optimization.md`。

## 两任务的数据共享关系

```
test_two_videos.sh
       │
       ▼
  segments_cache.json ───┬──→ calibrate_tts_duration.py (特征提取 + target_ms)
       │                 └──→ test_translate_only.py (GT 对比)
       │
  tts_segments/*.mp3 ───────→ calibrate_tts_duration.py (actual_ms)
       │
  audit/tts_feedback_log.json → calibrate_tts_duration.py (rate 去混淆)
       │
  score_videos.py ──────────→ CPS/UTMOS 门禁 (两任务都参考)
```

**关键约束**: `calibrate_tts_duration.py` 的 rate 去混淆依赖 `tts_feedback_log.json`，只有完整跑过 Phase 2 反馈闭环的视频才有此文件。跳过 TTS 步骤的模式 (--refine, --retranslate) 不产出校准数据。

## 10 视频覆盖状态

| 视频 ID | 领域 | 完整产出 | 校准可用 | GT 可用 |
|---------|------|---------|---------|---------|
| d4EgbgTm0Bg | — | 部分 (无 final.mp4) | 待验证 | ? |
| kCc8FmEb1nY | — | 早期 (无 TTS) | 否 | ? |
| zjMuIxRvygQ | — | 完整 | 是 | 是 |
| Calculus/WUvTyaaNkzM | 微积分 | 待确认 | 待确认 | ? |
| CS/03_bitcoin | 计算机 | 待确认 | 待确认 | 是 |
| CS/05_epidemic | 计算机 | 待确认 | 待确认 | 是 |
| DE/01_unsolvable | 微分方程 | 待确认 | 待确认 | ? |
| NN/aircAruvnKk | 神经网络 | 待确认 | 待确认 | ? |
| Analysis/02_fourier | 分析 | 待确认 | 待确认 | ? |
| Prob/06_CLT | 概率 | 待确认 | 待确认 | 是 |

**下一步**: 运行 `bash test_two_videos.sh` 补齐所有视频的完整产出，然后:
1. `calibrate_tts_duration.py --apply` → v3 校准 (10 视频)
2. `test_translate_only.py --compare-only --all` → 全量翻译质量基线
