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
    "temperature": 0.3
  }
}
```

命令行参数可覆盖配置文件中的值。如果 LLM 调用失败，会自动降级为 Google Translate。

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
│   ├── test_parse_translation.py   # 翻译解析器测试
│   └── test_refine_dedup.py        # 迭代去重测试
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

## TODO

### 1. 翻译质量优化

当前 LLM 翻译采用批量模式（一次 API 调用翻译 batch_size 条句子），需要进一步优化：

- **上下文翻译**：当前批次翻译缺乏全局语境，考虑在 prompt 中提供前后文摘要或视频主题，提升技术术语一致性
- **对齐保证**：批量翻译必须保持与原文严格一一对齐（输入 N 句，输出 N 句），否则后续 TTS 和时间对齐流程会错位。需加强解析校验和降级策略
- **翻译风格控制**：可在 prompt 中指定翻译风格（口语化/正式），适配不同视频类型

### 2. 语音一致性优化

当前每个片段独立计算加速/降速比，导致不同片段语速差异大（有的 0.9x 有的 1.3x），听感不连贯：

- **全局语速基线**：根据所有片段的 TTS 时长和目标时长，算法计算一个全局最优语速基线，使整体变速方差最小
- **平滑过渡**：相邻片段的语速变化应平滑，避免突然加速/减速的割裂感

### 3. 迭代性能优化

确认大迭代（人工审听循环）和小迭代（自动精简循环）都支持 early stop——当所有片段均满足阈值时立即终止，无需跑满 max_iterations：

- **避免重复计算**：early stop 后的剩余迭代跳过；已满足阈值的片段在后续迭代中标记跳过，只处理仍然超标的
- **增量 TTS**：只对翻译变更的片段重新生成 TTS，未变更的复用上一轮缓存

### 4. 排查 0 字节 TTS 文件

`output/32884a7ba3d/tts_segments/seg_0007.mp3` 为 0 字节空文件，确认为已知现象。需排查：

- 对应的 `text_zh` 是否为空或异常短（可能是翻译解析失败的遗留）
- edge-tts 对空文本或特殊字符是否会生成空文件
- 空文件是否导致最终视频中对应时间段无声音
- 是否需要在 TTS 生成阶段增加空文件检测和重试/静音填充

### 5. 理解 speed_report 中 skipped 的含义

`iter_N_speed_report.json` 中 `status: "skipped"` 的段（如 `f09d1957a98` 的 iter_1 中有 75/820 段）表示该段 tts_ms=0（无 TTS 音频可测速），速度评估被跳过。需确认：

- 这些段是否因 TTS 生成失败（0 字节文件）导致
- 被 skip 的段在最终视频中是否有声音
- 迭代优化是否应该尝试修复这些段而非跳过

## 待验证修复

以下修复已实现，需要在实际视频处理流程中验证效果：

- **迭代优化重复检测**：`_is_duplicate_of_neighbors()` 在采纳 LLM 精简/扩展结果前检查与相邻段的字符重叠率（阈值 60%），重复内容不予采纳并打印警告。需用实际视频验证拦截准确率和误杀率。
- **过短片段扩展**：`_expand_with_llm()` 调用 LLM 扩展 TTS 时长 < 原始时长 70% 的翻译，时间对齐阶段对仍过短的片段采用轻微降速（0.85x）+ 静音填充居中放置。需验证扩展后的翻译质量和静音填充的听感。
- **去重函数兜底**：`deduplicate_segments()` 在 Whisper 转录后和翻译后各调用一次，清理完全相同或子串包含的连续重复片段。需验证不会误删正常相邻的相似内容。

## 已解决的问题

- **翻译长度匹配问题**（已解决）：中文翻译与英文原文长度不匹配时的处理：
  - **过长翻译**（加速）：通过迭代优化（`--refine`）调用 LLM 精简翻译
  - **过短翻译**（降速）：迭代优化中自动检测过短片段（TTS 时长 < 原始时长 70%），调用 LLM 补充细节扩展翻译；时间对齐阶段对仍然过短的片段采用轻微降速 + 静音填充居中放置，避免极端降速导致的不自然感

- **语句重复问题**（已解决）：根因是迭代优化(`--refine`)过程中 LLM 精简翻译时偷懒复制相邻段内容。修复方案：
  - 精简/扩展 prompt 中明确要求不得与上下文重复
  - 采纳 LLM 结果前自动检测与相邻段的字符重叠率，重复内容不予采纳
  - 转录和翻译后各增加去重步骤，兜底清理完全重复或子串包含的片段

## 许可

MIT
