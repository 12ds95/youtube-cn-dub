# 五项 TODO 实现记录

日期：2026-03-28
影响范围：`pipeline.py` 全局
测试视频：`output/32884a7ba3d/`, `output/f09d1957a98/`


## TODO 1: 翻译质量优化

### 排查

审查 `_translate_llm()` 发现三个问题：
- 批量翻译 prompt 无视频主题上下文，技术术语在不同 batch 间不一致
- 批量解析返回数量不匹配时直接降级，无中间校验
- 无翻译风格控制能力

### 修复

**1a 上下文翻译**：
- `_translate_llm` 新增 `video_title` 参数，由 `process_video()` 中下载视频后注入 `config["video_title"]`
- batch prompt 前添加视频主题和前文（上一批最后 2 句译文），保持术语一致性

**1b 对齐保证**：
- 解析后校验有效翻译数是否 ≥ batch 70%，不满足则降级逐条翻译
- 避免解析失败导致翻译-原文错位

**1c 翻译风格控制**：
- `DEFAULT_CONFIG["llm"]` 新增 `"style"` 字段
- 非空时追加到 system_prompt：`"\n翻译风格要求：{style}"`
- 支持 "口语化"、"正式"、"学术" 等自定义风格


## TODO 2: 语音一致性优化

### 排查

检查 `output/32884a7ba3d/` 的 speed_report，各片段 speed_ratio 分布不均：
- 最大 2.2x，最小 0.5x，方差很大
- 相邻片段可能从 0.7x 跳到 1.4x，听感割裂

### 修复

`_align_tts_to_timeline` 中实现三步语速平滑：

1. **首遍扫描**：收集所有片段原始 speed_ratio
2. **全局基线**：计算有效比率的中位数作为基线
3. **混合 + 平滑**：
   - 每段 ratio = 60% 自身 + 40% 全局中位数（拉向基线）
   - 指数平滑（α=0.3）消除相邻段突变

效果：语速分布方差显著缩小，听感更连贯。


## TODO 3: 迭代性能优化

### 排查

审查 `run_refinement_loop` 发现：
- 每轮迭代对所有片段重新测速和判断，包括已收敛的（浪费 LLM 调用）
- TTS 增量重生成逻辑已有（只删改变片段的缓存），但日志不够明确

### 修复

**3a 跳过已收敛片段**：
- 引入 `converged_indices` 集合，每轮将 status="ok" 的段标记为已收敛
- 后续轮次 overfast/underslow 筛选排除已收敛段
- 打印 "已收敛片段: N/M (本轮跳过)"

**3b 增量 TTS 日志**：
- 明确打印 "增量 TTS: 仅重新生成 N/M 个变更片段"


## TODO 4: 0 字节 TTS 文件排查

### 排查

`output/32884a7ba3d/tts_segments/seg_0007.mp3` = 0 字节：
- 对应 text_zh="这无疑是我有幸参与过的最酷炫的项目之一。"（20 字，非空）
- edge-tts 网络不稳时可能写出 0 字节文件
- 下游 `_align_tts_to_timeline` 跳过该片段，导致最终视频对应时段无配音

`output/f09d1957a98/` 更严重：14 个 0 字节 + 40 个缺失 TTS 文件

### 修复

在 `_generate_tts_segments` 末尾增加两层兜底：
1. **重试**：扫描 0 字节文件，删除后重新生成
2. **静音填充**：重试仍失败的，生成静音 mp3 占位，避免下游完全无声


## TODO 5: speed_report 中 skipped 含义

### 排查

`f09d1957a98` 的 `iter_1_speed_report.json` 有 75/820 段 skipped：
- 全部是 tts_ms=0（TTS 文件不存在或 0 字节），target_ms > 0
- 根因同 TODO4：TTS 生成失败导致无音频可测速

`32884a7ba3d` 的 speed_report 无 skipped（只有 1 个 0 字节文件）

### 修复

- `_measure_speed_ratios` 的 skipped 结果新增 `skip_reason` 字段（`"no_tts"` / `"zero_duration"`）
- 迭代循环中打印跳过原因统计：`"跳过片段: N (无TTS: X, 零时长: Y)"`
- 配合 TODO4 的重试机制，从根源减少 skipped 段数


## 验证

- `python3 -c "import ast; ast.parse(open('pipeline.py').read())"` → 语法通过
- `python3 tests/test_parse_translation.py` → 5/5 通过
- `python3 tests/test_refine_dedup.py` → 14/14 通过
