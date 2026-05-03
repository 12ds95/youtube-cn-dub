# AGENT.md

## 项目

YouTube 英文视频中文配音工具链：下载 → 语音识别 → 翻译 → TTS 配音 → 合成视频

## Pipeline

```
yt-dlp下载 → ffmpeg提取音频 → [demucs分离] → faster-whisper识别 → [LLM翻译] → edge-tts配音 → 合成
```

## 核心文件

| 文件 | 职责 |
|------|------|
| `pipeline.py` | 主入口 |
| `phase2_translate.py` | Phase 2 翻译优化 |
| `calibrate_tts_duration.py` | TTS 时长校准 |
| `text_utils.py` | 文本处理工具 |
| `test_translate_only.py` | 翻译测试 |
| `run.sh` | 运行入口 |
| `setup.sh` | 环境安装 |

## 环境约束 (CRITICAL)

**运行 Python 脚本必须使用 venv**，系统 Python 缺少依赖且 SSL 不可用。

```
VENV_PATH: /Users/caixin/Desktop/youtube-cn-dub/venv/bin/python3
PYTHON_VERSION: 3.11.15
PACKAGES: 294
KEY_DEPS: torch 2.2.2, jieba 0.42.1, demucs 4.0.1, faster-whisper 1.2.1, edge-tts 7.2.8, pytest 9.0.2
```

正确用法: `venv/bin/python3 <script.py>`
错误用法: `python3 <script.py>` (会失败)

## API Key 安全

- 真实 API Key 只存放于 `config.json` (已在 .gitignore)
- 禁止硬编码、禁止写入临时文件、禁止提交到 git
- Shell 读取: `LLM_API_KEY=$(python3 -c "import json; c=json.load(open('config.json')); print(c.get('llm',{}).get('api_key',''))")`

## 配置

- `config.json` — 主配置 (不提交)
- `config.example.json` — 模板

## Prompts

- `prompts/default.txt` — 默认翻译
- `prompts/dubbing_concise.txt` — 简洁配音风格

## 文档归档

研究/计划类工作归档到 `docs/`:
- `docs/research/` — 调研、实验
- `docs/plan/` — 实施计划
- `docs/spec/` — 功能规格
命名: `YYYY-MM-DD-<topic>.md`

## 翻译引擎

| 引擎 | 配置值 | 说明 |
|------|--------|------|
| Google | `google` | 免费，无需 Key |
| LLM | `llm` | DeepSeek/Qwen，质量好 |

## TTS 引擎

| 引擎 | 说明 |
|------|------|
| edge-tts | 微软在线，质量最好 |
| gtts | Google TTS |

## 测试

```bash
venv/bin/python3 -m pytest tests/ -v
```