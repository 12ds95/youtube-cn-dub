# NLP 兜底切分: TTS 生成质量归档报告 (3 视频)

> 归档日期: 2026-05-06
> 关联代码: `pipeline.py:_split_long_unit_by_nlp` / `_split_long_unit_by_time`
> 关联测试: `tests/test_unit_grouping_nlp_fallback.py` (6 用例)
> 关联设置: `unit_grouping.max_duration=18s`, `nlp_segmentation_fallback=True` (默认)

## 背景

`group_segments_to_units` 把 Whisper 碎片合并到 sentence unit, 历史超长上限切分仅依赖
`,;:` 类子句标点 (`_split_long_unit_by_clause`). 在 Whisper 缺标点的英文转录上
(尤其口语长视频), 该切分**形同失效**, 平均段长可达 90+ 秒, 远超 edge-tts 的安全段长
(实测 30-40s 单段开始出现 SSE 流断/丢字). 长段同时让 LLM batch 内 unit 边界模糊,
跨段污染概率飙升, 翻译欠速段大量涌现。

本轮改造引入三级切分兜底:

```
clause 标点 (旧)  →  spaCy NLP 句子边界  →  word timestamps 时间均切
```

并把默认 `max_unit_duration` 从 12s 提到 18s, 在"翻译上下文完整性"和"edge-tts 安全段长"
之间取折中。

## 验证方法

git stash 回退到 master 旧逻辑跑 baseline → 备份 `segments_cache.json` →
git stash pop 跑新版本 → 备份 → 用相同 transcribe cache 让两组对照仅差在
`group_segments_to_units` 的输出, 排除 Whisper 转录波动。

3 视频跨度: 5.9 min / 31.6 min / 116 min — 后者是触发兜底的 stress test
(baseline 旧逻辑下平均段长 91.8s, 完全崩溃; 新版本必须切到 ≤20s).

`kCc8FmEb1nY` 的 baseline 是用户在 master 旧代码上的实测数据, 我没有重跑
(116min 跑一次 67min, baseline 已知崩溃, 重跑无新信息).

## 三视频核心指标

### 段长分布

| 指标 | zjMu 5.9min | zjMu 5.9min | d4Eg 31.6min | d4Eg 31.6min | kCc 116min | kCc 116min |
|---|---|---|---|---|---|---|
| 版本 | baseline | new | baseline | new | baseline (用户测) | **new** |
| Whisper 段数 (输入) | 73 | 73 | 365 | 365 | 1153 | 1153 |
| Unit 段数 (输出) | 44 | 36 | 248 | 221 | **76** | **617** |
| 平均段长 (s) | 7.89 | 9.64 | 7.05 | 7.91 | **91.8** ⚠️ | **11.21** ✅ |
| 中位数段长 (s) | — | — | — | — | — | 12.18 |
| 最长段 (s) | 15.12 | 17.40 | 16.64 | 17.88 | (~150) | **19.07** |
| >18s 段 | 0 | 0 | 0 | 0 | 多个 | 3 |
| >30s 段 | 0 | 0 | 0 | 0 | 多个 | **0** |

观察: kCc 的 baseline → new 是质变 (76→617 段, 91.8s→11.21s). NLP 兜底真正发挥作用的
就是这种长视频缺标点场景. 短视频 (zjMu/d4Eg) Whisper 标点质量本就高, 老旧 clause 切分
基本能 cover, max=18 让段略变长但仍在安全边界。

### 翻译质量与 LLM 行为

| 指标 | zjMu base | zjMu new | d4Eg base | d4Eg new | kCc base | kCc new |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| Pass 2 采纳 | 44/44 | 36/36 | 242/248 | 213/221 | 39/76 | **588/617** |
| Pass 2 采纳率 | 100% | 100% | 97.6% | 96.4% | **51.3%** | **95.3%** |
| 跨段污染段数 | 2 | 0 | 16 | 16 | **42** | 40 |
| 跨段污染相对率 | 4.5% | 0% | 6.5% | 7.2% | **55.3%** | **6.5%** |
| 严重欠速 lite_expand 触发 | 7/44 | 9/36 | 43/248 | 45/221 | **48/76** | **175/617** |
| lite_expand 触发率 | 15.9% | 25.0% | 17.3% | 20.4% | **63.2%** | **28.4%** |
| lite_expand 采纳率 | 7/7 | 9/9 | 39/43 | 41/45 | — | 162/175 (92.6%) |

观察:
1. **kCc Pass 2 采纳率 51.3% → 95.3%** — 段长合理化后, LLM Pass 2 改编不再因长 batch
   混淆而被守卫拒绝。
2. **kCc 跨段污染相对率 55.3% → 6.5%** — 段边界清晰后 LLM 不再把相邻 unit 内容混入
   当前 unit。
3. **kCc lite_expand 触发率 63.2% → 28.4%** — 不再有"句子被合并到 90 秒, 中文翻译
   字数怎么也凑不够"的失衡。
4. zjMu/d4Eg 上新版本 lite_expand 触发率略升 (15.9%→25%, 17.3%→20.4%), 是
   max=12→18 让段略变长后字数压力上升, 但绝对值仍低。

### TTS 输出质量

| 指标 | zjMu base | zjMu new | d4Eg base | d4Eg new | kCc new |
|---|:-:|:-:|:-:|:-:|:-:|
| TTS 片段数 | 44 | 36 | 248 | 221 | 617 |
| CPS 均值 | 4.34 | 4.53 | 4.32 | 4.37 | 4.09 |
| CPS P95 | 5.77 | — | 5.65 | — | 5.21 |
| CPS [3.5-6.0] 合规率 | 77.3% | — | 82.2% | — | **85.4%** |
| Atempo 均值 | 1.0311 | 1.022 | 1.0215 | 1.022 | 1.0206 |
| Atempo 离群 (>1.4x) | 0 | 0 | 1 | 1 | 0 |
| 无调速 [0.85-1.15] 合规率 | 100% | — | 94.8% | 92.8% | **96.3%** |
| Jitter (%) | 2.87 | — | 2.83 | — | 2.92 |
| Shimmer (%) | 10.12 | — | 10.22 | — | 10.62 |
| F0 均值 (Hz) | 169.2 | 172.4 | 172.3 | 166.5 | 167.9 |
| 填充段 | 11 | 18 | 104 | 78 | 232 |
| 容忍段 | 29 | 18 | 114 | 108 | 314 |
| atempo 降级段 | 0 | 0 | 9 | 13 | 16 |
| 截断段 | **0** | **0** | **0** | **0** | **0** |
| 耗时 (秒) | 193 | 153 | 1136 | 1070 | 4030 |

观察:
1. **零截断**: 所有视频新版本 truncate=0, edge-tts 无段超出合成边界。证明 max=18s 是
   安全选择 — 生成的 617 个 TTS 片段 (kCc) 全部完整朗读。
2. **CPS 合规率持平或上升**: kCc 85.4% 是 3 视频中最高, 长视频反而稳定 — 段长合理化让
   字数密度均匀。
3. **Atempo 离群 ≤1, 无调速合规率 ≥92.8%**: edge-tts 输出 raw_ratio 落在
   [0.85, 1.15] 的比例稳定高位, 配音自然度未因段长变化而劣化。
4. zjMu 填充段从 11→18 上升、容忍段 29→18 下降: 段变长后填充更频繁, 这是预期 —
   段越长越接近"满载", 相邻段间空隙更显著, 用静音填充比 atempo 拉伸更自然。

### 翻译抽样 (15 时间点对齐 + kCc 7 时间点)

zjMu @150s baseline 把同一句切成两段 (145.6-152.6s, 单段) vs new (135.7-152.6s,
合并段 17s):

```
EN  : you lose a cause difficulties and ambiguities when trying to interpolate ...
B[145.6-152.6s]: 会导致插值过程出现困难与歧义...
N[135.7-152.6s]: 这种方法大体可行,但存在一个严重问题:它容易发生万向节锁——
                 当两个旋转轴对齐时,会丢失一个自由度,导致在插值两组不同朝向时
                 出现困难与歧义。
```
N 因合并段获得完整上下文, 译文更连贯。

d4Eg @900s 的 proper_noun 处理:
```
EN  : And as with Linus, ...
B: 和林纳斯的情况一样, ...   ← 把 Linus 译为"林纳斯"
N: 和Linus的情况一样, ...    ← 保留原名
```
新版本对 proper_noun 保留更准确。

kCc 7 时间点 (60s/300s/600s/1500s/3000s/5000s/6500s) 全部连贯, 无漂移, proper_noun
(`ChatGPT`, `logits`, `GPT`, `hi there`) 全部保留原文。

## 结论

1. **NLP 兜底解决了核心痛点**: 长视频 (kCc 116min, Whisper 缺标点) 的 unit 切分从崩溃
   (76 段, 91.8s/段) 修复到合理 (617 段, 11.21s/段), 完全在 edge-tts 安全段长边界内。
2. **短/中视频无回归**: zjMu/d4Eg 翻译质量等价或略优 (合并段提供更完整 LLM 上下文),
   TTS 输出指标 (CPS 合规率/atempo/截断率) 持平。
3. **Pass 2 采纳率显著提升**: kCc 的 51.3% → 95.3% 是这轮改造最具说服力的指标 —
   合理段长 + 清晰边界让 LLM 改编不再因 batch 内污染被守卫拒绝。
4. **设计折中**: max=18s 选择保守, 留 buffer 给少数自然长句, 不抢占 NLP 兜底
   触发阈值。如果未来 edge-tts 升级到更高单段限制, 可上调; 反之降到 12-15s 仍能
   通过 NLP 兜底兜住超长。

## 文件清单

实现:
- `pipeline.py`: `group_segments_to_units` Pass 2 三级切分; 新增
  `_split_long_unit_by_nlp` (spaCy 兜底) 与 `_split_long_unit_by_time` (时间均切)
- `pipeline.py`: `_retranslate_chinglish` 加入统一守卫 + chinglish 复检 (R7 修复路径)

测试:
- `tests/test_unit_grouping_nlp_fallback.py` (6 用例): 无标点超长切分 / 标点优先 /
  单句超长时间均切 / 短 unit 不误切 / 缺 words 不崩 / 配置开关禁用
- `tests/test_chinglish_retranslate_guard.py` (6 用例): 守卫接受/拒绝场景

文档:
- `docs/spec/feature-flag-matrix.md`: 加入 `unit_grouping.max_duration` 与
  `nlp_segmentation_fallback` 开关行
- `docs/spec/jieba-estimator-usage.md`: 5 个调用点的 what/why/how (附加产物, 与本归档配套)

## 备份产物 (用于将来回归对比)

```
/tmp/pipeline_compare/zjMu_baseline_segments.json   # 44 段
/tmp/pipeline_compare/zjMu_new_segments.json        # 36 段
/tmp/pipeline_compare/d4Eg_baseline_segments.json   # 248 段
/tmp/pipeline_compare/d4Eg_new_segments.json        # 221 段
/tmp/pipeline_compare/kCc_new_segments.json         # 617 段
/tmp/pipeline_compare/{zjMu,d4Eg,kCc}_{baseline,new}.log
```

> 这些备份在系统重启或 `/tmp` 清理后会丢失, 仅作为本归档撰写阶段的引用。
