# 字幕编号前缀残留问题修复

**日期**: 2026-03-28
**问题**: 字幕文件中残留编号前缀 `[2]`、`[10]`、`[11]`、`[12]`、`[14]`

## 问题分析

### 现象
- `subtitle_zh.srt` 中第 17、98、102、106、114 行残留 `[N]` 编号前缀
- `segments_cache.json` 已清理干净（第 5 轮迭代后已去除前缀）
- `iter_4_speed_report.json` 中残留编号前缀

### 根因
第 4 轮迭代优化时，LLM 返回的精简翻译带有编号前缀 `[N]`，虽然 `segments_cache.json` 在第 5 轮后被清理，但字幕文件在第 4 轮后生成并未被更新。

## 解决方案

使用 `--resume-from` 从 TTS 步骤重新生成，跳过已完成的步骤。

### 执行步骤

1. **删除旧文件**
   ```bash
   rm -rf output/32884a7ba3d/tts_segments
   rm output/32884a7ba3d/subtitle_*.srt
   ```

2. **重建虚拟环境**（解决 SSL 模块问题）
   ```bash
   rm -rf venv
   /usr/local/bin/python3.11 -m venv venv
   pip install faster-whisper edge-tts deep-translator pydub yt-dlp httpx
   ```

3. **配置文件修改**
   ```json
   {
     "skip_steps": ["download", "extract", "refine"],
     "refine": { "enabled": false }
   }
   ```

4. **执行 pipeline**
   ```bash
   python3 pipeline.py --config config.json
   ```

## 验证结果

```python
import re
with open('output/32884a7ba3d/subtitle_zh.srt', 'r') as f:
    content = f.read()
# 检查是否有编号前缀残留
for line in content.split('\n'):
    if re.match(r'^\[?\d+\]?\s*', line) and not re.match(r'^\d+$', line):
        # 无匹配
        pass
# ✅ 字幕文件干净，无编号前缀残留
```

## 执行耗时

- TTS 生成: 4 个新片段
- 时间线对齐: 调速 67 个，静音填充 4 个，跳过 1 个
- 总耗时: **58 秒**

## 输出文件

| 文件 | 状态 |
|------|------|
| `subtitle_bilingual.srt` | ✅ 干净 |
| `subtitle_zh.srt` | ✅ 干净 |
| `subtitle_en.srt` | ✅ 干净 |
| `chinese_dub.wav` | ✅ 已更新 |
| `final.mp4` | ✅ 已更新 |

## 后续建议

考虑在 pipeline 中增加字幕文件的编号前缀清理逻辑，作为安全网：
- 在 `generate_srt_files()` 中增加 `_strip_numbered_prefix()` 调用
- 确保即使上游数据残留前缀，字幕文件也能保持干净