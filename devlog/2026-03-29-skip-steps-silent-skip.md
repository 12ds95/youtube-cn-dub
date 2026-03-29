# pipeline 静默跳过所有步骤：skip_steps + 空 output 目录

## 现象

新视频 kCc8FmEb1nY，config.json 中有 `skip_steps: ["download", "extract", "transcribe", "translate", "subtitle"]`，output 目录为空（新视频从未处理过）。

pipeline 0 秒完成，日志只显示 step 3/4 "跳过"，step 5/6/7 完全不出现，直接打印"处理完成"并报告 final.mp4（实际不存在）。

```
[3/7] 语音识别 - 跳过
[4/7] 翻译 - 跳过

🎉 处理完成! (耗时 0s)
```

## 根因

三层问题叠加：

1. **skip_steps 是从上一个视频遗留的配置**，用户换了新视频 URL 但忘了清掉 skip_steps
2. **缓存加载逻辑**：`cache_file.exists()` 为 false（空目录）→ 走 else 分支 → transcribe/translate 都在 skip 里 → `segments = []`
3. **后续步骤的 `and segments` 守卫**：segments 为空时字幕、TTS、合成全部跳过
4. **结尾打印"处理完成"不检查实际产出**：即使什么都没做也报"成功"

核心问题：**pipeline 在关键前置条件不满足时静默跳过而非报错**。

## 修复

在 step 3/4 之后、分支流程入口前，增加防御性检查：

1. `segments` 为空时逐项检查 skip_steps 中每个步骤的产出是否存在
2. 产出不存在 → 打印明确的错误诊断信息和解决方法 → 终止执行
3. 所有产出都存在但 segments 仍为空 → 可能视频无人声，打印警告后终止
4. step 1/2 即使跳过，也打印文件是否存在的状态

修复后输出：
```
❌ 错误: segments 为空，无法继续处理!
   skip_steps 跳过了关键步骤，但对应的产出文件不存在:
  - skip_steps 包含 'download' 但视频不存在: output/kCc8FmEb1nY/original.mp4
  - skip_steps 包含 'transcribe' 但缓存不存在: output/kCc8FmEb1nY/segments_cache.json
```

## 反思

这个问题本质是**配置残留 + 防御性编程缺失**的组合。经验教训：

1. **永远不要在"没有工作可做"的情况下报告成功** — 如果 pipeline 什么都没处理，必须报错或至少警告
2. **skip_steps 是危险配置** — 应该对每个 skip 项检查前置产出是否存在
3. **配置残留是常见用户错误** — 换视频后忘清 skip_steps/resume_from 是正常操作，代码要容错
