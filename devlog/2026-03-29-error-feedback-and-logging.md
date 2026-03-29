# 结构化错误反馈 + 执行日志

**日期**: 2026-03-29
**问题**: pipeline 报错后用户无法定位根因，也无法快速修复；执行过程无日志留存，调试只能靠回忆

## 现象

1. 用 `config.resume.json`（含 skip_steps）跑新视频时，pipeline 0 秒"处理完成"但无任何输出，用户无从下手
2. 下载失败、TTS 余额不足等可预知错误只打印原始 traceback，不给修复建议
3. 无执行日志文件，关掉终端后现场丢失

## 根因分析

- pipeline 对可预知错误（配置残留、依赖缺失、网络问题、API 余额不足）没有结构化处理，统一走 `except Exception` 后 `raise` 或 `sys.exit`
- 无日志系统，所有输出只到 stdout

## 修复方案

### 1. PipelineLogger 类（双输出日志 + 步骤计时）

```python
class PipelineLogger:
    def __init__(self, output_dir):
        # 创建 pipeline_YYYYMMDD_HHMMSS.log
    def step_begin(name) / step_end()  # 自动记录各步骤耗时
    def log_error(error_type, message, suggestion)  # 结构化错误
    def write_summary()  # 写耗时摘要到日志文件
```

- 日志文件保存在 `output/<video_id>/pipeline_*.log`
- 屏幕和文件双输出
- 错误发生时 traceback 只写日志文件（不打印到屏幕，避免吓到用户）
- 耗时摘要只写日志文件（屏幕已有"处理完成"输出）

### 2. 结构化错误反馈

每种可预知错误给出：
- **error_type**: 错误类型（配置错误/下载失败/前置条件缺失/语音识别失败/翻译失败）
- **message**: 具体问题描述
- **suggestion**: 直接可用的修复方案（含可复制的命令行/配置修改）

示例输出：
```
============================================================
❌ 错误 [配置错误]

  问题: segments 为空，skip_steps 跳过了关键步骤但产出文件不存在

  修复建议:
    方案 A: 移除 skip_steps 中的这些项，让 pipeline 从头执行:
      修改 config.json 中 "skip_steps" 为: ["subtitle"]
    方案 B: 如果你想从之前的运行恢复，确保 segments_cache.json 存在
============================================================
```

### 3. 覆盖的错误场景

| 步骤 | 错误类型 | 触发条件 | 修复建议 |
|------|---------|---------|---------|
| 下载 | 下载失败 | yt-dlp 异常 | 检查网络/URL/yt-dlp 版本 |
| 提取音频 | 前置条件缺失 | video 不存在 | 移除 download skip 或手动放视频 |
| 提取音频 | 音频提取失败 | ffmpeg 失败 | 检查 ffmpeg 安装和视频完整性 |
| 转录 | 语音识别失败 | Whisper 异常 | 检查 faster-whisper 安装、换小模型 |
| 翻译 | 翻译失败 | LLM/Google 异常 | 检查 API Key/网络、换引擎 |
| 前置条件 | 配置错误 | skip_steps 跳过但产出不存在 | 给出可直接使用的 skip_steps 修改建议 |

## 测试验证

- 93 passed, 1 skipped（siliconflow 余额不足正确标记为 skip）
- PipelineLogger 单元验证：日志文件创建、步骤耗时记录、错误格式化、摘要输出
- 修复 test_tts_smoke.py 中 `unittest_skip` 在 pytest 环境下不被识别为 skip 的问题（改为检测 pytest 可用时调用 `pytest.skip()`）

## 关键收获

- 错误反馈的核心不是告诉用户"出错了"，而是告诉用户"怎么修"
- 日志文件的价值在于保留现场——traceback 写文件不写屏幕，对用户友好且不丢信息
- 耗时统计是性能优化的前提，先有数据才能找瓶颈
