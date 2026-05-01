# 测试→反馈→修复 循环方法论

## 核心原则

**永远做端到端集成测试，所有新功能同时开启**。单个功能分别通过不等于组合后也能通过——翻译质量、分句、TTS、语速平滑、后校准、间隙借用必须在同一次运行中协同验证。

以 `bash test_pipeline.sh --fast` 为主测试循环入口，执行"测试→分析→修复→测试"闭环，直到所有指标通过或达到最大迭代次数。

**不跑 Whisper**。转录结果稳定，每次重跑浪费时间。始终复用 `transcribe_cache.json`。

## 测试模式

### 主测试（日常迭代，验收标准）
```bash
# 跳过 Whisper，复用 transcribe_cache.json（转录结果稳定，不浪费时间）
# 全功能端到端开启: two_pass + nlp_segmentation + 幻觉检测 + refine +
#   TTS + 后校准 + 间隙借用 + 视频减速 + 字幕 + 合成
bash test_pipeline.sh --fast
```
这是最常用的验收命令。**所有新功能同时开启**，从翻译到最终合成全链路验证。

**不测 `--full`（太慢）**。`--full` 包含 Whisper 转录，单次耗时过长且转录结果已稳定。日常迭代始终用 `--fast`。

### 翻译集成测试（改了翻译代码时用）
```bash
# 跳过 Whisper，从翻译开始跑完全链路
# 全功能开启: two_pass + nlp_segmentation + 幻觉检测 + refine + TTS + ...
bash test_pipeline.sh --integrated
```
改了翻译逻辑（两步翻译、幻觉检测、批对齐、上下文窗口等）时用这个。

### 辅助测试（定位问题用）
```bash
bash test_pipeline.sh --retranslate  # 只跑翻译，不跑 TTS 后步骤，快速定位翻译质量问题
bash test_pipeline.sh --refine       # 只跑迭代优化，定位精简/扩展逻辑问题
bash test_pipeline.sh --baseline     # 全功能关闭，验证不引入回归
```

### 何时用哪个模式

| 改了什么 | 用哪个模式 | 原因 |
|----------|-----------|------|
| 任何代码改动（日常验收） | `--fast` | 跳过 Whisper，全功能端到端 |
| 翻译 / 幻觉检测 / 两步翻译 / NLP分句 | `--fast` 或 `--integrated` | 重新翻译 + 后续全链路 |
| 迭代优化 / refine 精简扩展 | `--refine` | 只跑 refine 步骤 |
| 怀疑新功能引入回归 | `--baseline` | 全部关闭对比 |

## 流程

```
┌────────────────┐
│  1. 快速集成    │  bash test_pipeline.sh --fast (全功能开启，跳过 Whisper+翻译)
│                │  或 --integrated (改了翻译代码时)
└───────┬────────┘
        ▼
┌────────────────┐
│  2. 全量分析    │  对以下产出做量化分析（不能只看"通过/失败"）:
│                │  ┌─ segments_cache.json        → 幻觉率、覆盖率、相邻重复
│                │  ├─ audit/speed_report.json    → 标准差、离群段、均值
│                │  ├─ audit/slowdown_segments.json → 减速标记数和因子
│                │  ├─ audit/pipeline_*.log       → 各步骤耗时、错误、警告
│                │  └─ final.mp4                  → 人工抽查 3~5 段听感
└───────┬────────┘
        ▼
┌────────────────┐
│  3. 问题归因    │  某指标不通过 → 定位是哪个功能模块导致:
│                │  - 用 --retranslate 单独跑翻译排除 TTS 问题
│                │  - 用 --refine 单独跑迭代优化排除其他问题
│                │  - 用 --baseline 全关做对比
└───────┬────────┘
        ▼
┌────────────────┐
│  4. 修复        │  修改代码/参数（禁止直接编辑翻译文件绕过问题）
└───────┬────────┘
        ▼
┌────────────────┐
│  5. 回归验证    │  重新跑 --fast (或 --integrated)，确认:
│                │  - 目标问题已修复
│                │  - 其他指标未恶化
└───────┬────────┘
        ▼
      全部通过？
      ├─ 是 → 提交
      └─ 否 → 回到步骤 1（迭代 +1，最多 5 轮）
```

## 约束

- **最大迭代次数**: 5 次（超过后汇报现状，不再自动修复）
- **禁止修改翻译文件**: 不能编辑 segments_cache.json / transcribe_cache.json 来绕过问题
- **每轮必须记录**: 修改了什么、为什么修改、修复前后对比数据
- **测试前清理**: 每轮测试前恢复干净状态（test_pipeline.sh 自动处理）
- **不跑 Whisper**: 始终复用 transcribe_cache.json，避免无意义的转录耗时
- **全功能开启**: --fast 和 --integrated 自动开启所有新功能（two_pass, nlp_segmentation, post_tts_calibration, gap_borrowing, video_slowdown），不需要手动配置

## 检测指标

### 翻译质量
| 指标 | 计算方式 | 目标 | 相关功能 |
|------|----------|------|---------|
| 幻觉率 | 同一 text_zh 出现 ≥3 次的段数 / 总段数 | 0% | 幻觉防御、batch_size |
| 翻译覆盖率 | text_zh 不为空且 ≥2 字的比例 | ≥ 98% | 翻译核心 |
| 内容重复 | 相邻段子串包含率 | < 2% | 上下文窗口 |
| 长度合理性 | 中文字数 / 目标字数 偏差 > 50% 的比例 | < 10% | 两步翻译 |
| 两步一致性 | Pass 2 与 Pass 1 语义偏离的段数 | < 5% | two_pass |

### NLP 分句
| 指标 | 计算方式 | 目标 | 相关功能 |
|------|----------|------|---------|
| split 后时长一致 | Σ分句时长 == 原段时长 | 100% | nlp_segmentation |
| merge 后无碎片 | 合并后所有段 ≥ 1.5s | 100% | nlp_segmentation |
| 段数变化 | split 增加段数 + merge 减少段数 | 日志有记录 | nlp_segmentation |

### 语速/时间轴
| 指标 | 计算方式 | 目标 | 相关功能 |
|------|----------|------|---------|
| 语速标准差 | clamped_ratios 的 std_dev | < 0.08 | 语速平滑 |
| 离群段数 | raw_ratio > 1.4 的段数 | ≤ 5 | 自适应混合 |
| 平均语速 | clamped_ratios 均值 | 0.90 ~ 1.10 | trimmed mean 基线 |
| 截断段数 | 被 speed_max 限制的段数 | < 5% | 语速 clamp |

### 后校准 & 视频减速
| 指标 | 计算方式 | 目标 | 相关功能 |
|------|----------|------|---------|
| 校准修复率 | 后校准成功精简的段数 / 超速总段数 | ≥ 50% | post_tts_calibration |
| 减速标记数 | slowdown_segments.json 中的段数 | < 5 | video_slowdown |
| 减速因子范围 | factor 值范围 | 0.85 ~ 1.0 | max_slowdown_factor |

### TTS 自然度 (质量评分)
| 指标 | 计算方式 | WARN | FAIL | 依赖 |
|------|----------|------|------|------|
| CPS 均值 | 中文字数 / TTS实际时长 | > 5.5 | > 6.5 | mutagen (已有) |
| CPS P95 | 95th 百分位 CPS | > 6.5 | > 8.0 | mutagen |
| Atempo 均值 | speed_report.json avg_clamped | > 1.10 | > 1.20 | speed_report.json |
| Atempo 标准差 | speed_report.json std_clamped | > 0.06 | > 0.10 | speed_report.json |
| UTMOS MOS | 神经网络语音质量预测 | < 3.5 | < 3.0 | utmos (可选) |
| Jitter | 基频抖动 (Praat) | > 2.5% | > 5.0% | parselmouth (可选) |

自动化评分命令：
```bash
./venv/bin/python3 score_videos.py                    # 评分全部测试视频
./venv/bin/python3 score_videos.py output/VIDEO_ID    # 评分单个视频
./venv/bin/python3 score_videos.py --gate             # 质量门禁（超阈值退出 1）
./venv/bin/python3 score_videos.py --save-baseline    # 保存基线
./venv/bin/python3 score_videos.py --compare          # 对比基线
```

### 基线管理与回归检测

**何时保存基线**：
- 每次管线功能变更（翻译策略、TTS引擎、语速控制等）生效并验证通过后
- 使用 `--save-baseline` 保存当前评分，会记录 git commit hash 和时间戳

**回归检测机制**：

当存在基线文件 (`audit/baseline_scores.json`) 时，评分工具自动计算每个核心指标相对基线的变化百分比。

| 恶化程度 | 阈值 | 效果 |
|----------|------|------|
| WARN | 指标恶化 ≥ 15% | 黄色警告，不影响门禁 |
| FAIL | 指标恶化 ≥ 30% | 红色失败，`--gate` 模式退出码 1 |

**受监控指标**：
| 指标 | 恶化方向 | 说明 |
|------|----------|------|
| CPS 均值 | 上升=恶化 | 语速过快 |
| CPS P95 | 上升=恶化 | 尾部极端语速 |
| Atempo 均值 | 上升=恶化 | 调速幅度增大 |
| Atempo 标准差 | 上升=恶化 | 调速不一致 |
| UTMOS 均值 | 下降=恶化 | 语音自然度降低 (可选) |
| Jitter 均值 | 上升=恶化 | 声学质量下降 (可选) |

**工作流**：
```bash
# 1. 保存基线（管线功能变更前）
./venv/bin/python3 score_videos.py --save-baseline

# 2. 执行管线功能变更...

# 3. 重跑管线并评分（自动对比基线）
./venv/bin/python3 score_videos.py --gate

# 4. 如果回归通过，更新基线
./venv/bin/python3 score_videos.py --save-baseline
```

### 等时翻译 (isometric translation)

**原理**：IWSLT 2025 研究表明 LLM 忽略显式字数指令，但多候选生成 + 过滤可达 90%+ 长度合规率。

**配置**：
```json
{
  "llm": {
    "isometric": 3,
    "isometric_cps_threshold": 5.5
  }
}
```

**工作流**：翻译完成后，识别估算 CPS > 阈值的段，为每段生成 [轻]/[中]/[短] 三个长度变体，用 jieba 分词估算时长选最接近目标的。

| 指标 | 计算方式 | WARN | FAIL | 说明 |
|------|----------|------|------|------|
| 等时合规率 | CPS 在 [3.5, 6.0] 的段占比 | < 50% | < 40% | 越高越好 |

**复用现有基础设施**：
- `_parse_multi_candidates()` 解析 [轻]/[中]/[短]
- `_select_best_candidate()` 候选选择（新增 `allow_same_length` 参数）
- `_estimate_duration_jieba()` 时长估算

### 迭代优化 (refine mode)
| 指标 | 计算方式 | 目标 | 相关功能 |
|------|----------|------|---------|
| 精简成功率 | 变更数 / 超速数 | ≥ 60% | _refine_with_llm |
| 扩展成功率 | 采纳数 / 尝试数 | ≥ 40% | _expand_with_llm |
| 收敛轮次 | early stop 触发轮次 | ≤ 3 | 迭代控制 |

### 端到端交叉验证
| 指标 | 计算方式 | 目标 |
|------|----------|------|
| NLP分句→翻译无损 | 分句后的段翻译覆盖率 | ≥ 98% |
| 两步翻译→TTS 语速 | two_pass 开启后 std_dev 不恶化 | ≤ 基线 ×1.2 |
| 后校准→对齐无回退 | 校准后 speed_report.std 不高于校准前 | ≤ 校准前 |
| 全流程→最终视频 | final.mp4 存在且 >0 bytes | 100% |

## 分析检查项

### 翻译质量
- [ ] 同一 text_zh 出现 ≥3 次 → 幻觉
- [ ] 相邻段是否有内容重复（子串包含）
- [ ] 翻译是否与英文原文对应（非跨段混淆）
- [ ] 翻译长度是否合理（中文字数 vs 目标字数）
- [ ] 迭代优化是否引入错误（对比 iter_0 和最终 segments_cache）
- [ ] 两步翻译 Pass 2 是否偏离 Pass 1 直译含义

### NLP 分句
- [ ] split 是否在正确的句子边界（不在词中间切断）
- [ ] merge 后的段是否都 > 1.5s
- [ ] 分句前后总时长不变
- [ ] 分句后的段能否正常翻译和生成 TTS

### 语速/时间轴
- [ ] 超速段数量和最大值
- [ ] 平均语速是否在合理范围 (0.85x-1.15x)
- [ ] 标准差是否 < 0.08
- [ ] 截断段数（限速）
- [ ] 填充段数（静音填充）
- [ ] 间隙借用成功数

### 后校准
- [ ] 精简后 ratio 是否降到阈值内
- [ ] 精简是否保持语义忠实
- [ ] TTS 重新生成是否成功

### 视频减速
- [ ] slowdown_segments.json 中的 factor 是否在 [0.85, 1.0)
- [ ] 标记数量是否合理（< 5 为佳）

### 功能交互
- [ ] NLP 分句 + 翻译: 分句后的段翻译质量无下降
- [ ] 两步翻译 + TTS: Pass 2 译文的 TTS 语速不恶化
- [ ] 后校准 + 减速标记: 两者不冲突（校准先于减速判定）
- [ ] 迭代优化 + 后校准: refine 精简后，后校准仍能进一步收紧

## 全量分析脚本

```python
#!/usr/bin/env python3
"""集成测试全量分析脚本 — 一次性检查所有指标"""
import json, sys, os
from collections import Counter
from pathlib import Path

video_dir = sys.argv[1] if len(sys.argv) > 1 else "output/zjMuIxRvygQ"
p = Path(video_dir)

print("=" * 60)
print(f"集成测试分析: {p}")
print("=" * 60)

# ── 翻译质量 ──
cache = p / "segments_cache.json"
if cache.exists():
    segs = json.loads(cache.read_text())
    texts_zh = [s.get("text_zh", "") for s in segs]
    non_empty = [t for t in texts_zh if t and len(t.strip()) >= 2]
    counts = Counter(non_empty)
    hall = {t: c for t, c in counts.items() if c >= 3}
    hall_count = sum(hall.values())
    covered = len(non_empty)

    # 相邻重复
    adj_dup = 0
    for i in range(1, len(segs)):
        prev = segs[i-1].get("text_zh", "").strip()
        curr = segs[i].get("text_zh", "").strip()
        if len(prev) > 8 and len(curr) > 8 and (prev in curr or curr in prev):
            adj_dup += 1

    print(f"\n【翻译质量】")
    print(f"  总段数:     {len(segs)}")
    print(f"  幻觉率:     {hall_count}/{len(segs)} = {hall_count/len(segs):.1%}  {'✅' if hall_count == 0 else '❌'}")
    for t, c in hall.items():
        print(f"    ❌ '{t[:40]}' × {c}")
    print(f"  覆盖率:     {covered}/{len(segs)} = {covered/len(segs):.1%}  {'✅' if covered/len(segs) >= 0.98 else '❌'}")
    print(f"  相邻重复:   {adj_dup}/{len(segs)-1} = {adj_dup/(len(segs)-1):.1%}  {'✅' if adj_dup/(len(segs)-1) < 0.02 else '❌'}")

# ── 语速 ──
speed_file = p / "audit" / "speed_report.json"
if speed_file.exists():
    rpt = json.loads(speed_file.read_text())
    print(f"\n【语速/时间轴】")
    std = rpt.get("std_clamped", 999)
    avg = rpt.get("avg_clamped", 0)
    out = rpt.get("outliers_gt_1.4", 999)
    print(f"  标准差:     {std}  {'✅' if std < 0.08 else '❌'} (目标 <0.08)")
    print(f"  平均语速:   {avg}  {'✅' if 0.90 <= avg <= 1.10 else '❌'} (目标 0.90~1.10)")
    print(f"  离群段:     {out}  {'✅' if out < 3 else '❌'} (目标 <3)")
    print(f"  限速/提速:  {rpt.get('clamped_fast', '?')}/{rpt.get('clamped_slow', '?')}")
    print(f"  基线:       {rpt.get('baseline', '?')}")

# ── 减速标记 ──
slow_file = p / "audit" / "slowdown_segments.json"
if slow_file.exists():
    slows = json.loads(slow_file.read_text())
    print(f"\n【视频减速】")
    print(f"  标记数:     {len(slows)}  {'✅' if len(slows) < 5 else '⚠️'}")
    if slows:
        factors = [s["factor"] for s in slows]
        print(f"  因子范围:   {min(factors):.3f} ~ {max(factors):.3f}")

# ── 输出文件 ──
print(f"\n【输出文件】")
for name in ["final.mp4", "chinese_dub.wav", "subtitle_bilingual.srt",
             "subtitle_zh.srt", "subtitle_en.srt"]:
    fp = p / name
    if fp.exists() and fp.stat().st_size > 0:
        size = fp.stat().st_size / (1024*1024) if fp.stat().st_size > 1024*1024 else fp.stat().st_size / 1024
        unit = "MB" if fp.stat().st_size > 1024*1024 else "KB"
        print(f"  ✅ {name} ({size:.1f} {unit})")
    else:
        print(f"  ❌ {name} 缺失")

tts_dir = p / "tts_segments"
if tts_dir.exists():
    tts_count = len(list(tts_dir.glob("seg_*.mp3")))
    print(f"  ✅ tts_segments/ ({tts_count} 个)")

print(f"\n{'=' * 60}")
```

## test_pipeline.sh --fast / --integrated 配置模板

`--fast` 和 `--integrated` 自动生成以下配置（所有新功能开启）:

```json
{
  "translator": "llm",
  "llm": {
    "batch_size": 8,
    "two_pass": true
  },
  "nlp_segmentation": true,
  "refine": {
    "enabled": true,
    "max_iterations": 3,
    "post_tts_calibration": true,
    "calibration_threshold": 1.30
  },
  "alignment": {
    "gap_borrowing": true,
    "max_borrow_ms": 300,
    "video_slowdown": true,
    "max_slowdown_factor": 0.85
  }
}
```

## 示例记录格式

```
=== 集成测试 迭代 N ===
时间: 2026-05-01 18:xx
测试命令: bash test_pipeline.sh --fast
开启功能: two_pass, nlp_segmentation, post_tts_calibration, gap_borrowing, video_slowdown

翻译指标:
- 幻觉率: 0%  ✅
- 覆盖率: 100%  ✅
- 相邻重复: 0%  ✅

语速指标:
- std_dev: 0.054  ✅
- 均值: 1.04  ✅
- 离群段: 1  ✅

后校准:
- 校准段数: 3, 成功: 2  ✅

问题发现:
- [描述具体问题]

根因分析（用 --retranslate / --fast 定位）:
- [为什么会出现这个问题]

修复措施:
- [具体改了什么代码/参数]

修复后指标:
- [对比修复前后所有指标]

结论: [问题是否解决]
```
