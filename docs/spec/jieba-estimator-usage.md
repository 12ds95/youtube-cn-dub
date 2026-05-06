# jieba 时长估算器在 `--integrated` 流程中的使用 (what / why / how)

> 范围: `bash test_pipeline.sh --integrated` 单视频回归路径
> (skip_steps = `download / extract / separate / transcribe`)
> 更新: 2026-05-06

## 模型概览

模块 `duration_estimator.estimate_duration(text_zh) -> float (ms)`,
基于 Ridge v2 校准 (6 视频 / 3009 样本 / alpha=50 / R²=0.92):

| 类别 | 校准值 |
|---|---|
| 单字词 (的/是) | 138 ms |
| 双字词 (今天/学习) | 361 ms |
| 三字词 (计算机) | 506 ms |
| 四字及以上 | 223 ms / 字 |
| 英文字母 | 31 ms / 字符 |
| 数字 | 311 ms / 字符 |
| URL 字符 (逐字母朗读) | 16 ms / 字符 |
| 标点停顿 | 197 ms |
| 截距 | +1210 ms |

派生量: `text_utils._jieba_global_ms_per_char()` 用 50 字标准模板取得**全局 ms/字 ≈ 215.9 ms** (lazy-cached),
作为没有 sample 时的反向 char-range 计算系数。

## 5 个生效调用点

`--integrated` 跑 `transcribe` 缓存 → translate → TTS → (subtitle/merge 已 skip)。
jieba estimator 在这条路径上有 **5 处生效**:

| # | 阶段 | 文件:行 | 输入 | 用途 (what) | 动机 (why) |
|---|---|---|---|---|---|
| 1 | Pass 1 prompt | `pipeline.py:2565` (`build_unit_translation_lines`) | `dur_sec` (无中文样本) | 用全局 ms/字 反推 `(lo-hi 字)`, 内联到 `[N] (X-Y字) <英文>` 行 | 让 LLM 在生成时即看到目标字数, 强于事后软约束 |
| 2 | Pass 2 prompt | `pipeline.py:2545` (`build_pass2_lines`) | `dur_sec + Pass-1 中文样本` | 用 `sample_zh` 反向估算每段实际 ms/字 → 更精确 `(lo-hi 字)` | Pass 2 已有真实 Pass-1 草稿, 反向算更贴该段语速, 防 Pass 2 改编时长度漂移 |
| 3 | TTS rate 计算 | `pipeline.py:3187` | `text_zh` (最终译文) | `estimated_tts_ms / target_dur_ms = raw_ratio` → 决定 edge-tts `rate` (`[0.80, 1.35]` 截断) + 是否走 atempo fallback | 比纯字符计数准, 因为它考虑词粒度/标点/英文/URL/数字 |
| 4 | TTS feedback-loop 候选打分 | `pipeline.py:5611` (`_select_best_candidate`) | 各候选译文 | 估时长 → 与 `target_ms` 比 → 选 `ratio` 最接近 1.0 的候选 | feedback_loop 重新生成多候选时, jieba 估算用作"试听前的代价函数" |
| 5 | lite_expand 触发与选择 | `pipeline.py:4145` (`_identify_severely_underslow_segments`) + `pipeline.py:5874` (`_lite_expand_underslow`) | `text_zh` 与 `target_ms` | (a) 触发条件: `估算 CPS < 3.3` 或 `est_ms / target_ms < 0.72` ⇒ 拉入扩展队列; (b) 6 候选打分 (复用 `_select_best_candidate(mode='fill')`) | 严重欠速段需补字; jieba 估算同时承担"识别"和"选优" |

> 在 `--integrated` 中, 还有几处调用点不会触发:
> `_refine_with_llm` / `post_tts_calibration` / `pre_tts_text_adjust` / `isometric` 路径默认关闭
> (见 `docs/spec/feature-flag-matrix.md`)。

## 数据流

```
原始 segments (transcribe cache)
  │
  └─ Pass 1 prompt 注入 (lo,hi 字)            ← 用例 #1: 全局 ms/字
      │
      └─ Pass 1 中文 (用作 Pass 2 sample)
          │
          └─ Pass 2 prompt 注入 (lo,hi 字)    ← 用例 #2: per-seg 反向估算
              │
              └─ Pass 2 中文 → segments[i].text_zh
                  │
                  ├─ low_cps 扫描 (CPS<3.3 或 ratio<0.72)  ← 用例 #5(a)
                  │     │
                  │     └─ _lite_expand_underslow → 6 候选
                  │           │
                  │           └─ _select_best_candidate     ← 用例 #5(b)/#4
                  │
                  └─ TTS 阶段
                        │
                        └─ rate = f(est_ms / target_ms)    ← 用例 #3
```

## How (调用契约)

- 输入: 任意 `str` (中文为主, 含英文/数字/URL/标点).
- 输出: `float` 毫秒. 永远 `>= 0` (含 `INTERCEPT` 1210 ms 截距, 即使空串).
- 副作用: 无. jieba 第一次调用会触发 dict 加载 (~0.5 s), 后续是缓存. 适合在循环里频繁调用.
- 校准前提: 该参数体系是 **edge-tts zh-CN-YunxiNeural rate=1.0** 的拟合; 换 voice / 换引擎前需要重新跑 `calibrate_tts_duration.py`.

## 边界 / 已知限制

- **截距 1210 ms** 是模型整段的常数偏置, 极短 (≤2 字) 译文估算偏长, 但管线在 `_identify_severely_underslow_segments` 里有 `target_ms<=500` 的 short-circuit, 不会误触发 lite_expand.
- 反向求字数 (用例 #2) 假设 sample 与目标语速相近; 当 Pass-1 因严重欠速被拉入 lite_expand 时, 反向估出的 ms/字 偏高, 这是已知 bias, 由用例 #5(a) 的 ratio 双条件兜底.
- URL 估算是逐字母 16 ms/字符 (TTS 朗读 "h-t-t-p-..."), 假设译文不会出现长 URL — 这与 `text_for_tts` 的 URL 处理策略一致.
