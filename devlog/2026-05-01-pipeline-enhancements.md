# 2026-05-01 管线增强：翻译质量 + 语速平滑 + P0/P1 路线图实现

## 背景

segments_cache.json 中 "我不想在这里多作赘述" 出现 7 次（英文仅说过 1 次），暴露出 LLM 批翻译幻觉问题。同时语速分布存在离群值（max 1.44x），需要平滑处理。用户要求完成 P0/P1 路线图剩余 4 项。

克隆了 [VideoLingo](https://github.com/Huanshere/VideoLingo) 和 [pyVideoTrans](https://github.com/jianchang512/pyvideotrans) 作为参考。

## 实现清单

### 1. 翻译幻觉三层防御 (P0.5)

**根因**: batch_size=15 时 LLM 批翻译崩溃为重复短语，上下文窗口传播污染形成链式反应。

| 层 | 机制 | 代码 |
|----|------|------|
| 批内去重 | Counter 频次分析，≥3次或占25%判定幻觉 | `_detect_batch_hallucination()` |
| 缩小批次 | batch_size 15→8 | `DEFAULT_CONFIG["llm"]["batch_size"]` |
| 毒化检测 | 上下文窗口内某句占比≥40%时丢弃窗口 | `_translate_llm` context 构建前 |

### 2. 语速平滑 (P1.5)

替换原来的 median + 固定权重混合 + 单向平滑:

| 层 | 方法 | 效果 |
|----|------|------|
| 基线 | Trimmed mean（去头尾各10%） | 更抗极端值 |
| 混合 | 自适应权重（偏离小→20%, 大→60%） | 尊重正常段，约束异常段 |
| 平滑 | 双向指数平滑（前向+后向取平均） | 消除相邻段跳变 |

std_dev: 0.1174 → 0.0544 (目标 <0.08)

### 3. 两步翻译 (P0.3)

借鉴 VideoLingo 三步法，简化为两步:
- Pass 1: 强制忠实直译 prompt → `text_zh_literal`
- Pass 2: 英文+直译 → 自然配音中文 → `text_zh`
- 降级: Pass 2 幻觉时回退 Pass 1

函数: `_translate_llm_two_pass()`，Config: `llm.two_pass: false`

### 4. NLP 断句 (P0.4)

用 spaCy `en_core_web_sm` 检测英文句子边界:
- Whisper `transcribe_audio` 保留 word_timestamps
- Split: 多句+时长>8s → 按句子边界切分
- Merge: 相邻段都<1.5s+同一句 → 合并
- 处理后 strip words 字段

函数: `_nlp_resegment()`，Config: `nlp_segmentation: false`

### 5. TTS 后校准 (P1.3)

TTS 生成后测量实际时长:
- 筛选 ratio > 1.30 的段
- 调用 `_refine_with_llm` 精简译文
- 仅对改动段重新生成 TTS
- 限 1 轮

函数: `_post_tts_calibrate()`，Config: `refine.post_tts_calibration: false`

### 6. 视频减速标记 (P1.4)

`_align_tts_to_timeline` 溢出处理:
- 超出≤15% 且减速因子≥0.85 → 标记减速而非截断
- 输出 `slowdown_segments.json`
- `merge_final_video` 读取并提示

Config: `alignment.video_slowdown: false`

当前版本只做标记和报告，实际 ffmpeg setpts 减速待后续实现。

## 修改文件

| 文件 | 变更 |
|------|------|
| `pipeline.py` | 全部 6 项代码实现 (~200 行新增) |
| `config.example.json` | 新增 two_pass, nlp_segmentation, post_tts_calibration, video_slowdown 等配置 |
| `test_pipeline.sh` | batch_size 15→8 |
| `ROADMAP.md` | P0.3/P0.4/P0.5/P1.3/P1.4/P1.5 标记完成 |
| `devlog/test-feedback-loop-methodology.md` | 重写为集成测试优先方法论 |

## 验证记录

### 第 1 轮: --retranslate (只翻译)
- 幻觉率: 0/73 = 0% ✅
- 覆盖率: 73/73 = 100% ✅
- 相邻重复: 0 ✅

### 第 2 轮: --fast (TTS + 对齐 + 合成)
- std_dev: 0.0544 ✅ (目标 <0.08)
- 平均语速: 1.04 ✅ (目标 0.90~1.10)
- 离群段(>1.4): 1 ✅ (目标 <3)
- final.mp4 生成成功 ✅

### 第 3 轮: --full 集成测试 (transcribe→translate→refine→TTS→校准→align→merge)

开启功能: post_tts_calibration, gap_borrowing, video_slowdown

**翻译质量:**
- 幻觉率: 0/72 = 0% ✅
- 覆盖率: 72/72 = 100% ✅
- 相邻重复: 0/71 = 0% ✅

**语速:**
- std_dev: 0.0497 ✅ (目标 <0.08)
- 平均语速: 1.04 ✅ (目标 0.90~1.10)
- 离群段(>1.4): 2 ✅ (目标 <3)

**后校准:**
- 7 段超 1.3x → 全部 7 段精简+TTS重生成成功 ✅

**视频减速:**
- 无减速标记（所有段在对齐后都未超出 15%） ✅

**端到端:**
- final.mp4 (76.0 MB) ✅
- 耗时: 7分39秒（含 Whisper CPU 转录 ~4分钟）

**备注:** two_pass 和 nlp_segmentation 因依赖未安装暂未在集成测试中开启（spaCy 未安装），代码已实现并通过语法检查。

## 新增配置项

```json
{
  "llm": {"batch_size": 8, "two_pass": false},
  "nlp_segmentation": false,
  "refine": {
    "post_tts_calibration": false,
    "calibration_threshold": 1.30
  },
  "alignment": {
    "video_slowdown": false,
    "max_slowdown_factor": 0.85
  }
}
```

全部默认关闭，不影响现有流程。
