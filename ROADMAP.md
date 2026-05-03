# 功能路线图 (Roadmap)

> 项目定位：个人学习英文视频转中文配音工具，本机运行（macOS, CPU 优先），目标最大化转换后视频体验。

调研了 8 个开源项目的翻译、音频、优化、测试能力，提炼可借鉴特性，按优先级分 4 阶段规划。

## 参考项目

| 项目 | Stars | 借鉴方向 |
|------|-------|---------|
| [pyVideoTrans](https://github.com/jianchang512/pyvideotrans) | 17.2k | 配音简洁提示词、双重速度调节、外部模板 |
| [VideoLingo](https://github.com/Huanshere/VideoLingo) | 16.9k | 两步翻译、预翻译摘要+术语、NLP 分句、间隙借用 |
| [KrillinAI](https://github.com/krillinai/KrillinAI) | — | 滑动上下文窗口(3+3)、反幻觉规则、能量静音检测 |
| [ClearerVoice-Studio](https://github.com/modelscope/ClearerVoice-Studio) | — | MossFormer2 语音增强、带宽扩展(16k→48k) |
| [Voice-Pro](https://github.com/abus-aikorea/voice-pro) | — | F5-TTS/CosyVoice 声音克隆、UVR5 分离 |
| [Bluez-Dubbing](https://github.com/mfacecern/bluez-dubbing) | — | 单元+集成测试、服务注册模式 |
| [WhisperX](https://github.com/m-bain/whisperX) | 21.6k | 词级时间戳、内置 diarization |
| [pyannote-audio](https://github.com/pyannote/pyannote-audio) | 9.9k | 说话人分轨底座 |

---

## P0: 翻译质量增强

### P0.1 滑动上下文窗口 ✅

**来源**: KrillinAI (3前+3后) | **复杂度**: 低

- 回看扩展到 6 句（原 4 句）
- 新增前瞻 3 句英文预览
- 注入反幻觉指令："前文和下文仅供理解语境，翻译只针对当前批次编号内容"

### P0.2 外部提示词模板 ✅

**来源**: pyVideoTrans | **复杂度**: 低

- Config: `llm.prompt_template` 指向 `.txt` 模板文件
- 内置模板: `prompts/default.txt`, `prompts/dubbing_concise.txt`
- 模板变量支持，模板不存在时回退内置 prompt

### P0.3 两步翻译法（忠实→表达）✅

**来源**: VideoLingo 三步法简化 | **复杂度**: 中 | **Config**: `llm.two_pass: false`

- Pass 1（忠实）: 强制 prompt "逐句忠实翻译，保留所有信息点，不遗漏不添加" → `text_zh_literal`
- Pass 2（表达）: 输入 EN+直译，prompt "改写为适合配音朗读的自然中文" → `text_zh`
- 幻觉检测: Pass 2 检测到幻觉时自动回退到 Pass 1 直译结果
- 默认关闭，避免 API 费用翻倍
- **实现**: `_translate_llm_two_pass()` 函数

### P0.4 NLP 混合分句优化 ✅

**来源**: VideoLingo (spaCy) | **复杂度**: 中  
**新增依赖**: `spacy` + `en_core_web_sm` (~12MB)  
**Config**: `nlp_segmentation: false`

- 转录后保留 Whisper word_timestamps（`transcribe_audio` 输出含 `words` 字段）
- 拆分: segment 含多句 AND 时长 > 8s → 利用 word_timestamps 按 spaCy 句子边界拆分
- 合并: 相邻 segment < 1.5s 且属同一句子 → 合并
- 处理完自动 strip words 字段，不写入缓存
- **实现**: `_nlp_resegment()` 函数，集成于 `deduplicate_segments` 之后、翻译之前

### P0.5 翻译幻觉三层防御 ✅

**来源**: 自研，针对 LLM 批翻译崩溃问题 | **复杂度**: 中

**根因**: batch_size=15 时 LLM 对弱语义段崩溃为重复短语（如"我不想在这里多作赘述" ×7），且上下文窗口传播污染。

三层防御:
1. **批内去重检测** `_detect_batch_hallucination`: Counter 频次分析，同一译文 ≥3 次或占批次 25% 判定幻觉，清空走逐条重译
2. **缩小批次**: `batch_size` 15 → 8，减少 LLM 长上下文崩溃概率
3. **上下文窗口毒化检测**: 窗口内某句话重复占比 ≥40% 时丢弃整个窗口，阻断错误传播

**验证**: 73 段完整重翻译测试，幻觉率 0%，覆盖率 100%。

### P0.6 Phase 2 全文翻译质量优化 (实验中)

**来源**: 自研 | **复杂度**: 高 | **脚本**: `phase2_translate.py`

**问题**: 分段翻译 + refine 迭代导致翻译质量受限——跨段污染、上下文丢失、等时压缩加剧偏移。Phase 1 的副产品是每段中文字数的 ground truth（TTS 时长已验证）。

**v0 基线方案**: 全文连贯翻译 → 5 候选（不同 temperature/风格）→ DP 按字数 budget 最优切分

**v0 实验结论** (测试视频 zjMuIxRvygQ, 73 段):
- DP 切分精度极高：MAE=0.6字, 73 段 100% 在 ±2 字内
- 全文翻译质量明显更连贯自然
- **关键问题: 语义错位** — 纯按字数切分无法保证英中段对齐，全文翻译语序/详略与分段不同

**待探索的 4 个改进方向** (详见 `docs/research/2026-06-08-phase2-fulltext-translation-roadmap.md`):
1. Prompt 段边界标记（`|||`）
2. 英中 Sentence Alignment（embedding + DTW）
3. 两步法（全文翻译 + LLM 分句）
4. 混合策略（粗粒度标记 + 细粒度 DP）

**下一步**: 积累 5+ 视频数据后系统评估各方向。

---

## P1: 音频/时间轴质量增强

### P1.1 能量静音检测 ✅

**来源**: KrillinAI | **复杂度**: 低

- `_detect_silence_regions()`: pydub 检测静音区间
- `_is_in_silence()`: 确认间隙是真正静音后才允许借用

### P1.2 间隙借用 (Gap Borrowing) ✅

**来源**: VideoLingo | **复杂度**: 中  
**Config**: `alignment: {gap_borrowing: false, max_borrow_ms: 300}`

- TTS 稍超目标时长时，从相邻静音间隙借用时间而非截断
- 条件: 溢出 ≤ max_borrow_ms + 间隙足够(60%) + 确认静音
- 统计记录到 `speed_report.json`

### P1.3 TTS 后校准 (Post-TTS Calibration) ✅

**来源**: pyVideoTrans（"承认现实"策略） | **复杂度**: 中  
**Config**: `refine: {post_tts_calibration: false, calibration_threshold: 1.30}`

- TTS 全部生成后用 pydub 测量每段实际时长 vs 目标时长
- 筛选 ratio > calibration_threshold 的段 → 调用 `_refine_with_llm` 精简译文
- 仅对改动段重新生成 TTS（edge-tts），不影响其他段
- 限 1 轮校准，避免无限循环
- **实现**: `_post_tts_calibrate()` async 函数，集成于 TTS 生成后、时间线对齐前

### P1.4 极端情况视频减速 ✅

**来源**: pyVideoTrans | **复杂度**: 高  
**Config**: `alignment: {video_slowdown: false, max_slowdown_factor: 0.85}`

- 在 `_align_tts_to_timeline` 溢出处理中: 超出 ≤15% 且减速因子 ≥0.85 时，标记视频减速而非截断音频
- 输出 `slowdown_segments.json` 记录 index/时间范围/减速因子
- `merge_final_video` 读取报告并提示
- 当前版本: 标记+报告（实际 ffmpeg setpts 逐段减速待后续实现）

### P1.5 语速平滑优化 ✅

**来源**: 自研 | **复杂度**: 中

三级平滑方案:
1. **Trimmed mean 基线**: 去掉头尾各 10% 极端值再取均值
2. **自适应混合权重**: 偏离小(<0.15)→20%权重，偏离大(>0.3)→60%权重，中间→40%
3. **双向指数平滑**: 前向+后向各平滑一遍，取平均消除相邻段跳变

**验证**: 标准差 0.1174(raw) → 0.0544(smoothed)，目标 <0.08 通过。

---

## P2: 多角色配音

### P2.1 管线基础设施改造（无需新依赖）

1. Config 新增 `speaker_diarization` 配置块
2. Segment 结构添加 `speaker` 字段（向后兼容）
3. Speaker 感知的 merge/dedup — 不同 speaker 不合并/不去重
4. 多音色 TTS — `_resolve_segment_voice()` 按 speaker 分组合成
5. 字幕 speaker 标签

### P2.2 pyannote 说话人分轨集成

**新增依赖**: `pyannote-audio`（需 HuggingFace token）

1. `diarize_audio()` — pyannote speaker-diarization-3.1, CPU 模式
2. `assign_speakers_to_segments()` — 最大时间重叠匹配
3. 管线集成 — 转录后、翻译前，支持 `skip_steps: ["diarize"]`
4. 优先用分离后人声 `audio_vocals.wav`

### P2.3 高级特性（后续）

- 声音克隆（CosyVoice/F5-TTS 零样本，来源 Voice-Pro）
- 性别自动匹配（speaker embedding 判性别）
- WhisperX 替换 faster-whisper（词级时间戳 + 内置 diarization）
- Speaker 感知翻译（注入角色信息保持语气差异）

---

## P3: 高级功能

### P3.1 TTS 语音增强

**来源**: ClearerVoice-Studio (MossFormer2) | **复杂度**: 中  
**Config**: `audio_enhance: {enabled: false, method: "noisereduce", target_sr: 44100}`

渐进路径:
- A: `noisereduce` 降噪 + 提升采样率到 44.1kHz（轻量）
- B: `speechbrain` 预训练增强模型（CPU，~200MB）
- C: ClearerVoice MossFormer2（最优质量）

### P3.2 声音克隆

**来源**: Voice-Pro | **复杂度**: 高 | **依赖 P2**

- 扩展 `CosyVoiceEngine` 支持 zero-shot 模式
- 新增 `F5TTSEngine`
- 从分离人声自动选取参考片段

### P3.3 模块化重构

**来源**: Bluez-Dubbing | 当前 pipeline.py ~4100 行单文件

```
youtube_cn_dub/
├── config.py        # DEFAULT_CONFIG, load_config
├── download.py      # download_video
├── audio.py         # extract_audio, separate_audio
├── transcribe.py    # transcribe_audio, merge/dedup
├── translate.py     # _detect_translation_style, _translate_llm
├── refine.py        # run_refinement_loop, expand, refine
├── tts/             # base.py + 各引擎
├── align.py         # _align_tts_to_timeline
├── subtitle.py      # generate_srt_files
├── merge.py         # merge_final_video
└── pipeline.py      # process_video 编排器
```

---

## 测试增强策略

**来源**: Bluez-Dubbing, 现有 tests/ 11 个文件

| 计划 | 内容 |
|------|------|
| pytest 统一管理 | `conftest.py` 共享 fixture（mock LLM、示例音频） |
| P0 测试 | 验证上下文窗口构建、prompt 模板、两步翻译、幻觉防御 |
| P1 测试 | mock 已知时长音频验证 gap borrowing、速度 clamp、后校准 |
| P2 测试 | mock diarization 输出验证 speaker 分配 |
| 回归测试 | golden-file 对比已知输入的翻译输出 |
| Makefile | `make test` = unit + integration |

---

## 依赖汇总

| Phase | 新增依赖 | 大小 | 必需? |
|-------|---------|------|-------|
| P0.4 | `spacy` + `en_core_web_sm` | ~12MB | 可选 |
| P2.2 | `pyannote-audio` | ~1.5GB model | 可选 |
| P3.1 | `noisereduce` 或 `speechbrain` | ~200MB | 可选 |
| P3.2 | `f5-tts` 或 `cosyvoice` | ~2-4GB | 可选 |
| 测试 | `pytest`, `pytest-asyncio` | 轻量 | 开发 |

---

## 实施顺序

```
P0.1 滑动上下文窗口 ✅  ─┐
P0.2 外部提示词模板 ✅   ├─→ P0.3 两步翻译 ✅  ─→ P0.4 NLP分句 ✅
P0.5 幻觉三层防御 ✅      │
                          │
P1.1 能量静音检测 ✅ ──→ P1.2 间隙借用 ✅ ──→ P1.3 TTS后校准 ✅ ──→ P1.4 视频减速 ✅
P1.5 语速平滑 ✅                                │
                                                 │
P2.1 多角色基础设施 ──→ P2.2 pyannote 集成 ────┘
                                                 │
P3.1 语音增强 ──→ P3.2 声音克隆（依赖 P2）
P3.3 模块化重构（P1 完成后最佳）
```
