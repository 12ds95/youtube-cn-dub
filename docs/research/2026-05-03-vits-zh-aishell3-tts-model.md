# VITS-ZH-AISHELL3 多说话人中文 TTS 模型调研

> 调研日期: 2026-05-03
> 目的: 寻找本地离线、支持男声的中文 TTS 方案

## 1. 背景

项目已集成的本地 TTS 方案均无男声：
- Piper zh_CN: 仅 3 个女声 (huayan/chaowen/xiao_ya)
- sherpa-onnx MeloTTS: 单一女声
- pyttsx3: 依赖系统语音，质量不稳定

需要支持男声的本地离线方案，质量接近 edge-tts。

## 2. 方案调研

### 2.1 AISHELL-3 数据集

- 来源: 希尔贝壳中文普通话语音数据库
- 规模: 85小时, 88035句, **218名说话人**
- 特点: 多说话人、男女声混合、不同口音区域
- 标注: 拼音+韵律, 音字确率98%+

### 2.2 VITS 中文模型

HuggingFace 搜索结果:

| 模型 | 说话人 | 说明 |
|------|--------|------|
| `jackyqs/vits-aishell3-175-chinese` | 175人 | 原始 PyTorch 模型 |
| `csukuangfj/vits-zh-aishell3` | 175人 | ONNX 导出版 (sherpa-onnx 作者) |
| `Hollway/vits_for_chinese` | - | 单说话人 |
| `guiyun/Bert-VITS2-chinese` | - | Bert-VITS2 变体 |

### 2.3 选定方案

**`csukuangfj/vits-zh-aishell3`**

优势:
- ✅ 175 说话人 (含男女声)
- ✅ ONNX int8 量化 (~38MB, CPU友好)
- ✅ sherpa-onnx 作者维护, 格式兼容
- ✅ 包含完整文本处理文件 (lexicon/tokens/FST)

## 3. 模型下载

### 3.1 文件清单

```
models/vits-zh-aishell3/
├── vits-aishell3.int8.onnx   38MB    主模型 (int8量化)
├── lexicon.txt               1.9MB   词典 (66K+条)
├── tokens.txt                219行   音素表
├── phone.fst                 87KB    音素处理
├── number.fst                63KB    数字处理
├── date.fst                  58KB    日期处理
├── new_heteronym.fst         21KB    多音字处理
├── rule.far                  172MB   文本规则
└────────────────────────────────────
总计: ~213MB
```

### 3.2 下载命令

```bash
# 创建目录
mkdir -p models/vits-zh-aishell3

# 下载 (使用 hf-mirror.com 国内镜像)
BASE="https://hf-mirror.com/csukuangfj/vits-zh-aishell3/resolve/main"

aria2c -x 8 -s 8 -d models/vits-zh-aishell3 -o vits-aishell3.int8.onnx "$BASE/vits-aishell3.int8.onnx"
aria2c -x 8 -s 8 -d models/vits-zh-aishell3 -o lexicon.txt "$BASE/lexicon.txt"
aria2c -x 8 -s 8 -d models/vits-zh-aishell3 -o tokens.txt "$BASE/tokens.txt"
aria2c -x 8 -s 8 -d models/vits-zh-aishell3 -o phone.fst "$BASE/phone.fst"
aria2c -x 8 -s 8 -d models/vits-zh-aishell3 -o number.fst "$BASE/number.fst"
aria2c -x 8 -s 8 -d models/vits-zh-aishell3 -o date.fst "$BASE/date.fst"
aria2c -x 8 -s 8 -d models/vits-zh-aishell3 -o new_heteronym.fst "$BASE/new_heteronym.fst"
aria2c -x 8 -s 8 -d models/vits-zh-aishell3 -o rule.far "$BASE/rule.far"
```

### 3.3 验证

```bash
ls -lh models/vits-zh-aishell3/
# total 435488
# -rw-r--r--  58K  date.fst
# -rw-r--r-- 1.9M  lexicon.txt
# -rw-r--r--  21K  new_heteronym.fst
# -rw-r--r--  63K  number.fst
# -rw-r--r--  87K  phone.fst
# -rw-r--r-- 172M  rule.far
# -rw-r--r-- 1.6K  tokens.txt
# -rw-r--r--  38M  vits-aishell3.int8.onnx
```

## 4. 待完成事项

1. **查找 speaker_id ↔ gender 对应表**
   - AISHELL-3 数据集包含性别标注, 需下载 `spkrs.gender` 文件
   - 或从 sherpa-onnx 文档查找预设 ID

2. **代码集成**
   - 修改 TTS 引擎支持多说话人选择
   - 配置文件添加 speaker_id 字段

## 5. 参考资料

- [AISHELL-3 数据集](https://openslr.org/93/)
- [csukuangfj/vits-zh-aishell3 (HuggingFace)](https://huggingface.co/csukuangfj/vits-zh-aishell3)
- [sherpa-onnx TTS 模型列表](https://github.com/k2-fsa/sherpa-onnx/releases/tag/tts-models)
- [VITS 中文实现](https://github.com/csukuangfj/vits_chinese)