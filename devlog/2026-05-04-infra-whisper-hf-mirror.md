# Whisper 模型补齐 + HF 镜像 + 基础设施变更

**日期**: 2026-05-04
**类型**: 基础设施修复

## 1. Whisper 模型文件补齐

### 问题

`rhasspy/faster-whisper-medium-int8` 量化模型缺少两个文件：
- `tokenizer.json`: faster-whisper 尝试从 `openai/whisper-tiny` 在线下载，中国防火墙阻断
- `config.json`: 缺少 `alignment_heads` 字段，导致 `RuntimeError`

### 修复

`download_model.sh` 拆分下载源：
- `model.bin` + `vocabulary.txt` ← `rhasspy/faster-whisper-{size}-int8` (量化模型)
- `config.json` + `tokenizer.json` ← `Systran/faster-whisper-{size}` (官方非量化版)

所有下载通过 `hf-mirror.com` 代理。

### 涉及文件
- `download_model.sh`: Whisper 下载函数拆分双源
- `pipeline.py:760-779`: `transcribe_audio()` 优先使用本地模型目录

## 2. HuggingFace 镜像全局方案

### 问题

中国环境下 `huggingface.co` 不可达，影响：
- Whisper 模型下载 (`download_model.sh`)
- NLLB 模型下载 (`download_model.sh`)
- VITS 男声模型下载 (`download_model.sh`)
- UTMOSv2 质量评分模型加载 (`score_videos.py` → `facebook/wav2vec2-base`)

### 修复方案

| 组件 | 方式 |
|------|------|
| `download_model.sh` | URL 中直接使用 `hf-mirror.com` 替代 `huggingface.co` |
| `score_videos.py:29-30` | 运行时设置 `os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"` |

`score_videos.py` 的自动 fallback 仅在 `HF_ENDPOINT` 未设置时生效，不覆盖用户已有配置。

## 3. 其他基础设施变更

### score_videos.py 错误显示优化
- `warnings.filterwarnings` 抑制 `gradient_checkpointing` 和 `torch.load` 告警
- 模型加载错误截断为首行、最多 80 字符

### setup.sh 依赖补充
- `sentencepiece` 加入 PACKAGES 数组（NLLB 运行时依赖）
