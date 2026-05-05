# Sentence-Unit 流水线改造计划

> 制定日期: 2026-05-05
> 范围: pipeline.py 翻译 + TTS + 字幕子链
> 触发: phase2_translate.py 全文+DP 路径 7 轮实验天花板 align=0.508 + Phase 1 9 阶段重写质量退化

## 1. 目标

把流水线"对齐到 Whisper 73 个 segment"的硬需求降级为"对齐到 ~20 个 sentence unit"。
- 翻译质量好（一次性翻好，不再被 6 个 TTS 时长目标的阶段反复重写）
- TTS 对齐到句子/自然段落（用户实际可接受的粒度）
- 复用度高（segment 数据结构不变，下游 TTS / align / merge 自动适配）

## 2. 路径退役与资产复用

### 2.1 退役

`phase2_translate.py` 的"全文翻译 → 多候选 → DP 按 budget 切分"主路径归档退役：
- 实验 7 轮天花板 align=0.508，运行间方差 ±2-5 分
- 全文翻译的语序/详略分布与 segment 切分根本错配
- 对齐错位案例（段 10 讲四元数、切到的中文是"值得亲自体验"）无法靠 DP 修复

### 2.2 复用资产

| 来源 | 用途 |
|------|------|
| `phase2_translate.split_text_by_budgets` | Step 4：unit 内字幕子段切分（unit 已经语义对齐，DP 仅按字数贴合到子段，不会再产生段错位） |
| `phase2_translate.extract_budgets_jieba` | unit 时长校准 budget（含英文/数字/URL 段读得慢的修正），用作翻译字数区间提示 |
| `phase2_translate.CANDIDATE_CONFIGS` | Step 3 多温度多候选策略（T=0.3/0.5/0.7） |

phase2_translate.py 文件本身保留，主入口 main 加 deprecation 提示，但函数库继续被 import。

## 3. 4-Step 推进计划（TDD）

### Step 1 — unit 化合并（核心）

**新增**: `group_segments_to_units(segments, config) -> List[dict]` 在 pipeline.py 中。

**算法**:
1. 输入: Whisper 转录 segments（含 `start/end/text/words` 字段，words 可选）
2. 在原始 segment 序列上扫描，按以下规则合并到 unit:
   - **句末标点切**: 当前 segment 的 text 以 `.!?。！？` 结尾 → 关闭当前 unit
   - **句间静音切**: 相邻 segment 间静音 ≥ `min_unit_gap_ms`（默认 200ms）→ 关闭
   - **超长强制切**: 当前 unit 累计时长 ≥ `max_unit_duration`（默认 12s）→ 在最近的子句标点 `,;:` 强制切
   - **过短合并**: 如某 unit 时长 < `min_unit_duration`（默认 1.5s）→ 与时长更短的相邻 unit 合并
3. 输出: 每个 unit 含 `start/end/text`（拼接）、`words`（拼接）、`_unit_member_indices`（原始段索引列表，便于后续 TTS / 字幕回溯）

**接入点**: pipeline.py:5806 `segments = translate_segments(raw_segments, config)` 前，先 `raw_segments = group_segments_to_units(raw_segments, config)`。

**测试** (`tests/test_unit_grouping.py`):
- `test_句末标点合并` — `["A.", "B"]` → 1 unit "A. B"
- `test_句末停顿合并` — segment 间 gap < 200ms 时不切分
- `test_超长按子句切` — 单 segment 时长 14s 含逗号 → 在逗号切
- `test_短段合并` — 1s 单 segment + 邻段 → 合并
- `test_无_words_字段回退` — 仅按 text 标点合并

**预期产出**: 73 段 → 18-25 unit，平均 unit 时长 ~14s。

**验证**: 跑 `bash test_pipeline.sh --integrated`，看 `segments_cache.json`：
- 段数从 73 降到 18-25
- CPS 异常率（>6.0 或 <3.5）从 ~63% 降到 <20%

---

### Step 2 — 关掉 TTS 时长导向重写

**改动** (config + pipeline.py 默认开关):
- `llm.isometric` → 0（停掉 isometric_translate / expand_batch）
- `alignment.feedback_loop` → false（停掉 LLM 时长闭环）
- `refine.post_tts_calibration` → false
- TTS Phase 3 预检改写 → 加 config 开关 `alignment.pre_tts_text_adjust`，默认 false
- Pass 2 全局后校验 → 保留（防御性）
- `deduplicate_segments` / `merge_short_segments` → 保留

**接入**: 改 test_pipeline.sh 配置模板的 integrated/full 模式默认关掉这些开关。pipeline.py 入口处把 isometric 默认值翻转。

**测试**: 沿用现有 tests/test_two_pass_translation.py、test_translate_retry.py 等不变。新增 `tests/test_pre_tts_text_adjust_disabled.py` 验证 config 开关生效。

**验证**: 跑 integrated 模式，比较 `audit/two_pass_log.json` vs `audit/pre_tts_check_log.json`：后者应为空。

---

### Step 3 — 结构化 prompt + 逐 unit 字数

**改动** (pipeline.py 内 `_translate_llm_two_pass` 的 Pass 1 prompt 构造):

Prompt 模板:
```
你是英中视频配音翻译专家。下面 N 句英文请逐句翻译。

【硬要求】
1. 输出格式: [Un] (实际字数) 译文，n 与输入对应
2. 每句字数必须落入给定区间（来自 TTS 朗读时长，超出会音画不同步）
3. 自然中文，不照搬英文语序

【上下文】(不翻译，仅供理解)
前文: <前 1 unit 中文>
后文: <后 1 unit 英文>

【翻译任务】
[U1] (32-46字) <英文 1>
[U2] (28-40字) <英文 2>
...

请输出 N 行，每行 `[Un] (字数) 译文`。
```

**字数区间计算**: `target_chars = duration_sec * MS_PER_CHAR_RATE`，区间 `[target * 0.85, target * 1.15]`，使用 `extract_budgets_jieba` 的时长校准修正含英文/URL 段。

**多候选选优**:
- 不再跑 isometric_translate_batch
- Pass 1 直接生成 2 候选（T=0.3/0.5），按 (字数贴合 × 邻段非重复) 选优
- Pass 2 配音改编保留，但 Pass 2 prompt 也加每条字数区间约束

**测试** (`tests/test_unit_prompt.py`):
- `test_prompt_含每条字数区间`
- `test_prompt_含上下文段`
- `test_解析_Un_括号字数_译文_格式`
- `test_解析_缺漏编号回退原值`
- `test_选优按字数贴合度`

**验证**: 跑 integrated 模式，看 segments_cache.json：
- 字数命中率（落入 ±15% 区间）从 60% 提升到 ≥80%
- 翻译自然度（人工抽查 5 段）

---

### Step 4 — 字幕 unit 内分行

**改动**:
- 字幕生成器（subtitle 步骤）：每个 unit 中文按 `phase2_translate.split_text_by_budgets` 切到 ~12 字/行
- 字幕时间戳按字符数等比例分配到 unit 时间窗口
- 双语字幕：英文也按 unit 内的子段（用 word_timestamps 边界）

**测试** (`tests/test_subtitle_split.py`):
- `test_unit_内字幕分行`
- `test_单行不超过 N 字`
- `test_时间戳等比分配`
- `test_边界情况_unit_仅一句_无需分行`

**验证**: 看 subtitle_zh.srt：
- 每条字幕 ≤ 14 字
- 单条字幕时长 ≤ 6s
- 字幕语义连贯（不在虚词处切）

## 4. 端到端验证

按 test_pipeline.sh 跑五个模式（除 tts-only），每个模式都需 PASS：
- `--integrated`: 全功能，从翻译开始
- `--baseline`: 全部新功能关，对比基线
- `--fast`: 跳到 TTS+字幕，验证 cache 兼容
- `--refine`: 仅翻译+迭代
- `--retranslate`: 删除翻译缓存重跑

## 5. 不在本计划范围

- 多视频泛化测试（仅 zjMuIxRvygQ）
- TTS 引擎切换（保持 edge-tts → sherpa-onnx 链）
- 视频减速 / 间隙借用（已有逻辑保持）
- spaCy NLP 分句（unit 化已替代其用途，但保留代码不删）

## 6. 风险点与回退

| 风险 | 触发条件 | 回退 |
|------|---------|------|
| unit 化合并产生超长 unit (>15s) | 长段无标点 | 强制按 7s 切 |
| unit 化破坏现有 transcribe_cache.json | 仅当后续 step 期望 unit 化的 cache | unit 化在 translate 入口前重新跑，cache 仍是原始 73 段 |
| Pass 1 字数硬约束让 LLM 输出"凑字数"低质量 | 区间过紧 | 把区间放宽到 ±20% |
| 关掉 isometric 后高 CPS 段无 fallback | unit 内英文太密集 | atempo 0.95-1.05 兜底（已存在） |
