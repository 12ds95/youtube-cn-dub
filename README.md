# YouTube 英文视频中文配音工具

一套端到端的自动化工具链，将 YouTube 英文视频转换为带中文配音和中英双语字幕的视频。所有工具均为免费开源，全部在本地运行。

## 工作流程

```
YouTube 视频 → yt-dlp 下载 → ffmpeg 提取音频 → faster-whisper 语音识别
    → LLM 主题识别 + LLM/Google 翻译 → (可选)迭代优化
    → edge-tts 中文配音 → ffmpeg 时间对齐(语速钳制) + 字幕生成 → 合成最终视频
```

核心特性：

- **多翻译引擎**：Google Translate（免费）或 LLM 大模型（DeepSeek、Qwen、OpenAI 等 OpenAI 兼容 API）
- **翻译质量增强**：翻译前 LLM 自动扫描完整内容识别主题和专业术语，注入保护规则（如数学符号、负号）
- **迭代优化**：自动检测配音语速过快的片段，调用 LLM 精简翻译并重新生成，循环直到语速自然
- **语速钳制**：配音速度限定在 [0.95x, 1.25x] 区间，过慢静音填充、过快限速截断，听感自然
- **断点续跑**：每个步骤的中间结果均有缓存，支持从任意阶段恢复
- **结构化错误反馈**：可预知错误给出诊断信息和可操作的修复建议，不只打印 traceback
- **执行日志**：每次运行自动生成 `pipeline_*.log` 日志文件，记录各步骤耗时和错误详情
- **批量高效**：TTS 并发生成、LLM 批量翻译（带多层重试：批量→逐条→Google 兜底）

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
| `download_quality` | string | `"best"` | 下载画质：`"best"`(最高分辨率+帧率) / `"1080p"` / `"720p"` / `"480p"` |
| `rename` | string | null | 处理完成后重命名输出目录 |
| `resume_from` | string | null | 从已有输出目录断点续跑（如 `"output/f09d1957a98"`） |

### LLM 翻译配置

支持所有 OpenAI 兼容 API（DeepSeek、Qwen、Moonshot、GPT 等）。当 `translator` 设为 `"llm"` 或 `refine.enabled` 为 true 时需要配置。LLM 翻译失败会多层重试后降级为 Google Translate（回退链：LLM 批量(重试2次) → LLM 逐条(重试3次/条) → Google → 保留原文）。

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
| `skip_steps` | list | `[]` | 跳过指定步骤（按执行顺序）：`download` / `extract` / `transcribe` / `translate` / `refine` / `tts` / `subtitle` / `merge` |

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
| `original.mp4` | 下载的原始视频 |
| `audio.wav` | 提取的音频 |
| `info.json` | 视频元信息（标题、时长、帧率、分辨率） |
| `final.mp4` | 最终视频（中文配音 + 原声背景） |
| `subtitle_bilingual.srt` | 中英双语字幕 |
| `subtitle_zh.srt` / `subtitle_en.srt` | 单语字幕 |
| `segments_cache.json` | 转录+翻译缓存（可手动编辑微调） |
| `speed_report.json` | 语速调整统计（中位数、钳制数等） |
| `pipeline_YYYYMMDD_HHMMSS.log` | 执行日志（各步骤耗时 + 错误详情） |
| `tts_failure.json` | TTS 断点恢复文件（失败时生成） |
| `tts_segments/` | TTS 音频片段目录（含调速后的 `_adj.wav` 缓存） |
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
│   ├── test_tts_engines.py         # TTS 可插拔引擎架构测试
│   ├── test_tts_smoke.py           # TTS 引擎冒烟测试（真实合成）
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

本项目在多轮迭代中总结出以下工作流和守则，所有开发者（包括 AI agent）提交代码时必须遵守。

### 一、开发工作流（四环闭环）

#### 1. 排查记录 → devlog/

遇到 bug 或做功能调研时，在 `devlog/` 下新建 `{日期}-{问题简述}.md`，记录现象、排查过程（每一步做了什么、发现了什么）、根因定位和修复方案。格式参考已有日志。

**为什么要写 devlog**：排查过程是最容易遗忘的知识。不写 devlog，同一个坑会反复踩（本项目中 `_expand_with_llm` 垃圾内容问题就是因为没有及时记录第一次排查结果，导致后续又花时间重新验证才敢禁用）。

#### 2. 问题转测试 → tests/

将排查中的验证逻辑提取为 `tests/test_*.py` 中的测试函数，确保回归可检测。测试应可用 `python3 tests/test_xxx.py` 单独运行，也可通过 `bash test.sh unit` 批量运行。

#### 3. 测试反馈 → 代码修复

测试不只是验证"我的修改对不对"，更重要的是**发现代码本身的问题并反馈回去修复**。流程：

```
测试发现问题 → 分析根因在测试还是代码 → 修复代码 → 补充回归测试 → 再次验证
```

**经验教训**：本项目中测试发现 SiliconFlow 403 余额不足时引擎做无效重试（浪费几分钟），反馈回 pipeline.py 后新增了 `TTSFatalError` 异常链，遇到认证/余额等不可恢复错误立即跳到下一个引擎。如果只在测试侧 skip 掉而不修代码，生产环境还是会浪费时间。

**反面案例**：`skip_steps` 包含 `transcribe` 时 `segments` 不从缓存加载，导致 TTS 步骤被完全跳过——这个 bug 在手动测试中被偶然发现，但如果有针对 skip_steps 组合的测试，早就能拦住。

#### 4. 增量测试 + 定期全量

为了开发效率，每个任务**只跑受影响的增量测试**，不必每次全量。多次增量测试后再统一做一次全量测试。

**增量测试规则：**

- 每次 commit 只跑与当前改动相关的测试文件，commit message 中标注 `[增量测试 +1]`
- `+1` 是标记符，不是全局计数器，避免多人并发写冲突
- 示例：`fix: 修复 piper 二进制路径 [增量测试 +1]`

**全量测试规则：**

- 本地开发者检查从上次全量测试到当前 HEAD 之间有多少个 `[增量测试 +1]`
- 累计 ≥ 3 个增量提交后，必须做一次全量测试（`bash test.sh`）
- 全量测试通过后 commit message 标注 `[全量测试 ✅]`
- 示例：`test: 全量测试通过 31/31 [全量测试 ✅]`

**判断跑哪些测试：**

```bash
# 全量测试
bash test.sh

# 只跑环境冒烟检查
bash test.sh smoke

# 只跑单元测试
bash test.sh unit

# 只跑单个测试文件（增量）
venv/bin/python tests/test_tts_engines.py
venv/bin/python tests/test_tts_smoke.py
```

### 二、Commit 规范

commit message 格式：`{type}: {描述}`，type 取值：

- `fix:` 修复 bug
- `feat:` 新功能
- `test:` 测试相关
- `docs:` 文档
- `refactor:` 重构（不改变行为）

尾部可选标签：`[增量测试 +1]`、`[全量测试 ✅]`。

### 三、经验教训（踩坑总结）

以下是本项目开发过程中积累的教训，作为守则供后续开发参考。

**1. 不要只在测试侧绕过问题，要把修复反馈到生产代码。** 测试发现的异常处理缺失（如网络超时、API 余额不足）应该同步修复到 pipeline.py，而不是仅在测试中 try-except 或 skip。

**2. 缓存/跳过逻辑必须考虑所有 skip_steps 组合。** 本项目曾因 `cache_file.exists() and "transcribe" not in skip` 这个条件，导致 `skip_steps` 包含 `transcribe` 时缓存不加载、segments 为空、下游全部跳过。改动 skip 逻辑时要穷举组合验证。

**3. 错误分级：可恢复 vs 不可恢复。** 网络超时是可恢复的（重试有意义），但 401/403 认证失败是不可恢复的（重试浪费时间）。本项目因此引入 `TTSFatalError` 区分两类错误，不可恢复错误立即跳过引擎。

**4. 二进制依赖路径不能硬编码。** piper 引擎直接调用 `"piper"` 导致 venv 未激活时找不到。修复为先查 `sys.executable` 同目录，再 `shutil.which()`，最后兜底裸名。所有调用外部二进制的地方都应遵循此模式。

**5. 断点恢复信息要对用户可见。** `tts_failure.json` 最初只打印文件名，用户不知道文件在哪。所有断点恢复相关的日志必须打印完整路径，让用户知道怎么手动干预。

**6. 整体回退优于逐段混音。** TTS 引擎切换时如果只对失败片段用新引擎，会导致同一个视频中出现两种不同的声音。本项目采用整体回退策略：切换引擎时备份全部已有片段，用新引擎重新生成全部，保证语音一致性。

**7. LLM 生成内容必须校验再采纳。** 迭代优化中 LLM 曾将"唯一需要记住的规则是"扩展为"四元数非交换、天然适配三维旋转"（与原文完全无关）。所有 LLM 结果应做对齐校验、重叠率检测、长度合理性检查后再采纳。

**8. 永远不要在"没有工作可做"时报告成功。** pipeline 曾在 skip_steps 跳过所有关键步骤 + output 目录为空的情况下，0 秒"处理完成"并报告 final.mp4（实际不存在）。任何 skip 项必须检查前置产出是否存在，不存在则报错并给出诊断信息，而不是静默跳过。配置残留（换视频后忘清 skip_steps）是常见的用户错误，代码必须容错。

**9. 运行时依赖不只是 Python 包。** yt-dlp 需要 `yt-dlp-ejs` 包提供 JS challenge solver 脚本才能下载 YouTube 视频。裸装 `pip install yt-dlp` 不包含，必须用 `pip install "yt-dlp[default]"`。安装文档和环境检查（test.sh）应覆盖所有运行时依赖，包括非 Python 组件。

**10. 错误反馈的核心是"怎么修"而不是"出错了"。** pipeline 报错后如果只打印 traceback，用户看不懂、不知道该改什么。结构化错误反馈应包含三部分：错误类型（让用户知道是什么层面的问题）、具体描述（定位具体原因）、可操作的修复建议（最好包含可直接复制的命令或配置）。同时 traceback 只写日志文件不打印到屏幕，对用户友好且不丢失调试信息。

**11. 纯文档更新无需运行测试。** 仅修改 README.md 等文档文件的 commit，不涉及代码逻辑变更，无需运行测试。commit log 中应标注 `[增量测试: +0]` 而非 `+N`。这能节省时间，同时避免不必要的测试开销。但需注意：如果文档中涉及配置示例、命令示例的变更，仍应手动验证其正确性。

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
  - LLM 翻译失败会多层重试后回退 Google Translate（回退链：LLM 批量(重试2次) → LLM 逐条(重试3次) → Google → 保留原文）

- **语音一致性优化**（已解决）：各片段独立计算加速/降速比，语速方差大、听感割裂。修复方案：
  - `_align_tts_to_timeline` 实现三步语速平滑：收集原始 speed_ratio → 计算中位数基线 → 混合（60% 自身 + 40% 基线）+ 指数平滑（α=0.3）
  - 最终钳制到 [0.95, 1.25] 区间：过慢段静音居中填充，过快段限速截断
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
  - **过短翻译**（降速）：时间对齐阶段对过短片段采用轻微降速（0.95x 下限）+ 静音填充居中放置，避免极端降速导致的不自然感
  - ⚠️ 曾尝试用 LLM 扩展过短翻译（`_expand_with_llm`），但实测发现 LLM 会生成与英文原文完全无关的内容（如将"唯一需要记住的规则是……"扩展为"四元数非交换、天然适配三维旋转"），且后续迭代在错误基础上越改越偏。已禁用此功能，改为纯静音填充方案

- **语句重复问题**（已解决）：根因是迭代优化(`--refine`)过程中 LLM 精简翻译时偷懒复制相邻段内容。修复方案：
  - 精简 prompt 中明确要求不得与上下文重复
  - 采纳 LLM 结果前自动检测与相邻段的字符重叠率（`_is_duplicate_of_neighbors`，阈值 60%），重复内容不予采纳
  - 转录和翻译后各调用 `deduplicate_segments()` 去重，清理完全相同或子串包含的连续重复片段

- **下载画质与帧率**（已解决）：原下载参数硬编码 `height<=720`，与 YouTube 在线观看的高帧率/高分辨率体验不一致。修复方案：
  - 新增 `download_quality` 配置项，默认 `"best"` 下载源视频最高分辨率+最高帧率（如 1440p60/1080p60）
  - 支持 `"1080p"` / `"720p"` / `"480p"` 限制分辨率上限，始终选择可用的最高帧率
  - `info.json` 中记录实际下载的帧率和分辨率

- **语速调整失控**（已解决）：时间线对齐阶段 `_align_tts_to_timeline` 无语速区间约束，极端情况下出现 0.5x 拖慢或 1.8x 快进，听感极差。修复方案：
  - 语速钳制到 [0.95, 1.25] 区间：低于 0.95 的用静音居中填充而非极端降速，高于 1.25 的限速截断
  - 保留三步平滑策略（中位数基线 → 混合 → 指数平滑）后再钳制
  - 输出 `speed_report.json` 记录调速统计（中位数、钳制数等），支持调试和断点恢复
  - 调速后的 `seg_XXXX_adj.wav` 自动缓存，断点恢复时跳过已调速片段

- **翻译术语保护**（已解决）：LLM 翻译数学/科学内容时将 "i" 翻译为"我"、丢失负号等。修复方案：
  - 翻译前 LLM 扫描完整视频内容（均匀采样头/中/尾，避免被开头广告误导）自动识别主题和专业领域
  - 根据识别结果生成专业术语保护规则（如 `i→虚数i`、`负号不可省略`）注入到 system_prompt
  - 始终注入通用规则：数学符号不译为日常用语、负号必须保留、倒装句调整语序

- **LLM 翻译过早回退 Google**（已解决）：批量 LLM 翻译成功（HTTP 200）但部分段解析为空时，直接跳到 Google Translate，没给 LLM 逐条重试的机会。修复方案：
  - 批量请求本身增加 2 次重试（对齐失败或请求异常时等待后重试）
  - 批量解析后单条校验失败的段，先走 LLM 逐条重试（3 次/条）
  - 仅 LLM 逐条重试仍失败的段，才回退 Google Translate
  - 完整链路：批量LLM(重试2次) → 逐条LLM(重试3次) → Google → 保留原文

## TODO（按难度 / 优先级排序）

### 🔴 高优先级 / 低难度（快速改进）

- **输出日志优化**：~~当前 skip_steps 包含 transcribe/translate 时，日志显示 `[3/7] 语音识别 - 跳过` → `[4/7] 翻译 - 跳过` → 直接跳到 `[6/7] 生成中文配音`，中间跳过的步骤（字幕生成）没有任何提示。~~ 已部分解决：
  - ✅ 每次执行生成 `pipeline_YYYYMMDD_HHMMSS.log` 日志文件，记录各步骤耗时
  - ✅ 可预知错误给出结构化反馈（错误类型 + 问题描述 + 修复建议）
  - ✅ skip_steps 跳过关键步骤但产出不存在时，给出可直接使用的配置修改建议
  - 待完善：跳过的步骤应打印简要说明（如 `[5/7] 生成字幕 - 跳过（已在 skip_steps 中）`）
  - 待完善：断点恢复场景应明确标注哪些步骤从缓存恢复、哪些实际执行

- **字幕输出模式可选**：当前只生成外挂字幕（`.srt`），最终视频不内嵌字幕。目标：
  - 新增配置项 `subtitle_mode`，支持三种模式：`"external"`（仅外挂 `.srt`，默认）、`"embedded"`（仅内嵌到视频）、`"both"`（同时生成外挂和内嵌两个版本）
  - 内嵌字幕使用 ffmpeg `-vf subtitles=xxx.srt` 或 `-c:s mov_text` 软字幕
  - 输出文件命名：`final.mp4`（外挂）、`final_subtitled.mp4`（内嵌）

- **翻译质量增强 — 倒装句语序优化**：英文倒装句翻译后语序不符合中文习惯，且可能导致相邻译文内容交换（swap）。目标：
  - 优化 LLM 翻译 prompt，明确要求倒装句调整为中文习惯语序
  - 检测并处理倒装导致的相邻译文内容 swap 问题：当第 N 段译文与第 N+1 段原文更匹配时，自动交换，这种交换可能不止一次
  - 新增后处理步骤：翻译完成后扫描相邻段或者多断，用语义相似度（或 LLM 判定）检测错位并修复
  - 测试用例：收集典型英文倒装句（there be, 状语前置, 宾语前置等）验证翻译语序

### 🟡 中优先级 / 中难度（需一定重构）

- **人声与背景音分离**：当前混音直接对原始音频整体降低音量（`volume` 参数），人声和背景音（环境音、BGM）不区分，导致背景音也被压低。目标：
  - 引入音频分离模型（如 [demucs](https://github.com/facebookresearch/demucs)）将原始音频拆分为人声轨（vocals）和伴奏轨（accompaniment/other）
  - 合成阶段：人声轨大幅降低或静音（被中文配音替代），伴奏轨保持原始音量
  - 新增配置项：`"audio_separation": { "enabled": true, "vocal_volume": 0.0, "bgm_volume": 1.0 }`
  - 需评估：demucs 模型大小（~300MB）、CPU 推理耗时（预计 1-2 分钟/5分钟视频）、是否需要 GPU 加速

- **性能监控与优化**：~~各模块耗时记录~~ + 本地 GPU 资源优化。已部分解决：
  - ✅ PipelineLogger 为每个主要步骤记录耗时，写入日志文件
  - 待完善：生成性能报告（如 `output/VIDEO_ID/performance.json`），包含各阶段耗时、并发利用率、失败重试次数
  - 待完善：结合本地 GPU 资源（如 Whisper large-v3 CUDA 加速、TTS 本地模型 GPU 推理）优化资源分配
  - 待完善：支持配置 GPU 使用策略（`"gpu": "auto" / "cuda" / "cpu"`）

### 🟢 低优先级 / 高难度（架构级重构）

- **代码模块化重构 + 多角色预留**：当前 pipeline.py 单文件过大（2300+ 行），不利于迭代和多人协作。目标：
  - 按功能拆分为独立模块：`pipeline/` 目录包含 `download.py`、`transcribe.py`、`translate.py`、`tts/`（引擎抽象层）、`subtitle.py`、`merge.py`、`refine.py` 等
  - 保持现有 API 向后兼容，主入口仍为 `pipeline.py`（改为导入各模块）
  - 预留多角色支持：segments 数据结构增加 `speaker_id` 字段，TTS 引擎接口支持按角色分发（`synthesize(text, path, voice, speaker_id=None)`）
  - 配置支持多角色映射：`"speakers": {"narrator": "zh-CN-YunxiNeural", "character_a": "zh-CN-XiaoxiaoNeural"}`
  - 需要评估：speaker diarization 集成方案（pyannote-audio / Whisper 自带说话人分离）、跨引擎混用时的音质一致性、模块间数据流设计

## 许可

MIT
