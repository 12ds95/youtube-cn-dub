# YouTube 英文视频中文配音工具

一套端到端的自动化工具链，将 YouTube 英文视频转换为带中文配音和中英双语字幕的视频。所有工具均为免费开源，全部在本地运行。

## 工作流程

```
YouTube 视频 → yt-dlp 下载 → faster-whisper 语音识别 → LLM/Google 翻译
    → edge-tts 中文配音 → ffmpeg 时间对齐与合成 → 最终视频
```

核心特性：

- **多翻译引擎**：Google Translate（免费）或 LLM 大模型（DeepSeek、Qwen、OpenAI 等 OpenAI 兼容 API）
- **迭代优化**：自动检测配音语速过快的片段，调用 LLM 精简翻译并重新生成，循环直到语速自然
- **断点续跑**：每个步骤的中间结果均有缓存，支持从任意阶段恢复
- **批量高效**：TTS 并发生成、LLM 批量翻译

## 环境要求

- Python 3.9+
- ffmpeg ≥ 4.x（`brew install ffmpeg`）
- Node.js（yt-dlp 部分场景需要）

## 快速开始

```bash
# 1. 克隆并安装
git clone <repo-url> && cd youtube-cn-dub
bash setup.sh

# 2. 复制并编辑配置（可选，使用 LLM 翻译时需要）
cp config.example.json config.json
# 编辑 config.json，填入 LLM API Key 等信息

# 3. 运行
bash run.sh "https://www.youtube.com/watch?v=XXXX"

# 或使用配置文件
bash run.sh --config config.json
```

## 使用方式

### 基础用法

```bash
# Google 翻译（免费，无需配置 API Key）
bash run.sh "https://www.youtube.com/watch?v=XXXX"

# LLM 翻译（质量更好）
bash run.sh "https://www.youtube.com/watch?v=XXXX" --translator llm --llm-api-key sk-xxx

# 使用配置文件（推荐，避免命令行暴露 API Key）
bash run.sh --config config.json
```

### 常用参数

```bash
# 选择中文语音（仅影响 edge-tts 引擎）
--voice zh-CN-YunxiNeural        # 男声（默认）
--voice zh-CN-XiaoxiaoNeural     # 女声
--voice zh-CN-YunyangNeural      # 男声（播报风格）

# Whisper 模型（精度 vs 速度）
--whisper-model tiny              # 最快，精度一般（~75MB）
--whisper-model small             # 推荐（默认，~500MB）
--whisper-model medium            # 最精确，较慢（~1.5GB）

# 原声背景音量（0.0=静音，1.0=原始）
--volume 0.2

# 处理完成后重命名输出目录
--rename "线性代数精讲"
```

## 配置文件详解

所有配置项都可以在 `config.json` 中设置。复制 `config.example.json` 为 `config.json` 后按需修改，所有字段均为可选，未设置的使用默认值。配置优先级：命令行参数 > config.json > 默认值。

### 基础参数

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `url` | string | null | YouTube 视频 URL（与 `resume_from` 二选一） |
| `output` | string | `"output"` | 输出根目录，每个视频存入 `output/<video_id>/` |
| `voice` | string | `"zh-CN-YunxiNeural"` | edge-tts 语音（仅影响 edge-tts 引擎，其他引擎有各自配置） |
| `whisper_model` | string | `"small"` | Whisper 模型：`tiny` / `small` / `medium` |
| `volume` | float | `0.15` | 原声背景音量混入比例：0.0=静音，1.0=原始音量 |
| `browser` | string | `"chrome"` | yt-dlp 读取 cookies 的浏览器：chrome / firefox / edge / safari |
| `rename` | string | null | 处理完成后重命名输出目录 |
| `resume_from` | string | null | 从已有输出目录断点续跑（如 `"output/f09d1957a98"`） |

### LLM 翻译配置

支持所有 OpenAI 兼容 API（DeepSeek、Qwen、Moonshot、GPT 等）。当 `translator` 设为 `"llm"` 或 `refine.enabled` 为 true 时需要配置。LLM 翻译失败会自动降级为 Google Translate（回退链：LLM 批量 → LLM 逐条重试 → Google → 保留原文）。

```json
{
  "translator": "llm",
  "llm": {
    "api_url": "https://api.deepseek.com/v1",
    "api_key": "sk-your-key-here",
    "model": "deepseek-chat",
    "batch_size": 15,
    "temperature": 0.3,
    "style": ""
  }
}
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `llm.api_url` | string | `"https://api.deepseek.com/v1"` | API 端点 URL |
| `llm.api_key` | string | `""` | API 密钥（也可用 `--llm-api-key` 命令行传入） |
| `llm.model` | string | `"deepseek-chat"` | 模型名称 |
| `llm.system_prompt` | string | *(内置翻译 prompt)* | 翻译 system prompt（一般无需修改） |
| `llm.batch_size` | int | `15` | 每批翻译的句子数（过大可能导致对齐问题） |
| `llm.temperature` | float | `0.3` | 生成温度：0.0=确定性，1.0=多样性 |
| `llm.style` | string | `""` | 翻译风格：`""` / `"口语化"` / `"正式"` / `"学术"` |

### TTS 配音引擎

支持 7 个 TTS 引擎，通过 `tts_chain` 定义引擎优先级链。单引擎失败时整体回退到下一个引擎重新生成全部片段，保证语音一致性。远程引擎自动阶梯降并发重试（正常→半→1，连续 3 轮无改善才放弃），本地引擎失败即放弃。切换引擎前自动备份到 `tts_backup_{engine}/`，支持从 `tts_failure.json` 断点恢复。

| 引擎 | 类型 | 中文质量 | 需要 | 说明 |
|------|------|---------|------|------|
| `edge-tts` | 远程免费 | 优秀 | 网络 | 默认引擎，微软免费 API，6+ 中文音色 |
| `siliconflow` | 远程免费 | 最佳 | API Key | 硅基流动 CosyVoice2，注册送额度 |
| `gtts` | 远程免费 | 良好 | 网络 | Google Translate TTS，单音色 |
| `pyttsx3` | 本地离线 | 一般 | 无 | 系统自带 TTS，零依赖终极兜底 |
| `piper` | 本地离线 | 良好 | 下载模型(~70MB) | ONNX 推理，CPU 友好 |
| `sherpa-onnx` | 本地离线 | 良好 | 下载模型(~110MB) | MeloTTS 中英混合模型，CPU 友好 |
| `cosyvoice` | 本地部署 | 最佳 | GPU | 阿里开源，支持声音克隆 |

每个引擎有独立的语音配置（通过 `resolve_voice()` 方法），`voice` 字段仅影响 edge-tts，其他引擎会忽略它并使用各自的配置。

**`tts_chain` 推荐配置：**

```bash
# 纯在线（最省事，默认推荐）
"tts_chain": ["edge-tts", "gtts", "pyttsx3"]

# 在线优先 + 离线兜底
"tts_chain": ["edge-tts", "gtts", "piper", "pyttsx3"]

# 最佳音质优先（需 SiliconFlow API Key）
"tts_chain": ["siliconflow", "edge-tts", "pyttsx3"]

# 纯离线（完全无需网络，需先下载模型）
"tts_chain": ["piper", "sherpa-onnx", "pyttsx3"]
```

**edge-tts 可选中文语音**（`voice` 字段，仅影响 edge-tts）：

| 语音 ID | 性别 | 风格 |
|---------|------|------|
| `zh-CN-YunxiNeural` | 男 | 自然流畅（默认） |
| `zh-CN-YunjianNeural` | 男 | 硬朗，适合新闻/纪录片 |
| `zh-CN-YunyangNeural` | 男 | 播音腔 |
| `zh-CN-XiaoxiaoNeural` | 女 | 温柔自然 |
| `zh-CN-XiaoyiNeural` | 女 | 活泼 |
| `zh-CN-YunxiaNeural` | 男 | 偏年轻 |

**各引擎专属配置：**

使用 SiliconFlow CosyVoice2（注册 https://cloud.siliconflow.cn 获取免费 API Key）：

```json
{
  "tts_chain": ["siliconflow", "edge-tts", "pyttsx3"],
  "siliconflow": {
    "api_key": "sk-xxx",
    "model": "FunAudioLLM/CosyVoice2-0.5B",
    "voice": "FunAudioLLM/CosyVoice2-0.5B:alex"
  }
}
```

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `siliconflow.api_key` | `""` | 硅基流动 API Key |
| `siliconflow.model` | `"FunAudioLLM/CosyVoice2-0.5B"` | 模型 ID |
| `siliconflow.voice` | `"...CosyVoice2-0.5B:alex"` | 音色：alex / benjamin / charles / cosmo |

使用本地离线引擎（需先下载模型）：

```json
{
  "tts_chain": ["piper", "sherpa-onnx", "pyttsx3"],
  "piper": { "model_path": "models/piper/zh_CN-huayan-medium.onnx" },
  "sherpa_onnx": {
    "model": "models/sherpa-onnx/vits-melo-tts-zh_en/model.onnx",
    "lexicon": "models/sherpa-onnx/vits-melo-tts-zh_en/lexicon.txt",
    "tokens": "models/sherpa-onnx/vits-melo-tts-zh_en/tokens.txt"
  }
}
```

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `piper.model_path` | null | Piper 模型路径（`bash download_model.sh piper` 下载） |
| `sherpa_onnx.model` | `""` | sherpa-onnx 模型文件路径 |
| `sherpa_onnx.lexicon` | `""` | 词典文件路径 |
| `sherpa_onnx.tokens` | `""` | tokens 文件路径 |
| `sherpa_onnx.dict_dir` | `""` | 词典目录（可选） |
| `sherpa_onnx.speaker_id` | `0` | 说话人 ID（多人模型时选择） |
| `cosyvoice.model_path` | null | CosyVoice 模型路径（需 GPU） |

`pyttsx3` 离线兜底需 macOS 中文语音包（系统设置 → 辅助功能 → 朗读内容 → 管理声音 → 下载 Ting-Ting）：

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `pyttsx3.voice_name` | null | 系统语音名：`"Ting-Ting"` / `"Mei-Jia"` / null=自动查找中文 |
| `pyttsx3.rate` | `180` | 语速 (words per minute) |

### 性能选项

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `tts_concurrency` | int | `5` | TTS 并发数（远程引擎失败时自动阶梯降并发） |
| `whisper_beam_size` | int | `5` | Whisper beam search 大小（越大越精确但越慢） |
| `skip_steps` | list | `[]` | 跳过指定步骤：`download` / `transcribe` / `translate` / `subtitle` / `tts` / `merge` |

### 迭代优化

当中文翻译比英文原文长时，TTS 配音需要加速播放以匹配时间线。迭代优化功能会自动检测加速过大的片段，调用 LLM 精简翻译后重新生成。需要配置 `llm.api_key`。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `refine.enabled` | bool | `false` | 是否启用迭代优化 |
| `refine.max_iterations` | int | `5` | 单次运行最大迭代轮次（收敛后 early stop） |
| `refine.speed_threshold` | float | `1.25` | 加速倍率阈值：>1.25x 即触发精简（1.0=原速，1.5x 已很明显） |
| `refine.resume_iteration` | int | null | 从第 N 轮迭代恢复（大循环断点续跑） |
| `clean_iterations` | bool | `false` | 清理 `iterations/` 目录后重新优化 |

```bash
# LLM 翻译 + 3 轮迭代优化
bash run.sh "URL" --translator llm --llm-api-key sk-xxx --refine 3

# 自定义加速阈值（默认 1.25x）
bash run.sh "URL" --refine 5 --refine-threshold 1.2
```

迭代分为两层：小循环（自动）在一次运行中反复精简直到所有片段低于阈值；大循环（人工）是运行完成后人工审听，不满意则用 `--resume-iteration` 继续优化。

```bash
# 从第 2 轮迭代恢复
bash run.sh --resume-from output/VIDEO_ID --refine 5 --resume-iteration 2

# 清理迭代数据，从头优化
bash run.sh --resume-from output/VIDEO_ID --clean-iterations --refine 3
```

### 断点续跑

```bash
# 从已有输出目录恢复（自动跳过已完成步骤）
bash run.sh --resume-from output/VIDEO_ID

# 手动编辑翻译后重新生成配音和视频
#   1. 编辑 output/VIDEO_ID/segments_cache.json 中的 text_zh 字段
#   2. 删除旧的 TTS 缓存和字幕
rm -rf output/VIDEO_ID/tts_segments output/VIDEO_ID/subtitle_*.srt
#   3. 重新运行
bash run.sh --resume-from output/VIDEO_ID
```

## 输出文件

每个视频的输出在 `output/<video_id>/` 目录下：

| 文件 | 说明 |
|------|------|
| `final.mp4` | 最终视频（中文配音 + 原声背景） |
| `subtitle_bilingual.srt` | 中英双语字幕 |
| `subtitle_zh.srt` / `subtitle_en.srt` | 单语字幕 |
| `segments_cache.json` | 转录+翻译缓存（可手动编辑微调） |
| `iterations/` | 迭代优化快照（`--refine` 时生成） |

播放时用 VLC/IINA 等播放器打开 `final.mp4`，加载 `subtitle_bilingual.srt` 即可。

## 项目结构

```
youtube-cn-dub/
├── pipeline.py            # 主程序
├── run.sh                 # 一键启动脚本
├── setup.sh               # 环境部署脚本
├── test.sh                # 测试入口（smoke / unit / all）
├── download_model.sh      # 模型下载（Whisper / Piper / sherpa-onnx，含国内镜像）
├── config.example.json    # 配置模板
├── tests/                 # 单元测试
│   ├── test_estimate_speed.py      # 字符估算语速测试
│   ├── test_expand_disabled.py     # expand 禁用验证测试
│   ├── test_merge_short.py         # 短段合并测试
│   ├── test_parse_translation.py   # 翻译解析器测试
│   ├── test_refine_dedup.py        # 迭代去重测试
│   ├── test_translate_retry.py     # 翻译重试回退测试
│   ├── test_translation_quality.py # 翻译质量优化测试
│   ├── test_tts_retry.py           # TTS 重试增强测试
│   ├── test_tts_engines.py        # TTS 可插拔引擎架构测试
│   └── test_voice_smoothing.py     # 语速平滑测试
├── devlog/                # 开发日志（排查记录）
└── models/                # 模型目录（不入库）
    ├── faster-whisper-*/   # Whisper 语音识别模型
    ├── piper/              # Piper TTS 中文模型
    └── sherpa-onnx/        # sherpa-onnx MeloTTS 中文模型
```

## 模型下载

统一下载脚本，默认使用国内镜像（海外加 `--no-mirror`）：

```bash
# Whisper 语音识别模型
bash download_model.sh whisper small    # 推荐，约 500MB
bash download_model.sh whisper tiny     # 轻量，约 75MB

# Piper TTS 中文模型（CPU 离线，~70MB）
bash download_model.sh piper            # 默认 huayan 女声
bash download_model.sh piper chaowen    # 其他语音

# sherpa-onnx MeloTTS 中文模型（CPU 离线，~110MB）
bash download_model.sh sherpa

# 一次下载全部
bash download_model.sh all
```

模型源：

| 模型 | 官方地址 | 国内镜像 |
|------|---------|---------|
| Whisper | `huggingface.co/Systran/faster-whisper-*` | `hf-mirror.com/Systran/faster-whisper-*` |
| Piper | `huggingface.co/rhasspy/piper-voices` | `hf-mirror.com/rhasspy/piper-voices` |
| sherpa-onnx | `github.com/k2-fsa/sherpa-onnx/releases` | `ghfast.top` 加速 |

## 开发规范

修复 bug 或添加功能时，请遵循以下流程：

1. **排查记录 → devlog**：在 `devlog/` 下新建 `{日期}-{问题简述}.md`，记录现象、排查过程（每一步做了什么、发现了什么）、根因定位和修复方案。格式参考已有日志。

2. **问题转测试 → tests/**：将排查中的验证逻辑提取为 `tests/test_*.py` 中的测试函数，确保回归可检测。测试应可用 `python3 tests/test_xxx.py` 单独运行，也可通过 `bash test.sh unit` 批量运行。

3. **运行测试**：修改完成后执行 `bash test.sh` 确认环境检查和单元测试全部通过。

```bash
bash test.sh          # 全部测试
bash test.sh smoke    # 仅环境冒烟检查
bash test.sh unit     # 仅单元测试
```

## 已知限制

1. YouTube 下载需要能访问 YouTube 的网络环境
2. yt-dlp 读取 Chrome cookies 时需关闭 Chrome 浏览器
3. Google Translate 对技术内容翻译较弱，推荐使用 LLM 翻译
4. 使用 LLM 翻译或迭代优化需额外安装 `httpx`（`setup.sh` 已包含）
5. Anaconda 自带的 ffmpeg 可能版本过旧（3.x），`run.sh` 已自动处理 Homebrew 优先

## 已解决的问题

- **翻译质量优化**（已解决）：批量 LLM 翻译缺乏上下文、对齐校验和风格控制。修复方案：
  - `_translate_llm` 注入视频标题和前文（上一批最后 2 句译文）作为上下文，提升术语一致性
  - 解析后校验有效翻译数 ≥ batch 70%，不满足则降级逐条翻译，避免翻译-原文错位
  - `DEFAULT_CONFIG["llm"]` 新增 `style` 字段，支持 "口语化"、"正式"、"学术" 等自定义翻译风格
  - LLM 翻译失败时自动回退 Google Translate（回退链：LLM 批量 → LLM 逐条重试 → Google → 保留原文）

- **语音一致性优化**（已解决）：各片段独立计算加速/降速比，语速方差大、听感割裂。修复方案：
  - `_align_tts_to_timeline` 实现三步语速平滑：收集原始 speed_ratio → 计算中位数基线 → 混合（60% 自身 + 40% 基线）+ 指数平滑（α=0.3）
  - 语速分布方差显著缩小，相邻片段过渡更自然

- **迭代性能优化**（已解决）：迭代优化对所有片段重复计算，且先生成 TTS 再迭代导致大量浪费。修复方案：
  - 引入 `converged_indices` 集合，已满足阈值的片段在后续轮次中跳过
  - 迭代阶段改用 `_estimate_speed_ratios` 基于字符数估算语速（中文 ~250ms/字），不依赖 TTS 文件
  - 流程重排：翻译 → 迭代优化（纯文本）→ TTS（一次性）→ 对齐 → 合成，避免首轮 50% TTS 白生成
  - 所有片段收敛时 early stop，无需跑满 max_iterations

- **0 字节 TTS 文件**（已解决）：edge-tts 网络不稳时生成 0 字节空文件（非文本问题，同长度文本有成功有失败），导致最终视频对应时段无配音。修复方案：
  - 翻译后 `merge_short_segments` 将 < 3 字的极短片段合并到相邻段，从源头减少 TTS 失败
  - 0 字节文件最多 3 轮重试，每轮并发降到 2、间隔递增（2s/4s/6s），避免触发 edge-tts 限流
  - 重试仍失败的，生成静音 mp3 占位，避免下游完全无声

- **speed_report 中 skipped 段**（已解决）：`status: "skipped"` 的段全部是 tts_ms=0（TTS 文件不存在或 0 字节）。修复方案：
  - `_measure_speed_ratios` 的 skipped 结果新增 `skip_reason` 字段（`"no_tts"` / `"zero_duration"`）
  - 迭代循环中打印跳过原因统计
  - 配合 TTS 重试机制从根源减少 skipped 段数

- **翻译长度匹配问题**（已解决）：中文翻译与英文原文长度不匹配时的处理：
  - **过长翻译**（加速）：通过迭代优化（`--refine`）调用 LLM 精简翻译
  - **过短翻译**（降速）：时间对齐阶段对过短片段采用轻微降速（0.85x）+ 静音填充居中放置，避免极端降速导致的不自然感
  - ⚠️ 曾尝试用 LLM 扩展过短翻译（`_expand_with_llm`），但实测发现 LLM 会生成与英文原文完全无关的内容（如将"唯一需要记住的规则是……"扩展为"四元数非交换、天然适配三维旋转"），且后续迭代在错误基础上越改越偏。已禁用此功能，改为纯静音填充方案

- **语句重复问题**（已解决）：根因是迭代优化(`--refine`)过程中 LLM 精简翻译时偷懒复制相邻段内容。修复方案：
  - 精简 prompt 中明确要求不得与上下文重复
  - 采纳 LLM 结果前自动检测与相邻段的字符重叠率（`_is_duplicate_of_neighbors`，阈值 60%），重复内容不予采纳
  - 转录和翻译后各调用 `deduplicate_segments()` 去重，清理完全相同或子串包含的连续重复片段

## TODO（按难度 / 优先级排序）

### 🔴 高优先级 / 低难度（快速改进）

- **输出日志优化**：当前 skip_steps 包含 transcribe/translate 时，日志显示 `[3/7] 语音识别 - 跳过` → `[4/7] 翻译 - 跳过` → 直接跳到 `[6/7] 生成中文配音`，中间跳过的步骤（字幕生成）没有任何提示。目标：
  - 跳过的步骤也应打印简要说明（如 `[5/7] 生成字幕 - 跳过（已在 skip_steps 中）`）
  - 断点恢复场景应明确标注哪些步骤从缓存恢复、哪些实际执行
  - TTS 引擎链切换、备份、断点恢复等关键操作应有更清晰的路径提示（已完成 tts_failure.json 路径打印改进）

### 🟡 中优先级 / 中难度（需一定重构）

- **性能监控与优化**：各模块耗时记录 + 本地 GPU 资源优化。目标：
  - 为每个主要步骤（下载、转录、翻译、TTS、对齐、合成）记录耗时并输出到日志
  - 生成性能报告（如 `output/VIDEO_ID/performance.json`），包含各阶段耗时、并发利用率、失败重试次数
  - 结合本地 GPU 资源（如 Whisper large-v3 CUDA 加速、TTS 本地模型 GPU 推理）优化资源分配
  - 支持配置 GPU 使用策略（`"gpu": "auto" / "cuda" / "cpu"`）

### 🟢 低优先级 / 高难度（架构级重构）

- **代码模块化重构 + 多角色预留**：当前 pipeline.py 单文件过大（2300+ 行），不利于迭代和多人协作。目标：
  - 按功能拆分为独立模块：`pipeline/` 目录包含 `download.py`、`transcribe.py`、`translate.py`、`tts/`（引擎抽象层）、`subtitle.py`、`merge.py`、`refine.py` 等
  - 保持现有 API 向后兼容，主入口仍为 `pipeline.py`（改为导入各模块）
  - 预留多角色支持：segments 数据结构增加 `speaker_id` 字段，TTS 引擎接口支持按角色分发（`synthesize(text, path, voice, speaker_id=None)`）
  - 配置支持多角色映射：`"speakers": {"narrator": "zh-CN-YunxiNeural", "character_a": "zh-CN-XiaoxiaoNeural"}`
  - 需要评估：speaker diarization 集成方案（pyannote-audio / Whisper 自带说话人分离）、跨引擎混用时的音质一致性、模块间数据流设计

## 许可

MIT
