# Project Guidelines

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

## 文档归档

研究/计划类工作归档到 `docs/`:
- `docs/research/` — 调研、实验
- `docs/plan/` — 实施计划
- `docs/spec/` — 功能规格
命名: `YYYY-MM-DD-<topic>.md`