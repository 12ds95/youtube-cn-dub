# 参考项目分析

对比 VideoLingo 和 pyVideoTrans 的核心架构，提取可借鉴的设计思路。

## VideoLingo

**仓库:** https://github.com/Huanshere/VideoLingo

### 三步翻译 (Translate → Reflect → Adaptation)

核心文件: `core/translate_lines.py`, `core/_4_2_translate.py`, `core/prompts.py`

| 步骤 | 策略 | 我们的对应实现 |
|------|------|----------------|
| Step 1 Faithfulness | 逐行忠实直译，提供上下文+主题摘要+术语表 | `_translate_llm` (Pass 1 of two_pass) |
| Step 2 Expressiveness | 分析直译问题→修改建议→意译 | `_translate_llm_two_pass` (Pass 2 配音改编) |
| Step 3 Length trim | 超时长时 AI 精简字幕 | `_refine_with_llm` + `_post_tts_calibrate` |

**可借鉴:**
- 他们在 Step 2 包含"分析直译问题"的中间推理步骤，我们的 Pass 2 直接从直译改编，缺少显式反思环节
- `SequenceMatcher` 相似度校验 (< 0.9 报错)，我们可以在 two_pass 中检测 Pass 2 与 Pass 1 的语义偏离

### NLP 四层分句

核心文件: `core/_3_1_split_nlp.py`, `core/spacy_utils/split_by_*.py`

| 层 | 策略 | 我们的对应 |
|----|------|-----------|
| 1. split_by_mark | 按句号/dash/ellipsis 分句 | `_nlp_resegment` Pass 1: spaCy sents |
| 2. split_by_comma | 逗号右侧有完整主谓(≥3词)才切 | 未实现 |
| 3. split_by_connector | 按 that/which/because 等连词拆 | 未实现 |
| 4. split_long_by_root | DP 找最佳切分点，超长句60token强切 | 我们用 duration>8s 触发 |

**可借鉴:**
- 他们的分句更精细(4层)，我们只用 spaCy 基础句子检测 + 时长阈值
- 按逗号/连词切分可以减少单段过长问题
- DP 最佳切分点是更优方案（我们目前按 word timestamp 顺序切）

### TTS 时长匹配

核心文件: `core/tts_backend/estimate_duration.py`, `core/_10_gen_audio.py`

- `AdvancedSyllableEstimator`: 基于音节数估算时长 (中文 0.21s/音节，英文 0.225s/音节)
- 翻译后即预检时长，超长时调 GPT 精简
- 音频生成时按 chunk 计算 speed_factor，用 ffmpeg atempo

**可借鉴:**
- 音节级时长估算比我们的字符级(4.5字/秒)更精确
- 翻译后立即预检时长 → 精简，避免到 TTS 阶段才发现超速

---

## pyVideoTrans

**仓库:** https://github.com/jianchang512/pyvideotrans

### 多角色配音

核心文件: `videotrans/task/_dubbing.py`, `videotrans/process/prepare_audio.py`

| 组件 | 策略 | 我们是否需要 |
|------|------|-------------|
| 说话人分离 | pyannote / sherpa_onnx / ali_CAM | 暂不需要(单人视频) |
| 逐行角色分配 | line_number → role mapping | 未来可扩展 |
| 声音克隆 | F5-TTS / CosyVoice 从原视频提取参考音频 | 已支持 CosyVoice |
| 队列化处理 | queue_tts 按字幕逐条处理 | 我们用并发 batch |

### 音视频同步 (SpeedRate)

核心文件: `videotrans/task/_rate.py`

三种同步策略:
1. **纯音频加速**: Rubberband 时间拉伸
2. **纯视频减速**: FFmpeg setpts
3. **混合**: ratio > 1.2x 时，50/50 分摊音频加速+视频减速

**可借鉴:**
- **Rubberband 替代 atempo**: 更高质量的时间拉伸(保持音调)
- **混合策略**: 我们目前只有"音频加速 OR 视频减速"二选一，他们的 50/50 分摊更平衡
- **静音间隙移除**: 压缩字幕间的空白时间

### 翻译

- 支持完整 SRT 格式批量送入 LLM (提供时间轴上下文)
- MD5 缓存避免重复翻译
- LLM 重分句: 调用 ChatGPT 基于语音时长重新切分字幕

**可借鉴:**
- SRT 格式输入保留了时间轴信息，帮助 LLM 理解段间关系
- LLM 重分句是 NLP 分句的替代方案，无需 spaCy 依赖

---

## 对我们项目的启示（按优先级）

| 优先级 | 改进点 | 来源 | 复杂度 |
|--------|--------|------|--------|
| P1 | 音节级时长估算替代字符级 | VideoLingo | 中 |
| P2 | two_pass 增加反思环节 | VideoLingo | 低 |
| P2 | 混合音视频同步策略 | pyVideoTrans | 中 |
| P3 | 更精细的 NLP 分句(逗号/连词层) | VideoLingo | 高 |
| P3 | Rubberband 替代 ffmpeg atempo | pyVideoTrans | 低 |
| P4 | 多角色配音支持 | pyVideoTrans | 高 |
| P4 | LLM 重分句替代 spaCy | pyVideoTrans | 中 |
