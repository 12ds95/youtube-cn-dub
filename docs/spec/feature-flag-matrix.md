# 功能开关矩阵 (Feature Flag Matrix)

各脚本/模式下的功能开关与配置参数对照。所有"集成"模式以
`bash test_pipeline.sh --integrated` 为参考基线 (sentence-unit pipeline 时代)。

> 更新于 2026-05-06。配置改动须同步更新本表。

## 关键 LLM / 翻译开关

| 开关 | test_pipeline.sh `--integrated` | test_pipeline.sh `--retranslate` | test_pipeline.sh `--baseline` | test_pipeline.sh `--fast` | test_two_videos.sh | batch_process.py `--integrated`/`--retranslate` |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| `llm.two_pass` | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| `llm.isometric` | 0 | 0 | 0 | 0 | 0 | 0 |
| `nlp_segmentation` | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |

## 时间线对齐 (alignment)

| 开关 | `--integrated` | `--retranslate` | `--baseline` | `--fast` | test_two_videos | batch_process |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| `gap_borrowing` | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| `max_borrow_ms` | 300 | 300 | — | 300 | 300 | 300 |
| `video_slowdown` | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| `max_slowdown_factor` | 0.85 | 0.85 | — | 0.85 | 0.85 | 0.85 |
| `atempo_disabled` | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| `feedback_loop` | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |

> 注: `feedback_tolerance` 之前在 batch_process.py 中默认 0.15, 已移除以与
> test_pipeline.sh 对齐 (pipeline.py 内部已有该值的代码默认)。

## refine / post calibration

| 开关 | `--integrated` | `--retranslate` | `--baseline` | `--fast` | `--refine` | test_two_videos | batch_process |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| `refine.enabled` | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `refine.max_iterations` | 3 | 3 | 3 | 3 | 3 | 3 | 3 |
| `refine.speed_threshold` | 1.5 | 1.5 | 1.5 | 1.5 | 1.5 | 1.5 | 1.5 |
| `refine.post_tts_calibration` | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `refine.calibration_threshold` | 1.30 | 1.30 | 1.30 | 1.30 | 1.30 | 1.30 | 1.30 |

> sentence-unit pipeline + pre_tts 守卫已替代 refine/post_calibration 的职能, 故全部默认关闭。

## skip_steps (流程跳过)

| 模式 | skip_steps | 用途 |
|---|---|---|
| `--full` | `["download", "extract", "separate"]` | 跑 transcribe + 后续完整流程 |
| `--baseline` | `["download", "extract", "separate"]` | 全功能关闭, 验证不引入回归 |
| `--integrated` | `+ "transcribe"` | 用 transcribe 缓存, 重新翻译 + TTS + 字幕 + 合成 |
| `--fast` | `+ "transcribe", "translate"` | 用全部缓存, 仅跑 TTS + 字幕 + 合成 |
| `--tts-only` | `+ "transcribe", "translate", "subtitle", "merge"` | 仅跑 TTS + 预检 |
| `--refine` | `+ "transcribe", "translate", "tts", "subtitle", "merge"` | 仅跑迭代优化 |
| `--retranslate` | `+ "transcribe", "tts", "subtitle", "merge"` | 仅重新翻译, 不跑 TTS 后步骤 |
| test_two_videos.sh | `+ "transcribe?", "subtitle", "merge"` | 跑到 TTS 收集校准数据 (跳过 transcribe 取决于是否有 cache) |
| batch_process `--integrated` | `["download", "extract", "separate", "transcribe"]` | 与 test_pipeline `--integrated` 对齐 |

## 音频分离

| 开关 | 所有非 baseline 模式 | baseline |
|---|:-:|:-:|
| `audio_separation.enabled` | ✅ | ✅ |
| `model` | htdemucs | htdemucs |
| `vocal_volume` | 0.15 | 0.15 |
| `bgm_volume` | 1.0 | 1.0 |
| `device` | auto | auto |

## 命令对照速查

```bash
# 单视频快速验证 (全功能 + transcribe 用 cache)
bash test_pipeline.sh --integrated

# 单视频跑完整流程 (含 transcribe)
bash test_pipeline.sh --full

# 多视频集成测试 (output/ 下所有视频)
bash test_two_videos.sh

# 批量重新翻译 (基于视频列表 JSON)
cd ~/Desktop/youtube-cn-dub-batch
python3 batch_process.py --integrated --dry-run        # 预览
python3 batch_process.py --integrated                   # 执行
python3 batch_process.py --integrated --topic "Calculus" # 单 topic
```

`--integrated` 在 batch_process.py 中是 `--retranslate` 的别名 (语义相同, 名字更清晰)。
