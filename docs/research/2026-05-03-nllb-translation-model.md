# NLLB-200 本地翻译模型调研

> 调研日期: 2026-05-03
> 目的: 寻找本地离线英译中翻译模型，作为 Google Translate fallback

## 1. 背景

项目翻译模块当前使用：
- `deep_translator.GoogleTranslator` (在线，免费)
- LLM API (在线，需付费)

需要本地离线翻译方案作为 fallback，当网络不可用或 API 失败时替代 Google Translate。

## 2. 方案调研

### 2.1 候选方案对比

| 方案 | 大小 | 格式 | 依赖 | 质量 | 推荐度 |
|------|------|------|------|------|--------|
| **NLLB-200-distilled-600M ct2 int8** | ~600MB | ctranslate2 | ✅ 已有 | ⭐⭐⭐⭐ | **首选** |
| meta-flores T5-base GGUF Q4_K_M | ~147MB | GGUF | 需 llama.cpp | ⭐⭐⭐⭐ | 备选 |
| meta-translation T5-small GGUF Q4_K_M | ~42MB | GGUF | 需 llama.cpp | ⭐⭐⭐ | 备选 |
| OPUS-MT-en-zh | ~300MB | transformers | 需 PyTorch | ⭐⭐⭐ | 不推荐 |
| Helsinki-NLP/opus-mt-en-zh | ~300MB | transformers | 需额外依赖 | ⭐⭐⭐ | 不推荐 |

### 2.2 选定方案理由

**NLLB-200-distilled-600M-ct2-int8**

1. **依赖兼容**: 项目已安装 ctranslate2 (faster-whisper 使用)，无需额外依赖
2. **多语言支持**: Meta NLLB-200 支持 200+ 语言，英译中 (eng_Latn → zho_Hans) 质量高
3. **int8 量化**: ~600MB，CPU 可运行，内存友好
4. **官方模型**: Meta AI 发布，质量有保障

### 2.3 HuggingFace 搜索结果

NLLB ctranslate2 版本:
```
JustFrederik/nllb-200-distilled-600M-ct2-int8   ← 选定
JustFrederik/nllb-200-distilled-1.3B-ct2-int8
JustFrederik/nllb-200-1.3B-ct2-int8
```

其他候选:
```
NeuraFusionAI/meta-translation-chinese-english-model  (T5-small, ~230MB)
NeuraFusionAI/meta-flores-translation-chinese-english-model  (T5-base, ~850MB)
mradermacher/meta-translation-chinese-english-model-GGUF  (Q4_K_M: ~42MB)
mradermacher/meta-flores-translation-chinese-english-model-GGUF  (Q4_K_M: ~147MB)
Helsinki-NLP/opus-mt-en-zh  (~300MB)
```

## 3. 模型下载

### 3.1 文件清单

```
models/nllb-200-distilled-600M-ct2-int8/
├── model.bin               594MB   主模型 (int8量化)
├── sentencepiece.bpe.model  4.6MB   分词器
├── tokenizer.json           17MB    tokenizer 配置
├── shared_vocabulary.txt    2.4MB   共享词汇表
├── config.json              159B    模型配置
└───────────────────────────────────
总计: ~618MB
```

### 3.2 下载命令

```bash
# 创建目录
mkdir -p models/nllb-200-distilled-600M-ct2-int8

# 下载 (使用 hf-mirror.com 国内镜像)
BASE="https://hf-mirror.com/JustFrederik/nllb-200-distilled-600M-ct2-int8/resolve/main"

aria2c -x 16 -s 16 -d models/nllb-200-distilled-600M-ct2-int8 -o model.bin "$BASE/model.bin"
aria2c -x 8 -s 8 -d models/nllb-200-distilled-600M-ct2-int8 -o sentencepiece.bpe.model "$BASE/sentencepiece.bpe.model"
aria2c -x 8 -s 8 -d models/nllb-200-distilled-600M-ct2-int8 -o config.json "$BASE/config.json"
aria2c -x 8 -s 8 -d models/nllb-200-distilled-600M-ct2-int8 -o tokenizer.json "$BASE/tokenizer.json"
aria2c -x 8 -s 8 -d models/nllb-200-distilled-600M-ct2-int8 -o shared_vocabulary.txt "$BASE/shared_vocabulary.txt"
```

### 3.3 验证

```bash
ls -lh models/nllb-200-distilled-600M-ct2-int8/
# total 1266360
# -rw-r--r--  159B  config.json
# -rw-r--r--  594M  model.bin
# -rw-r--r--  4.6M  sentencepiece.bpe.model
# -rw-r--r--  2.4M  shared_vocabulary.txt
# -rw-r--r--   17M  tokenizer.json
```

## 4. 使用方式 (待集成)

### 4.1 ctranslate2 调用示例

```python
import ctranslate2

# 加载模型
translator = ctranslate2.Translator("models/nllb-200-distilled-600M-ct2-int8")

# 英译中
# NLLB 语言代码: eng_Latn (英文) → zho_Hans (简体中文)
source_text = "Hello, how are you?"
result = translator.translate_batch(
    [["eng_Latn", source_text]],
    target_lang="zho_Hans"
)
print(result[0].hypotheses[0])  # "你好，你好吗？"
```

### 4.2 语言代码映射

| 语言 | NLLB 代码 |
|------|-----------|
| English | `eng_Latn` |
| 简体中文 | `zho_Hans` |
| 繁体中文 | `zho_Hant` |
| 日语 | `jpn_Jpan` |

### 4.3 集成建议

修改 `pipeline.py` 翻译模块，添加 `"nllb"` 选项：

```json
{
  "translator": "nllb",
  "nllb_model": "models/nllb-200-distilled-600M-ct2-int8"
}
```

或在 `_translate_google()` 中作为 fallback：

```python
def _translate_google(segments):
    try:
        # 尝试 Google Translate
        ...
    except Exception:
        # 网络失败时 fallback 到 NLLB
        return _translate_nllb(segments)
```

## 5. 性能预估

| 指标 | 估算值 |
|------|--------|
| 单句翻译时间 | ~100-300ms (CPU) |
| 内存占用 | ~700MB |
| 并发批量翻译 | 支持 (translate_batch) |
| 质量 | 接近 Google Translate |

## 6. 参考资料

- [Meta NLLB-200 论文](https://arxiv.org/abs/2207.04672)
- [JustFrederik/nllb-200-distilled-600M-ct2-int8 (HuggingFace)](https://huggingface.co/JustFrederik/nllb-200-distilled-600M-ct2-int8)
- [ctranslate2 文档](https://opennmt.net/CTranslate2/)
- [FLORES-200 数据集](https://github.com/facebookresearch/flores)