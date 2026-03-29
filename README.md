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
# 选择中文语音
--voice zh-CN-YunxiNeural        # 男声（默认）
--voice zh-CN-XiaoxiaoNeural     # 女声
--voice zh-CN-YunyangNeural      # 男声（播报风格）

# Whisper 模型（精度 vs 速度）
--whisper-model tiny              # 最快，精度一般
--whisper-model small             # 推荐（默认）
--whisper-model medium            # 最精确，较慢

# 原声背景音量（0.0=静音，1.0=原始）
--volume 0.2

# 处理完成后重命名输出目录
--rename "线性代数精讲"
```

### LLM 翻译配置

支持所有 OpenAI 兼容 API。在 `config.json` 中配置：

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

`style` 字段可选，设置后会影响翻译风格。常用值：`"口语化"`、`"正式"`、`"学术"`。留空则使用默认风格。

命令行参数可覆盖配置文件中的值。如果 LLM 调用失败，会自动降级为 Google Translate。

### TTS 引擎配置

支持 7 个 TTS 引擎，可在 `config.json` 中切换。失败时自动按 `tts_fallback` 列表依次降级。

| 引擎 | 类型 | 中文质量 | 需要 | 说明 |
|------|------|---------|------|------|
| `edge-tts` | 在线免费 | 优秀 | 网络 | 默认引擎，微软免费 API，多音色 |
| `siliconflow` | 在线免费 | 最佳 | API Key | 硅基流动 CosyVoice2，注册送额度 |
| `gtts` | 在线免费 | 良好 | 网络 | Google Translate TTS，单音色 |
| `pyttsx3` | 离线免费 | 一般 | 无 | 系统自带 TTS，零依赖终极兜底 |
| `piper` | 离线免费 | 良好 | 下载模型 | ONNX 推理，CPU 友好 |
| `sherpa-onnx` | 离线免费 | 良好 | 下载模型 | 含 MeloTTS 中文模型，CPU 友好 |
| `cosyvoice` | 本地部署 | 最佳 | GPU | 阿里开源，支持声音克隆 |

配置示例（推荐 edge-tts 主引擎 + gtts/pyttsx3 兜底）：

```json
{
  "tts_engine": "edge-tts",
  "tts_fallback": ["gtts", "pyttsx3"]
}
```

使用 SiliconFlow CosyVoice2（注册 https://cloud.siliconflow.cn 获取免费 API Key）：

```json
{
  "tts_engine": "siliconflow",
  "tts_fallback": ["edge-tts", "pyttsx3"],
  "siliconflow": {
    "api_key": "sk-xxx",
    "model": "FunAudioLLM/CosyVoice2-0.5B",
    "voice": "FunAudioLLM/CosyVoice2-0.5B:alex"
  }
}
```

`pyttsx3` 离线兜底需 macOS 中文语音包（系统设置 → 辅助功能 → 朗读内容 → 管理声音 → 下载 Ting-Ting）。

### 迭代优化

当中文翻译比英文原文长时，TTS 配音需要加速播放以匹配时间线。迭代优化功能会自动检测加速过大的片段，调用 LLM 精简翻译后重新生成：

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
├── download_model.sh      # Whisper 模型下载（国内镜像）
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
│   └── test_voice_smoothing.py     # 语速平滑测试
├── devlog/                # 开发日志（排查记录）
└── models/                # Whisper 模型目录（不入库）
```

## Whisper 模型下载

如果 HuggingFace 访问不畅，可用国内镜像下载：

```bash
bash download_model.sh small    # 推荐，约 500MB
bash download_model.sh tiny     # 轻量，约 75MB
```

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

## 许可

MIT
