## YouTube 英文视频中文配音方案 (v2)

### 方案概览

这是一套完整的端到端工具链，可以将 YouTube 英文视频自动转换为带中文配音和中英双语字幕的视频。所有工具均为免费开源，全部在本地运行。

项目位置: `/Users/caixin/Desktop/youtube-cn-dub/`

### 工具链

| 步骤 | 工具 | 作用 |
|------|------|------|
| 下载视频 | yt-dlp + yt-dlp-ejs | 从 YouTube 下载视频，通过浏览器 cookies 认证 |
| 语音识别 | faster-whisper | 英文语音转文字，带精确时间戳 |
| 翻译 | Google Translate / LLM (可选) | 英文翻译为中文，支持大模型翻译 |
| 中文配音 | edge-tts | 微软 TTS 引擎，自然中文语音（并发生成） |
| 时间对齐 | ffmpeg atempo | 调速使中文配音匹配原始时间线 |
| 视频合成 | ffmpeg | 合并视频+配音+原声背景 |

### 快速使用

```bash
cd /Users/caixin/Desktop/youtube-cn-dub

# 方式一：直接指定 URL
source venv/bin/activate
python pipeline.py "https://www.youtube.com/watch?v=XXXX"

# 方式二：使用 JSON 配置文件
python pipeline.py --config config.json

# 方式三：使用 run.sh 封装脚本
bash run.sh "https://www.youtube.com/watch?v=XXXX"
```

### JSON 配置文件

复制 `config.example.json` 为 `config.json`，按需修改。配置优先级：CLI 参数 > JSON 配置 > 默认值。

```bash
cp config.example.json config.json
# 编辑 config.json 后运行
python pipeline.py --config config.json

# CLI 参数可覆盖配置文件中的值
python pipeline.py --config config.json --translator llm --voice zh-CN-XiaoxiaoNeural
```

### 常用参数

```bash
# 选择中文语音
python pipeline.py "URL" --voice zh-CN-XiaoxiaoNeural    # 女声（温暖）
python pipeline.py "URL" --voice zh-CN-YunxiNeural        # 男声（默认）
python pipeline.py "URL" --voice zh-CN-YunyangNeural      # 男声（播报风格）

# 选择 Whisper 模型（精度 vs 速度）
python pipeline.py "URL" --whisper-model tiny     # 最快，精度一般
python pipeline.py "URL" --whisper-model small    # 推荐，精度好
python pipeline.py "URL" --whisper-model medium   # 最精确，较慢

# 调整原声背景音量（0.0=静音，1.0=原始）
python pipeline.py "URL" --volume 0.2

# 指定浏览器（默认 chrome）
python pipeline.py "URL" --browser edge
```

### LLM 大模型翻译

支持所有 OpenAI 兼容 API（DeepSeek、OpenAI、Moonshot、Ollama 等），翻译质量显著优于 Google Translate。

```bash
# 命令行方式
python pipeline.py "URL" --translator llm \
    --llm-api-key sk-xxxxx \
    --llm-api-url https://api.deepseek.com/v1 \
    --llm-model deepseek-chat

# 或在 config.json 中配置（推荐，避免在命令行暴露 API Key）
```

config.json 中 LLM 部分示例：
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

如果 LLM 调用失败，会自动降级为 Google Translate。

### 调试中间步骤

使用 `--resume-from` 可从已有的输出目录重跑部分步骤，配合手动编辑 `segments_cache.json` 微调翻译效果：

```bash
# 切换为 LLM 翻译并重新生成（删除旧缓存后）
rm output/zjMuIxRvygQ/segments_cache.json
python pipeline.py --resume-from output/zjMuIxRvygQ --translator llm --llm-api-key sk-xxx

# 仅重新生成配音和视频（保留已有翻译）
python pipeline.py --resume-from output/zjMuIxRvygQ

# 手动微调翻译后重新生成字幕+配音
#   1. 编辑 output/zjMuIxRvygQ/segments_cache.json 中的 text_zh 字段
#   2. 删除 tts_segments 目录和旧字幕
#   3. 重新运行
rm -rf output/zjMuIxRvygQ/tts_segments output/zjMuIxRvygQ/subtitle_*.srt
python pipeline.py --resume-from output/zjMuIxRvygQ
```

### 完成后重命名

```bash
# 处理完成后将 output/<video_id> 重命名为有意义的名字
python pipeline.py "URL" --rename "线性代数精讲"
# 输出: output/线性代数精讲/
```

### 性能优化选项

```bash
# 增大 TTS 并发数（默认 5，网络好可提高到 10）
python pipeline.py "URL" --tts-concurrency 10

# 使用更快的 Whisper 模型（tiny 最快，但精度降低）
python pipeline.py "URL" --whisper-model tiny
```

在 config.json 中可用 `skip_steps` 跳过特定步骤：
```json
{
  "skip_steps": ["download", "transcribe"]
}
```
可跳过的步骤名：`download`, `extract`, `transcribe`, `translate`, `subtitle`, `tts`, `merge`

### 输出文件说明

每个视频的输出在 `output/<video_id>/` 目录下：

| 文件 | 说明 |
|------|------|
| `final.mp4` | 最终视频（中文配音 + 原声背景） |
| `subtitle_bilingual.srt` | 中英双语字幕 |
| `subtitle_zh.srt` | 中文字幕 |
| `subtitle_en.srt` | 英文字幕 |
| `original.mp4` | 原始视频 |
| `chinese_dub.wav` | 中文配音音轨 |
| `segments_cache.json` | 转录+翻译缓存（可手动编辑微调） |
| `info.json` | 视频元信息 |

播放时用 VLC/IINA 等播放器打开 `final.mp4`，手动加载 `subtitle_bilingual.srt` 即可看到中英双语字幕。

### 下载 Whisper 模型（国内网络）

由于 HuggingFace 在国内访问不稳定，提供了镜像下载脚本：

```bash
bash download_model.sh small    # 推荐，约 500MB
bash download_model.sh tiny     # 轻量，约 75MB
```

模型下载后保存在 `models/` 目录，pipeline 会自动优先使用本地模型。

### 已知限制与建议

1. **网络要求**：YouTube 下载需要代理；Google 翻译和 edge-tts 需要网络连接
2. **Chrome 需关闭**：yt-dlp 读取 Chrome cookies 时，Chrome 浏览器需要处于关闭状态
3. **翻译质量**：Google Translate 对技术内容翻译偏弱，推荐使用 LLM 翻译（DeepSeek 费用低效果好）
4. **配音语速**：部分中文翻译较长的片段会被加速，极端情况下可能听起来不自然
5. **Intel Mac 性能**：Whisper small 模型转录一个 6 分钟视频大约需要 2-3 分钟
6. **LLM 依赖**：使用 LLM 翻译需额外安装 `pip install httpx`
