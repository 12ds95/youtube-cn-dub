# 模型量化升级可行性分析

**日期**: 2025-06-05
**状态**: Whisper 已实施（small → medium 默认 + large-v3-turbo 可配置），其余暂不实施

## 项目当前模型清单

| 模型 | 用途 | 当前版本 | 参数量 | 磁盘大小 |
|------|------|---------|--------|---------|
| faster-whisper | ASR 转录 | small | 244M | ~500MB |
| Piper TTS | 本地 TTS (fallback) | zh_CN-huayan-medium | - | ~70MB |
| sherpa-onnx MeloTTS | 本地 TTS (fallback) | vits-melo-tts-zh_en | - | ~110MB |
| Demucs | 人声分离 | htdemucs | - | ~300MB |
| spaCy | NLP 断句 | en_core_web_sm | - | ~12MB |
| DeepSeek | LLM 翻译/精简 | deepseek-chat | 远程 API | N/A |

## 分析结论

### Whisper: small → medium int8 (默认) + large-v3-turbo (可配置) — 已实施

| 变体 | 参数量 | 磁盘大小 | 量化 | GPU 速率¹ | WER¹ |
|------|--------|---------|------|---------|------|
| small | 244M | 461MB | float16 | 52.6s | 2.39% |
| **medium (新默认)** | **769M** | **749MB** | **int8** | **26.1s** | **2.39%** |
| medium (Systran 原版) | 769M | 1,457MB | float16 | 26.1s | 2.39% |
| **large-v3-turbo (可配置)** | **809M** | **~1.6GB** | **float16** | **19.2s** | **1.92%** |
| large-v3 | 1550M | ~3.1GB | float16 | 52.0s | 2.88% |

¹ 13 分钟音频，GPU fp16 beam=5

- 两级方案：medium int8 作为默认（中文 WER -26% vs small，磁盘仅 +288MB），turbo 作为高级选项
- medium int8 来源：`rhasspy/faster-whisper-medium-int8`（社区预量化，749MB vs Systran float16 1,457MB）
- int8 量化精度几乎无损（CTranslate2 运行时本来就会做 int8 量化，预量化只省加载时间和磁盘）
- turbo 无 Systran 官方 repo，使用 `deepdml/faster-whisper-large-v3-turbo-ct2`
- 国内镜像：hf-mirror.com 均可访问

**实施内容**:
- `pipeline.py`: DEFAULT_CONFIG `whisper_model` = `"medium"`，CLI 添加 `large-v3-turbo` 选项
- `pipeline.py`: `transcribe_audio()` 添加 `_HF_MODEL_MAP` 映射 medium → int8 repo，turbo → 社区 repo
- `download_model.sh`: 默认下载 medium int8，支持 `bash download_model.sh whisper large-v3-turbo`
- `config.example.json`: 更新默认值和说明

### Demucs: htdemucs → htdemucs_ft — 不推荐

- htdemucs_ft 平均 SDR 反而更低（8.23 vs 8.38 dB），速度慢 3.5x
- 人声 SDR 几乎相同（8.80 vs 8.86）
- audio_separation 默认关闭，非核心路径

### spaCy: en_core_web_sm → md/lg — 不推荐

- sentencizer/parser 精度三个模型完全相同，差异仅在 NER 和词向量
- 本项目只用断句，lg 模型 560MB 换来零提升

### Piper TTS / sherpa-onnx — 不可行

- zh_CN Piper 模型仅有 medium 质量级别，无 high 版本
- sherpa-onnx vits-melo-tts-zh_en 无量化变体
- 均为 fallback 引擎，对最终质量影响小

### DeepSeek LLM — 非量化问题

- 远程 API，升级路径是换模型（deepseek-v3, gpt-4o 等），属 API 选择而非量化

## 总结

| 模型 | 升级方案 | 效果提升 | 速度影响 | 推荐 |
|------|---------|---------|---------|------|
| **Whisper** | small → large-v3-turbo int8 | WER -20% | CPU 慢 1-2x | **强烈推荐** |
| Demucs | htdemucs → htdemucs_ft | 无提升(-1.8%) | 慢 3.5x | 不推荐 |
| spaCy | sm → lg | 断句无提升 | 加载慢 | 不推荐 |
| Piper/sherpa-onnx | 无更大版本 | N/A | N/A | 不可行 |
