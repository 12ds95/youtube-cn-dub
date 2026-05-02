#!/usr/bin/env python3
"""
YouTube 英文视频 → 中文配音 + 中英双语字幕 端到端 Pipeline (v3)
================================================================
工具链: yt-dlp + faster-whisper + deep-translator/LLM + edge-tts + ffmpeg

用法:
    # 基础用法
    python pipeline.py "https://www.youtube.com/watch?v=XXXX"

    # 使用 JSON 配置文件
    python pipeline.py --config config.json

    # 从已有输出目录断点续跑（调试翻译/配音）
    python pipeline.py --resume-from output/my_video

    # LLM 翻译 + 迭代优化（自动精简过长翻译）
    python pipeline.py "URL" --translator llm --llm-api-key sk-xxx --refine 3

    # 清理迭代数据重来
    python pipeline.py --resume-from output/VIDEO_ID --clean-iterations --refine 3

输出:
    output/<video_id_or_name>/
        ├── original.mp4          # 原始视频
        ├── audio.wav             # 原始音频
        ├── segments_cache.json   # 转录+翻译缓存（可手动编辑）
        ├── subtitle_en.srt       # 英文字幕
        ├── subtitle_zh.srt       # 中文字幕
        ├── subtitle_bilingual.srt # 中英双语字幕
        ├── chinese_dub.wav       # 中文配音音轨
        ├── final.mp4             # 最终输出（中文配音 + 外挂字幕）
        └── iterations/           # 迭代优化快照（--refine 时生成）
            ├── iter_0_segments.json       # 初始翻译快照
            ├── iter_0_speed_report.json   # 语速分析报告
            ├── iter_1_segments.json       # 第 1 轮优化后翻译
            ├── iter_1_changes.json        # 第 1 轮变更记录
            └── ...
"""

# CTranslate2 (faster-whisper) 和 PyTorch (demucs/numpy) 各自链接了一份
# libiomp5 (OpenMP)，同一进程中加载两份会触发 SIGABRT。
# 必须在任何 import 之前设置此环境变量。
import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import argparse
import asyncio
import json
import logging
import math
import re
import shutil
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any


# ─── 审计目录 ──────────────────────────────────────────────────────
def _audit_dir(output_dir: Path) -> Path:
    """返回审计日志子目录 output_dir/audit/，不存在则创建。"""
    d = output_dir / "audit"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _migrate_audit_files(output_dir: Path):
    """将旧版根目录下的审计文件迁移到 audit/ 子目录（一次性兼容）。"""
    audit = _audit_dir(output_dir)
    # 单文件迁移
    for name in ("speed_report.json", "slowdown_segments.json",
                 "calibration_results.json", "style_detection.json",
                 "tts_failure.json"):
        old = output_dir / name
        if old.exists() and not (audit / name).exists():
            shutil.move(str(old), str(audit / name))
    # pipeline_*.log 迁移
    for old in output_dir.glob("pipeline_*.log"):
        dest = audit / old.name
        if not dest.exists():
            shutil.move(str(old), str(dest))
    # iterations/ 目录迁移
    old_iter = output_dir / "iterations"
    new_iter = audit / "iterations"
    if old_iter.is_dir() and not new_iter.exists():
        shutil.move(str(old_iter), str(new_iter))


# ─── 日志系统 ──────────────────────────────────────────────────────
class PipelineLogger:
    """双输出日志：同时写屏幕和文件，自动记录各步骤耗时"""

    def __init__(self, output_dir: Path = None):
        self.step_timings = []        # [(step_name, elapsed_secs), ...]
        self._step_start = None
        self._step_name = None
        self.log_path = None
        self._file = None
        self._t_start = time.time()

        if output_dir:
            self.setup_file(output_dir)

    def setup_file(self, output_dir: Path):
        """设置日志文件，在 output_dir 确定后调用"""
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = _audit_dir(output_dir) / f"pipeline_{ts}.log"
        self._file = open(self.log_path, "w", encoding="utf-8")
        self._file.write(f"# Pipeline 执行日志 - {datetime.now().isoformat()}\n\n")

    def log(self, msg: str, also_print: bool = True):
        """写日志（同时打印到屏幕）"""
        if also_print:
            print(msg)
        if self._file:
            self._file.write(msg + "\n")
            self._file.flush()

    def step_begin(self, name: str):
        """标记步骤开始"""
        self._finish_prev_step()
        self._step_name = name
        self._step_start = time.time()

    def step_end(self):
        """标记步骤结束"""
        self._finish_prev_step()

    def _finish_prev_step(self):
        if self._step_name and self._step_start:
            elapsed = time.time() - self._step_start
            self.step_timings.append((self._step_name, elapsed))
            self._step_name = None
            self._step_start = None

    def log_error(self, error_type: str, message: str, suggestion: str,
                  exception: Exception = None):
        """记录结构化错误：类型 + 详情 + 修复建议"""
        sep = "=" * 60
        lines = [
            f"\n{sep}",
            f"❌ 错误 [{error_type}]",
            f"",
            f"  问题: {message}",
            f"",
            f"  修复建议:",
        ]
        for line in suggestion.strip().split("\n"):
            lines.append(f"    {line}")
        if exception:
            lines.append(f"")
            lines.append(f"  异常详情: {type(exception).__name__}: {exception}")
        lines.append(sep)
        full_msg = "\n".join(lines)
        self.log(full_msg)

        # 写完整 traceback 到日志文件（不打印到屏幕）
        if exception and self._file:
            self._file.write(f"\n--- Traceback ---\n")
            self._file.write(traceback.format_exc())
            self._file.write(f"--- End Traceback ---\n\n")
            self._file.flush()

    def write_summary(self):
        """写执行摘要（耗时统计）"""
        self._finish_prev_step()
        total = time.time() - self._t_start
        lines = ["\n--- 各步骤耗时 ---"]
        for name, secs in self.step_timings:
            lines.append(f"  {name}: {secs:.1f}s")
        lines.append(f"  总计: {total:.1f}s")
        lines.append("--- End ---\n")
        summary = "\n".join(lines)
        # 耗时摘要只写文件，不打印屏幕（屏幕已有"处理完成"输出）
        if self._file:
            self._file.write(summary)
            self._file.flush()

    def close(self):
        """关闭日志文件"""
        self.write_summary()
        if self._file:
            self._file.close()
            self._file = None


# 全局 logger 实例，在 process_video 中初始化
_logger: Optional[PipelineLogger] = None


def _log(msg: str, also_print: bool = True):
    """便捷日志函数"""
    global _logger
    if _logger:
        _logger.log(msg, also_print)
    elif also_print:
        print(msg)

# ─── 默认配置 ──────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    # ── 基础参数 ──
    "url": None,                     # YouTube 视频 URL（与 resume_from 二选一）
    "output": "output",              # 输出根目录，每个视频存入 output/<video_id>/
    "voice": "zh-CN-YunxiNeural",    # edge-tts 语音（仅影响 edge-tts 引擎，其他引擎有各自配置）
                                     # 可选: YunxiNeural(默认男) / YunjianNeural(硬朗男) /
                                     #        YunyangNeural(播音男) / XiaoxiaoNeural(温柔女) /
                                     #        XiaoyiNeural(活泼女) / YunxiaNeural(年轻男)
    "whisper_model": "small",        # Whisper 语音识别模型: tiny(75MB快) / small(500MB 推荐) / medium(1.5GB 精确)
    "volume": 0.15,                  # 原声背景音量混入比例: 0.0=静音 / 0.15=默认 / 1.0=原始音量
    "browser": "chrome",             # yt-dlp 读取 cookies 的浏览器: chrome / firefox / edge / safari
    "download_quality": "best",      # 下载画质: "best"(原始最高质量) / "1080p" / "720p" / "480p"
                                     # best 会下载源视频最高分辨率+最高帧率(如 1440p60/1080p60)
    "rename": None,                  # 处理完成后重命名输出目录（如 "线性代数精讲"）
    "resume_from": None,             # 从已有输出目录断点续跑（如 "output/f09d1957a98"）

    # ── 翻译引擎 ──
    "translator": "google",          # 翻译引擎: "google"(免费) 或 "llm"(质量更好，需 API Key)
    "llm": {                         # LLM 翻译配置（translator="llm" 或 refine.enabled=true 时生效）
                                     # 支持所有 OpenAI 兼容 API: DeepSeek / Qwen / Moonshot / GPT 等
        "api_url": "https://api.deepseek.com/v1",  # API 端点 URL
        "api_key": "",               # API 密钥（也可用 --llm-api-key 命令行传入）
        "model": "deepseek-chat",    # 模型名称
        "system_prompt": (           # 翻译 system prompt（一般无需修改）
            "你是专业的英中翻译引擎。将以下英文文本翻译为简体中文。"
            "要求：1)翻译准确流畅，符合中文表达习惯；"
            "2)计算机领域缩写保留英文原词（如 API、SDK、HTTP、GPU 等），但不要加括号注音（禁止写成'四元数（Quaternions）'这种形式）；"
            "3)忠实原文语义，不要为缩短字数而曲解原意，也不要过度扩充；"
            "4)翻译要适合做视频配音朗读，语句通顺自然，短句为主；"
            "5)只输出翻译结果，不要解释。"
        ),
        "batch_size": 8,             # 每批翻译的句子数（过大可能导致幻觉重复）
        "temperature": 0.3,          # 生成温度: 0.0=确定性 / 0.3=推荐 / 1.0=多样性
        "style": "",                 # 翻译风格: ""(默认) / "口语化" / "正式" / "学术" 等
        "prompt_template": "",       # 外部提示词模板路径（如 "prompts/dubbing_concise.txt"），为空则用内置 system_prompt
        "two_pass": False,           # 两步翻译: Pass1 忠实直译 → Pass2 配音改编（API 成本翻倍）
        "isometric": 0,              # 等时多候选翻译: 0=关闭, 3=为高CPS段生成3个长度变体自动选优
        "isometric_cps_threshold": 5.5,  # 估算CPS超过此值才触发多候选（自然中文 3.5-6.0）
        "isometric_expand_cps_threshold": 3.5,  # CPS低于此值触发多候选扩展
    },

    # ── TTS 配音引擎 ──
    #   tts_chain: 引擎优先级链（推荐方式），第一个为主引擎，后面按顺序整体回退
    #   也可用旧方式: tts_engine(主) + tts_fallback(回退列表)
    #   各引擎有独立语音配置（voice 字段仅影响 edge-tts），详见各引擎 resolve_voice()
    #   引擎分类:
    #     远程: edge-tts(免费), gtts(免费)
    #     本地: pyttsx3(零依赖), piper(需下载~70MB), sherpa-onnx(需下载~110MB)
    "tts_chain": None,               # 引擎优先链: 如 ["edge-tts", "gtts", "pyttsx3"]
                                     # 为 null 时使用 tts_engine + tts_fallback 组合
    "tts_engine": "edge-tts",        # 主 TTS 引擎（tts_chain 为空时生效）
    "tts_fallback": [],              # 回退引擎列表（tts_chain 为空时生效）

    # 各 TTS 引擎专属配置（仅使用对应引擎时需要）
    "pyttsx3": {                     # 系统自带 TTS（完全离线，macOS=NSSpeech / Windows=SAPI5）
                                     # macOS 需先下载中文语音: 系统设置 → 辅助功能 → 朗读内容 → 管理声音
        "voice_name": None,          # 系统语音名: "Ting-Ting"(普通话女) / "Mei-Jia"(台湾女) / null=自动查找中文
        "rate": 180,                 # 语速 (words per minute)
    },
    "piper": {                       # Piper 本地 ONNX TTS（需 bash download_model.sh piper 下载模型）
        "model_path": None,          # 模型路径: 如 "models/piper/zh_CN-huayan-medium.onnx"
    },
    "sherpa_onnx": {                 # sherpa-onnx MeloTTS（需 bash download_model.sh sherpa 下载模型）
        "model": "",                 # 模型文件: 如 "models/sherpa-onnx/vits-melo-tts-zh_en/model.onnx"
        "lexicon": "",               # 词典文件: 如 "models/sherpa-onnx/vits-melo-tts-zh_en/lexicon.txt"
        "tokens": "",                # tokens 文件: 如 "models/sherpa-onnx/vits-melo-tts-zh_en/tokens.txt"
        "dict_dir": "",              # 词典目录（可选）
        "speaker_id": 0,             # 说话人 ID（多人模型时选择）
    },

    # ── 人声/背景音分离 ──
    "audio_separation": {
        "enabled": False,            # 是否启用音频分离（需要 demucs: pip install demucs）
        "model": "htdemucs",         # demucs 模型: htdemucs(默认,快速) / htdemucs_ft(精度更高,更慢)
        "vocal_volume": 0.0,         # 原始人声音量: 0.0=静音(被中文配音替代) / 0.1=保留一点原声
        "bgm_volume": 1.0,           # 背景音/伴奏音量: 1.0=原始音量 / 0.5=减半
        "device": "auto",            # 推理设备: auto(自动检测GPU) / cpu / cuda
    },

    # ── 性能选项 ──
    "tts_concurrency": 5,            # TTS 并发数（远程引擎失败时会自动阶梯降并发）
    "whisper_beam_size": 5,          # Whisper beam search 大小（越大越精确但越慢）
    "nlp_segmentation": False,       # spaCy NLP 断句优化（split长段/merge碎段，需 pip install spacy）
    "cpu_threads": 0,                # CPU 线程数限制: 0=使用所有核心, N=限制为 N 线程
                                     # 影响: demucs / whisper / ffmpeg atempo 并行数
    "global_speed": 1.0,             # 全局语速倍率: 0.8=慢速朗读 / 1.0=正常 / 1.2=快速
                                     # 统一缩放所有 TTS 片段语速，不影响原始音频
    "alignment": {                   # 时间线对齐增强选项
        "gap_borrowing": False,      # 间隙借用: TTS 稍超目标时长时，从相邻静音间隙借用时间而非截断
        "max_borrow_ms": 300,        # 最大借用时长 (ms)，防止段间重叠
        "video_slowdown": False,     # 视频减速: TTS 超时≤15%且无法借用时，对视频段施加减速而非截断音频
        "max_slowdown_factor": 0.85, # 最大减速因子 (0.85 = 视频播放速度降至 85%)
        "atempo_disabled": True,     # 禁用 ffmpeg atempo 后处理调速，改用 TTS 原生 rate 控制
        "tts_rate_range": [0.80, 1.35],  # TTS 原生 rate 安全区间（edge-tts 支持 0.5-2.0）
        "overflow_tolerance": 0.10,  # 允许 TTS 超目标时长此比例不截断（0.10 = 10%）
        "feedback_loop": True,       # 启用试发-反馈闭环：TTS 后测时长，偏差大则重生成
        "feedback_tolerance": 0.15,  # 闭环触发阈值：实测偏差 > 15% 时重新生成
    },
    "skip_steps": [],                # 跳过指定步骤（按执行顺序）:
                                     #   标准流程(7/8步): download / extract / separate / transcribe / translate / subtitle / tts / merge
                                     #   迭代流程(8/9步): download / extract / separate / transcribe / translate / refine / tts / subtitle / merge

    # ── 迭代优化（翻译过长时自动精简）──
    #   小循环（自动）：字符估算语速 → LLM 精简过长翻译 → 再估算 → 仍超速则继续精简，直到收敛
    #   大循环（人工）：人工审听后决定是否再跑一轮，用 --resume-iteration 断点续跑
    #   需要 LLM API（同 translator="llm" 的配置）
    "refine": {
        "enabled": False,            # 是否启用迭代优化
        "max_iterations": 5,         # 单次运行最大迭代轮次（收敛后 early stop）
        "speed_threshold": 1.25,     # 加速倍率阈值: >1.25x 即触发精简（1.0=原速, 1.5x 已很明显）
        "resume_iteration": None,    # 从第 N 轮迭代恢复（大循环断点续跑）
        "post_tts_calibration": False,  # TTS 后校准: 测量实际时长，对超速段重新精简+重生成
        "calibration_threshold": 1.30,  # 后校准触发阈值 (ratio > 此值的段将被精简)
    },
    "clean_iterations": False,       # 清理迭代中间数据（iterations/ 目录）后重新优化
}


# ─── 配置加载 ──────────────────────────────────────────────────────
def load_config(args) -> dict:
    """
    配置优先级: CLI 参数 > JSON 配置文件 > 默认值
    """
    import copy
    config = copy.deepcopy(DEFAULT_CONFIG)

    # 1) 从 JSON 配置文件加载
    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"❌ 配置文件不存在: {args.config}")
            sys.exit(1)
        with open(config_path, "r", encoding="utf-8") as f:
            file_config = json.load(f)
        # 深度合并
        _deep_merge(config, file_config)
        print(f"📄 已加载配置文件: {args.config}")

    # 2) CLI 参数覆盖（只覆盖显式指定的）
    cli_map = {
        "url": "url",
        "output": "output",
        "voice": "voice",
        "whisper_model": "whisper_model",
        "volume": "volume",
        "browser": "browser",
        "rename": "rename",
        "resume_from": "resume_from",
        "translator": "translator",
        "llm_api_url": ("llm", "api_url"),
        "llm_api_key": ("llm", "api_key"),
        "llm_model": ("llm", "model"),
        "tts_concurrency": "tts_concurrency",
    }
    for cli_key, config_key in cli_map.items():
        val = getattr(args, cli_key, None)
        if val is not None:
            if isinstance(config_key, tuple):
                config[config_key[0]][config_key[1]] = val
            else:
                config[config_key] = val

    # 3) 验证必要参数
    if not config["url"] and not config["resume_from"]:
        print("❌ 必须提供 url 或 --resume-from 参数")
        sys.exit(1)

    # 4) --refine 特殊处理
    refine_val = getattr(args, "refine", None)
    if refine_val is not None:
        config["refine"]["enabled"] = refine_val > 0
        config["refine"]["max_iterations"] = refine_val
    threshold_val = getattr(args, "refine_threshold", None)
    if threshold_val is not None:
        config["refine"]["speed_threshold"] = threshold_val
    resume_iter_val = getattr(args, "resume_iteration", None)
    if resume_iter_val is not None:
        config["refine"]["resume_iteration"] = resume_iter_val
        config["refine"]["enabled"] = True
    if getattr(args, "clean_iterations", False):
        config["clean_iterations"] = True

    return config


def _deep_merge(base: dict, override: dict):
    """深度合并 override 到 base"""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ─── URL 处理 ──────────────────────────────────────────────────────
def normalize_youtube_url(url: str) -> str:
    """
    规范化 YouTube URL，统一为标准格式
    支持格式:
      - https://www.youtube.com/watch?v=VIDEO_ID
      - https://www.youtube.com/watch/VIDEO_ID  (非标准但有效)
      - https://youtu.be/VIDEO_ID
      - https://www.youtube.com/v/VIDEO_ID
      - https://www.youtube.com/embed/VIDEO_ID
    """
    if not url:
        return url

    video_id = extract_video_id(url)
    if video_id:
        # 统一为标准格式，附带原始 URL 中的 list 参数等
        normalized = f"https://www.youtube.com/watch?v={video_id}"
        if normalized != url:
            print(f"  ℹ️  URL 已规范化: {url}")
            print(f"     → {normalized}")
        return normalized
    return url


def extract_video_id(url: str) -> Optional[str]:
    """从各种 YouTube URL 格式中提取 11 位视频 ID"""
    patterns = [
        r"(?:v=)([a-zA-Z0-9_-]{11})",                          # watch?v=ID
        r"(?:youtube\.com/watch/)([a-zA-Z0-9_-]{11})",          # watch/ID
        r"(?:youtu\.be/)([a-zA-Z0-9_-]{11})",                  # youtu.be/ID
        r"(?:youtube\.com/(?:v|embed)/)([a-zA-Z0-9_-]{11})",   # /v/ID, /embed/ID
        r"(?:youtube\.com/shorts/)([a-zA-Z0-9_-]{11})",        # shorts/ID
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


# ─── 依赖检查 ──────────────────────────────────────────────────────
def _ensure_node_in_path():
    """确保 Node.js 在 PATH 中 (yt-dlp EJS 需要)"""
    import glob
    import subprocess
    
    # 优先使用 nvm 的 node（通常更新、可用）
    nvm_nodes = glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin"))
    for path in nvm_nodes:
        node_path = os.path.join(path, "node")
        if os.path.isfile(node_path):
            # 测试 node 是否可用
            try:
                subprocess.run([node_path, "--version"], capture_output=True, timeout=5, check=True)
                os.environ["PATH"] = path + ":" + os.environ.get("PATH", "")
                print(f"  🔧 使用 Node.js: {node_path}")
                return
            except Exception:
                continue
    
    # 检查系统 node 是否可用
    if shutil.which("node"):
        try:
            subprocess.run(["node", "--version"], capture_output=True, timeout=5, check=True)
            return
        except Exception:
            pass  # 系统 node 坏了，继续尝试其他选项
    
    # 最后尝试 homebrew
    for path in ["/opt/homebrew/bin", "/usr/local/bin"]:
        node_path = os.path.join(path, "node")
        if os.path.isfile(node_path):
            try:
                subprocess.run([node_path, "--version"], capture_output=True, timeout=5, check=True)
                os.environ["PATH"] = path + ":" + os.environ.get("PATH", "")
                return
            except Exception:
                continue
    
    print("  ⚠️  未找到可用的 Node.js，YouTube 下载可能失败。请安装: brew install node")


def check_dependencies(config: dict):
    """检查必要工具

    注意: 使用 importlib.util.find_spec 而非 __import__ 检查 Python 模块，
    避免实际加载模块。因为 CTranslate2（faster-whisper 后端）与 PyTorch（demucs）
    在同一进程中先后加载会导致 segfault。
    """
    import importlib.util
    missing = []
    for mod in ["faster_whisper", "edge_tts", "deep_translator", "pydub"]:
        if importlib.util.find_spec(mod) is None:
            missing.append(mod.replace("_", "-"))
    for cmd in ["ffmpeg", "ffprobe"]:
        if not shutil.which(cmd):
            missing.append(cmd)
    if not shutil.which("yt-dlp"):
        try:
            __import__("yt_dlp")
        except ImportError:
            missing.append("yt-dlp")

    if config["translator"] == "llm" or config.get("refine", {}).get("enabled"):
        try:
            import httpx
        except ImportError:
            missing.append("httpx")

    if missing:
        print(f"❌ 缺少依赖: {', '.join(missing)}")
        print(f"   pip install {' '.join(missing)}")
        sys.exit(1)
    print("✅ 依赖检查通过")


# ─── Step 1: 下载视频 ──────────────────────────────────────────────
def download_video(url: str, output_dir: Path, browser: str = "chrome",
                   download_quality: str = "best") -> Tuple[Path, str]:
    """使用 yt-dlp 下载视频
    download_quality: "best"(最高画质+帧率) / "1080p" / "720p" / "480p"
    """
    import yt_dlp

    video_path = output_dir / "original.mp4"
    if video_path.exists() and video_path.stat().st_size > 0:
        print(f"  ⏭  视频已存在，跳过下载")
        info_cache = output_dir / "info.json"
        title = "unknown"
        if info_cache.exists():
            with open(info_cache, "r") as f:
                title = json.load(f).get("title", "unknown")
        return video_path, title

    # 规范化 URL
    url = normalize_youtube_url(url)
    print(f"  📥 下载视频: {url}")
    _ensure_node_in_path()

    # 根据 download_quality 构建 format 选择器（多级 fallback 更鲁棒）
    # "best" → 不限分辨率，多级 fallback: 分离轨→合并轨→任意可用
    # "1080p"/"720p"/"480p" → 限高但多级 fallback
    quality = download_quality.lower().strip()
    if quality == "best":
        # 多级 fallback: 分离视频+音频 → 合并最佳 → 任意视频 → 任意格式
        fmt = "bestvideo+bestaudio/best/bestvideo*/best*"
        print(f"  🎬 画质: 最高可用 (多级 fallback)")
    else:
        height = int(quality.replace("p", "")) if quality.replace("p", "").isdigit() else 720
        # 限高 + 多级 fallback
        fmt = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/bestvideo[height<={height}]*/best[height<={height}]*/best"
        print(f"  🎬 画质: ≤{height}p (多级 fallback)")

    ydl_opts = {
        "format": fmt,
        "outtmpl": str(output_dir / "original.%(ext)s"),
        "quiet": False,
        "no_warnings": True,
        "cookiesfrombrowser": (browser,),
        "js_runtimes": {"node": {}},
        "retries": 15,
        "fragment_retries": 15,
        "socket_timeout": 60,
        "merge_output_format": "mp4",
        "postprocessors": [],
        "keepvideo": True,
    }

    title = "unknown"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "unknown")
            # 提取实际选中的视频格式信息（帧率、分辨率）
            req_fmts = info.get("requested_formats", [])
            vid_fmt = next((f for f in req_fmts if f.get("vcodec", "none") != "none"), {})
            with open(output_dir / "info.json", "w", encoding="utf-8") as f:
                json.dump({"title": title, "id": info.get("id", ""),
                           "url": url, "duration": info.get("duration", 0),
                           "fps": vid_fmt.get("fps") or info.get("fps"),
                           "resolution": vid_fmt.get("resolution") or info.get("resolution"),
                           },
                          f, ensure_ascii=False, indent=2)
    except Exception as e:
        if "Postprocessing" not in str(e):
            raise

    if not video_path.exists() or video_path.stat().st_size == 0:
        _manual_merge(output_dir, video_path)

    if not video_path.exists() or video_path.stat().st_size == 0:
        raise RuntimeError("视频下载/合并失败，请检查网络后重试")

    # 清理分轨文件
    for f in output_dir.glob("original.f*"):
        f.unlink(missing_ok=True)
    for f in output_dir.glob("original.temp.*"):
        f.unlink(missing_ok=True)

    print(f"  ✅ 下载完成: {title}")
    return video_path, title


def _manual_merge(output_dir: Path, video_path: Path):
    """手动合并分轨文件"""
    vid, aud = None, None
    for f in sorted(output_dir.glob("original.f*"), key=lambda x: x.stat().st_size, reverse=True):
        if f.suffix == ".mp4" and vid is None:
            vid = f
        elif f.suffix in (".webm", ".m4a") and aud is None:
            aud = f
        elif f.suffix == ".webm" and vid is None:
            vid = f
    if vid and aud and vid != aud:
        print(f"  🔧 手动合并: {vid.name} + {aud.name}")
        subprocess.run([
            "ffmpeg", "-i", str(vid), "-i", str(aud),
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(video_path), "-y"
        ], capture_output=True, check=True)
    elif vid:
        vid.rename(video_path)


# ─── Step 2: 提取音频 ──────────────────────────────────────────────
def extract_audio(video_path: Path, output_dir: Path) -> Path:
    """提取 WAV 音频"""
    audio_path = output_dir / "audio.wav"
    if audio_path.exists():
        print(f"  ⏭  音频已存在，跳过")
        return audio_path
    print(f"  🎵 提取音频...")
    subprocess.run([
        "ffmpeg", "-i", str(video_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(audio_path), "-y"
    ], capture_output=True, check=True)
    print(f"  ✅ 音频提取完成")
    return audio_path


# ─── Step 2.5: 人声/背景音分离 ─────────────────────────────────────

def separate_audio(video_path: Path, output_dir: Path,
                   config: dict = None, cpu_threads: int = 0) -> dict:
    """使用 demucs 将原始音频分离为人声和伴奏

    通过子进程运行 demucs，避免 PyTorch 的 libiomp5 与主进程中
    ctranslate2 (faster-whisper) 的 libiomp5 冲突导致死锁。

    Args:
        video_path: 原始视频路径 (original.mp4)
        output_dir: 视频输出目录
        config: audio_separation 配置

    Returns:
        dict: {"vocals": Path, "accompaniment": Path} 分离后的文件路径
    """
    config = config or {}
    vocals_path = output_dir / "audio_vocals.wav"
    accomp_path = output_dir / "audio_accompaniment.wav"

    # 缓存检查: 两个文件都存在则跳过
    if vocals_path.exists() and accomp_path.exists():
        print(f"  ⏭  音频分离结果已缓存，跳过")
        return {"vocals": vocals_path, "accompaniment": accomp_path}

    model_name = config.get("model", "htdemucs")
    device = config.get("device", "auto")

    # Step 1: 从原始视频提取高品质音频（44.1kHz 立体声）
    hq_audio = output_dir / "audio_hq.wav"
    if not hq_audio.exists():
        print(f"     提取高品质音频 (44.1kHz stereo)...")
        subprocess.run([
            "ffmpeg", "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
            str(hq_audio), "-y"
        ], capture_output=True, check=True)

    print(f"  🎛  音频分离中 (model={model_name}, device={device})...")
    print(f"     (子进程运行 demucs，避免 libiomp5 冲突)")
    t0 = time.time()

    # Step 2: 在子进程中运行 demucs，避免与 ctranslate2 的 libiomp5 死锁
    thread_limit = cpu_threads if cpu_threads > 0 else 0
    demucs_script = f'''
import os
import torch
if {thread_limit} > 0:
    torch.set_num_threads({thread_limit})
    os.environ["OMP_NUM_THREADS"] = str({thread_limit})
from demucs.pretrained import get_model, SOURCES
from demucs.separate import load_track, apply_model, save_audio
from pathlib import Path

model_name = {model_name!r}
device_cfg = {device!r}
hq_audio = Path({str(hq_audio)!r})
vocals_path = Path({str(vocals_path)!r})
accomp_path = Path({str(accomp_path)!r})

if device_cfg == "auto":
    device_str = "cuda" if torch.cuda.is_available() else "cpu"
else:
    device_str = device_cfg

model = get_model(model_name)
device_obj = torch.device(device_str)
model.to(device_obj)
model.eval()

wav = load_track(hq_audio, model.audio_channels, model.samplerate)
wav = wav.to(device_obj)

with torch.no_grad():
    sources = apply_model(model, wav.unsqueeze(0), device=device_obj,
                          shifts=1, split=True, overlap=0.25,
                          progress=False)[0]

src_map = {{name: s for name, s in zip(SOURCES, sources)}}
vocals = src_map["vocals"]
accompaniment = sum(s for name, s in src_map.items() if name != "vocals")

save_audio(vocals.cpu(), vocals_path, model.samplerate)
save_audio(accompaniment.cpu(), accomp_path, model.samplerate)

# 清理: 释放资源
del model, sources, wav, src_map, vocals, accompaniment
import gc
gc.collect()

print("DEMUCS_OK")
import sys
sys.stdout.flush()
os._exit(0)  # 强制退出，跳过 atexit 清理
'''
    result = subprocess.run(
        [sys.executable, "-c", demucs_script],
        capture_output=True, text=True, encoding="utf-8",
    )

    if result.returncode != 0 or "DEMUCS_OK" not in result.stdout:
        error_msg = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"demucs 子进程失败: {error_msg}")

    # 清理高品质中间文件
    hq_audio.unlink(missing_ok=True)

    elapsed = time.time() - t0
    print(f"  ✅ 音频分离完成 ({elapsed:.1f}s)")
    print(f"     人声: {vocals_path.name}, 伴奏: {accomp_path.name}")

    return {"vocals": vocals_path, "accompaniment": accomp_path}


# ─── Step 3: 语音识别 ──────────────────────────────────────────────
def transcribe_audio(audio_path: Path, model_size: str = "small",
                     beam_size: int = 5, cpu_threads: int = 0) -> List[dict]:
    """使用 faster-whisper 转录"""
    from faster_whisper import WhisperModel

    script_dir = Path(__file__).resolve().parent
    local_model = script_dir / "models" / f"faster-whisper-{model_size}"
    if local_model.exists() and (local_model / "model.bin").exists():
        model_path = str(local_model)
        print(f"  🎙  本地模型: {local_model.name}")
    else:
        model_path = model_size
        print(f"  🎙  从 HuggingFace 下载模型 ({model_size})...")

    threads = cpu_threads if cpu_threads > 0 else os.cpu_count() or 4
    model = WhisperModel(model_path, device="cpu", compute_type="int8",
                         cpu_threads=threads)
    segments_raw, info = model.transcribe(
        str(audio_path), language="en", beam_size=beam_size,
        word_timestamps=True, vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
    )
    segments = []
    for s in segments_raw:
        seg = {"start": s.start, "end": s.end, "text": s.text.strip()}
        # 保留 word_timestamps 用于 NLP 分句
        if s.words:
            # 借鉴 pyvideotrans: 跳过 >30 字符的 "单词" (ASR 幻觉)
            seg["words"] = [{"start": w.start, "end": w.end, "word": w.word}
                            for w in s.words if len(w.word.strip()) <= 30]
        # 跳过无效段 (end <= start)
        if seg["end"] <= seg["start"] or not seg["text"]:
            continue
        segments.append(seg)
    print(f"  ✅ 转录完成: {len(segments)} 段, 语言={info.language} ({info.language_probability:.0%})")
    return segments


# ─── Step 3.5: 去重 ────────────────────────────────────────────────
def merge_short_segments(segments: List[dict], min_chars: int = 3) -> List[dict]:
    """
    将过短的翻译片段合并到相邻段，避免 TTS 无法正常生成。
    text_zh < min_chars 的片段合并到前一段（优先）或后一段。
    合并后的时间窗口覆盖两段。
    """
    if not segments:
        return segments

    merged = []
    skip_next = False
    for i, seg in enumerate(segments):
        if skip_next:
            skip_next = False
            continue

        text_zh = seg.get("text_zh", seg.get("text", "")).strip()
        if len(text_zh) < min_chars and text_zh:
            # 尝试合并到前一段
            if merged:
                prev = merged[-1]
                prev["text_zh"] = prev.get("text_zh", "") + text_zh
                prev["text_en"] = prev.get("text_en", "") + " " + seg.get("text_en", seg.get("text", ""))
                prev["end"] = max(prev["end"], seg["end"])
                print(f"  🔗 合并短段 #{i}（\"{text_zh}\"）→ 前段")
                continue
            # 没有前段，合并到后一段
            elif i + 1 < len(segments):
                nxt = segments[i + 1].copy()
                nxt["text_zh"] = text_zh + nxt.get("text_zh", "")
                nxt["text_en"] = seg.get("text_en", seg.get("text", "")) + " " + nxt.get("text_en", nxt.get("text", ""))
                nxt["start"] = min(seg["start"], nxt["start"])
                merged.append(nxt)
                skip_next = True
                print(f"  🔗 合并短段 #{i}（\"{text_zh}\"）→ 后段")
                continue

        merged.append(seg)

    if len(merged) < len(segments):
        print(f"  🔗 短段合并: {len(segments)} → {len(merged)} 段")
    return merged


def deduplicate_segments(segments: List[dict]) -> List[dict]:
    """
    去除 Whisper 转录中的重复片段。
    检测连续出现的相同或高度相似文本，合并时间戳或移除重复项。
    也会清理迭代优化残留的重复。
    """
    if not segments:
        return segments

    deduped = [segments[0]]
    removed = 0

    for i in range(1, len(segments)):
        curr = segments[i]
        prev = deduped[-1]

        curr_text = curr.get("text", curr.get("text_en", "")).strip().lower()
        prev_text = prev.get("text", prev.get("text_en", "")).strip().lower()

        if not curr_text or not prev_text:
            deduped.append(curr)
            continue

        # 完全相同文本
        if curr_text == prev_text:
            # 合并时间戳：扩展前一段的 end 到当前段的 end
            deduped[-1]["end"] = max(prev["end"], curr["end"])
            removed += 1
            continue

        # 高度相似：一个是另一个的子串（Whisper 有时会拆分或重复部分内容）
        if (len(curr_text) > 5 and len(prev_text) > 5 and
            (curr_text in prev_text or prev_text in curr_text)):
            # 保留较长的那个，合并时间戳
            if len(curr_text) > len(prev_text):
                deduped[-1] = curr
                deduped[-1]["start"] = min(prev["start"], curr["start"])
            deduped[-1]["end"] = max(prev["end"], curr["end"])
            removed += 1
            continue

        deduped.append(curr)

    if removed > 0:
        print(f"  🔄 去重: 移除 {removed} 个重复片段 ({len(segments)} → {len(deduped)})")
    return deduped


# ─── Step 3.6: NLP 分句优化 ───────────────────────────────────────────

def _nlp_resegment(segments: List[dict]) -> List[dict]:
    """
    用 spaCy 检测英文句子边界，优化 Whisper 分段:
    - Split: 含多个完整句子 且 duration > 8s 的段 → 按 word timestamp 切分
    - Merge: 相邻段都 < 1.5s 且属同一句 → 合并
    完成后 strip words 字段（不写入 cache）。
    """
    try:
        import spacy
    except ImportError:
        print("  ⚠️  spaCy 未安装，跳过 NLP 分句 (pip install spacy && python -m spacy download en_core_web_sm)")
        return segments

    try:
        nlp = spacy.load("en_core_web_sm")
    except OSError:
        print("  ⚠️  spaCy 模型 en_core_web_sm 未下载，跳过 NLP 分句")
        return segments

    result = []
    split_count = 0
    merge_count = 0

    # ── Pass 1: Split long multi-sentence segments ──
    split_segments = []
    for seg in segments:
        duration = seg["end"] - seg["start"]
        text = seg.get("text", "").strip()
        words = seg.get("words")

        # 只对有 word_timestamps 且 duration > 8s 的段尝试 split
        if duration <= 8.0 or not words or not text:
            split_segments.append(seg)
            continue

        doc = nlp(text)
        sentences = list(doc.sents)
        if len(sentences) < 2:
            split_segments.append(seg)
            continue

        # 尝试按句子边界切分
        # 建立 char→word index 映射
        char_pos = 0
        word_char_ranges = []
        for w in words:
            w_text = w["word"].strip()
            # 找到 word 在 text 中的位置
            idx = text.find(w_text, char_pos)
            if idx == -1:
                idx = char_pos
            word_char_ranges.append((idx, idx + len(w_text)))
            char_pos = idx + len(w_text)

        # 找到每个句子边界对应的 word index
        new_segs = []
        sent_start_word = 0
        for sent_idx, sent in enumerate(sentences):
            sent_end_char = sent.end_char
            # 找到最后一个 word 结束位置 <= sent_end_char 的 word
            sent_end_word = sent_start_word
            for wi in range(sent_start_word, len(word_char_ranges)):
                if word_char_ranges[wi][0] < sent_end_char:
                    sent_end_word = wi
                else:
                    break

            # 构建新段
            seg_words = words[sent_start_word:sent_end_word + 1]
            if seg_words:
                new_seg = {
                    "start": seg_words[0]["start"],
                    "end": seg_words[-1]["end"],
                    "text": sent.text.strip(),
                    "words": seg_words,
                }
                new_segs.append(new_seg)

            sent_start_word = sent_end_word + 1

        if len(new_segs) >= 2:
            # 修正首段 start 和末段 end 对齐原段
            new_segs[0]["start"] = seg["start"]
            new_segs[-1]["end"] = seg["end"]
            split_segments.extend(new_segs)
            split_count += len(new_segs) - 1
        else:
            split_segments.append(seg)

    # ── Pass 2: Merge adjacent short segments (<1.5s) belonging to same sentence ──
    if len(split_segments) < 2:
        result = split_segments
    else:
        # 拼接全文用于句子边界判断
        all_text = " ".join(s.get("text", "") for s in split_segments)
        doc_all = nlp(all_text)
        # 建立 char offset → sentence index 映射
        sent_boundaries = [(sent.start_char, sent.end_char) for sent in doc_all.sents]

        def get_sent_idx(char_offset):
            for idx, (sc, ec) in enumerate(sent_boundaries):
                if sc <= char_offset < ec:
                    return idx
            return -1

        # 计算每段在 all_text 中的 char offset
        seg_char_offsets = []
        offset = 0
        for s in split_segments:
            seg_char_offsets.append(offset)
            offset += len(s.get("text", "")) + 1  # +1 for the space

        result = [split_segments[0]]
        for i in range(1, len(split_segments)):
            prev_seg = result[-1]
            curr_seg = split_segments[i]
            prev_dur = prev_seg["end"] - prev_seg["start"]
            curr_dur = curr_seg["end"] - curr_seg["start"]

            # 只有两段都短(<1.5s) 且属于同一句子才合并
            if prev_dur < 1.5 and curr_dur < 1.5:
                prev_sent = get_sent_idx(seg_char_offsets[i - 1])
                curr_sent = get_sent_idx(seg_char_offsets[i])
                if prev_sent == curr_sent and prev_sent >= 0:
                    # 合并
                    merged_text = (prev_seg.get("text", "") + " " + curr_seg.get("text", "")).strip()
                    merged_words = prev_seg.get("words", []) + curr_seg.get("words", [])
                    result[-1] = {
                        "start": prev_seg["start"],
                        "end": curr_seg["end"],
                        "text": merged_text,
                        "words": merged_words,
                    }
                    merge_count += 1
                    continue

            result.append(curr_seg)

    # ── Strip words field ──
    for seg in result:
        seg.pop("words", None)

    if split_count > 0 or merge_count > 0:
        print(f"  🧠 NLP 分句: split +{split_count}, merge -{merge_count} ({len(segments)} → {len(result)} 段)")
    return result


# ─── Step 4: 翻译 ──────────────────────────────────────────────────

def translate_segments(segments: List[dict], config: dict) -> List[dict]:
    """根据配置选择翻译引擎"""
    engine = config.get("translator", "google")
    if engine == "llm":
        llm_cfg = config["llm"]
        video_title = config.get("video_title", "")
        # 获取输出目录用于保存风格检测等 JSON
        output_dir = None
        if config.get("resume_from"):
            output_dir = Path(config["resume_from"])
        if llm_cfg.get("two_pass", False):
            return _translate_llm_two_pass(segments, llm_cfg, video_title, output_dir=output_dir)
        return _translate_llm(segments, llm_cfg, video_title, output_dir=output_dir)
    else:
        return _translate_google(segments)


def _translate_google(segments: List[dict], batch_size: int = 20) -> List[dict]:
    """Google Translate 引擎"""
    from deep_translator import GoogleTranslator
    print(f"  🌐 Google 翻译 ({len(segments)} 段)...")
    translator = GoogleTranslator(source="en", target="zh-CN")
    result = []
    for i in range(0, len(segments), batch_size):
        batch = segments[i:i + batch_size]
        texts = [s["text"] for s in batch]
        try:
            translations = translator.translate_batch(texts)
        except Exception:
            translations = []
            for t in texts:
                try:
                    translations.append(translator.translate(t))
                except Exception:
                    translations.append(t)
                time.sleep(0.5)
        for seg, zh in zip(batch, translations):
            text_zh = zh if (zh and len(zh.strip()) >= 2) else seg["text"]
            result.append({
                "start": seg["start"], "end": seg["end"],
                "text_en": seg["text"], "text_zh": text_zh,
            })
        print(f"     进度: {min(i+batch_size, len(segments))}/{len(segments)}")
        if i + batch_size < len(segments):
            time.sleep(1)
    print(f"  ✅ 翻译完成")
    return result


def _detect_translation_style(segments: List[dict], video_title: str,
                               endpoint: str, headers: dict, model: str,
                               temperature: float, output_dir: Path = None) -> str:
    """扫描完整视频内容后，识别主题和翻译风格，返回追加到 system_prompt 的指导规则。

    策略：把全部英文原文拼接后送给 LLM 做一次完整扫描（专用轮次），而非只看开头。
    - 如果总文本 < 8000 字符，直接全量发送
    - 如果过长，均匀采样（头部 30% + 中部 40% + 尾部 30%），保证覆盖视频各阶段
    这样即使开头是广告/赞助商口播/无关寒暄，也不会误判整体主题。
    """
    import httpx

    if not segments:
        return ""

    # ── 构建完整文本，必要时均匀采样 ──
    all_texts = [s["text"] for s in segments if s.get("text", "").strip()]
    full_text = "\n".join(all_texts)

    MAX_CHARS = 8000  # LLM 上下文窗口预算（留足空间给 prompt + response）
    if len(full_text) > MAX_CHARS:
        # 均匀采样：头部 30% + 中部 40% + 尾部 30%
        n = len(all_texts)
        head_end = int(n * 0.3)
        mid_start = int(n * 0.3)
        mid_end = int(n * 0.7)
        tail_start = int(n * 0.7)

        head = all_texts[:head_end]
        mid = all_texts[mid_start:mid_end]
        tail = all_texts[tail_start:]

        # 在各段之间加标记，让 LLM 知道这是采样
        sample_parts = []
        sample_parts.append(f"=== 视频前段 (第1~{head_end}段，共{n}段) ===")
        sample_parts.append("\n".join(head))
        sample_parts.append(f"\n=== 视频中段 (第{mid_start+1}~{mid_end}段) ===")
        sample_parts.append("\n".join(mid))
        sample_parts.append(f"\n=== 视频后段 (第{tail_start+1}~{n}段) ===")
        sample_parts.append("\n".join(tail))
        sample_text = "\n".join(sample_parts)

        # 如果还是太长，按比例截断每部分
        if len(sample_text) > MAX_CHARS:
            budget_per_part = MAX_CHARS // 3
            head_text = "\n".join(head)[:budget_per_part]
            mid_text = "\n".join(mid)[:budget_per_part]
            tail_text = "\n".join(tail)[:budget_per_part]
            sample_text = (
                f"=== 视频前段 ===\n{head_text}\n"
                f"=== 视频中段 ===\n{mid_text}\n"
                f"=== 视频后段 ===\n{tail_text}"
            )
        content_desc = f"均匀采样 {n} 段（头/中/尾各约 30%/40%/30%）"
    else:
        sample_text = full_text
        content_desc = f"完整内容 {len(all_texts)} 段"

    print(f"     🔍 主题识别中（{content_desc}）...")

    detect_prompt = f"""你是翻译领域专家。请仔细阅读以下视频的完整英文内容，分析其核心主题、专业领域和翻译注意事项。

注意：
- 视频开头可能有广告、赞助商口播、寒暄等与主题无关的内容，请忽略这些，聚焦于视频的核心主题
- 请综合头、中、尾部内容做整体判断，不要只看开头

视频标题: {video_title or '(无标题)'}

英文原文:
{sample_text}

请用以下JSON格式返回（只返回JSON，不要其他内容）:
{{
  "topic": "视频核心主题（如: 线性代数/量子力学/React前端开发/宏观经济学/日常Vlog 等，尽量具体）",
  "style": "建议的翻译风格（如: 学术严谨/口语化教学/新闻播报/技术文档/轻松聊天）",
  "term_rules": [
    "列出本视频中出现的专业术语翻译规则，每条格式: 英文 → 中文",
    "只列出真正在原文中出现过的术语，不要凭空臆造"
  ],
  "warnings": [
    "列出本视频翻译中需要特别注意的陷阱",
    "如: 某个常见词在本视频的专业语境中有特殊含义",
    "如: 容易被误译的符号、缩写、双关语等"
  ]
}}"""

    try:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是翻译领域专家。请基于视频完整内容做全局分析，不要仅凭开头几段下结论。"},
                {"role": "user", "content": detect_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 1500,
        }
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()

        # 提取 JSON（LLM 可能在外面包了 ```json ... ```）
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            print(f"     ⚠️  主题识别返回格式异常，使用通用规则")
            return _default_translation_rules()
        detected = json.loads(json_match.group())

        # 构建追加到 system_prompt 的风格指导
        guide_parts = []
        topic = detected.get("topic", "")
        style = detected.get("style", "")
        if topic:
            guide_parts.append(f"\n本视频主题: {topic}")
        if style:
            guide_parts.append(f"翻译风格: {style}")

        # 术语规则
        term_rules = detected.get("term_rules", [])
        if term_rules:
            guide_parts.append("专业术语翻译规则（必须遵守）:")
            for rule in term_rules[:15]:  # 限制不超过15条
                guide_parts.append(f"  - {rule}")

        # 翻译陷阱警告
        warnings = detected.get("warnings", [])
        if warnings:
            guide_parts.append("翻译注意事项:")
            for w in warnings[:8]:
                guide_parts.append(f"  - {w}")

        # 始终注入的通用保护规则
        guide_parts.append(_default_translation_rules())

        result = "\n".join(guide_parts)
        print(f"     ✅ 主题: {topic} | 风格: {style} | 术语规则: {len(term_rules)} 条")
        if term_rules:
            for rule in term_rules[:5]:
                print(f"        · {rule}")
            if len(term_rules) > 5:
                print(f"        ... 共 {len(term_rules)} 条")

        # 保存风格检测结果 JSON (便于 review/debug)
        if output_dir:
            style_path = _audit_dir(output_dir) / "style_detection.json"
            style_data = {"topic": topic, "style": style,
                          "term_rules": term_rules, "warnings": warnings}
            with open(style_path, "w", encoding="utf-8") as _f:
                json.dump(style_data, _f, ensure_ascii=False, indent=2)

        return result

    except Exception as e:
        print(f"     ⚠️  主题识别失败 ({e})，使用通用翻译规则")
        return _default_translation_rules()


def _default_translation_rules() -> str:
    """通用翻译保护规则，无论主题识别是否成功都会注入"""
    return (
        "\n通用翻译规则（始终遵守）:"
        "\n  - 禁止使用任何 Markdown 格式：不要使用 **加粗**、*斜体*、`反引号`、# 标题等标记"
        "\n  - 代码相关词汇（函数名、变量名、类名等）直接用中文描述或保留原文，不要用反引号包裹"
        "\n  - 数学符号（i, e, π, θ 等）在数学/科学语境中保持为专业术语，不可翻译为日常用语（如 i→'我'）"
        "\n  - 负号'-'在数学/科学语境中必须翻译为'负'，不可省略（字幕'-3'应读作'负三'）"
        "\n  - 英文倒装句（there be, 状语前置等）翻译时需调整为中文习惯语序"
        "\n  - 翻译结果用于语音配音朗读，需通顺自然，适合听觉理解，输出纯文本"
        "\n  - 前后文语义连贯，避免相邻段之间出现语义断裂或内容重复"
        "\n  - 译文长度应与原文时长匹配：短句译文要简洁，长句可适当展开，避免配音时语速异常"
    )


def _load_prompt_template(template_path: str) -> str:
    """加载外部提示词模板文件，返回模板内容。路径不存在则返回空字符串。"""
    if not template_path:
        return ""
    # 支持相对路径（相对于项目根目录）
    p = Path(template_path)
    if not p.is_absolute():
        p = Path(__file__).parent / template_path
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


def _detect_batch_hallucination(translations: List[str], prev_context: List[str] = None) -> set:
    """检测批内幻觉：相同译文出现 3+ 次（或占 25%+ 批次），或与上下文窗口重复 2+ 次。
    返回应标记为失败的 translation index 集合。"""
    from collections import Counter
    non_empty = [(i, t.strip()) for i, t in enumerate(translations) if t and t.strip()]
    if len(non_empty) < 3:
        return set()
    counts = Counter(t for _, t in non_empty)
    threshold = max(3, int(len(translations) * 0.25))
    hallucinated_texts = {text for text, cnt in counts.items() if cnt >= threshold}
    # 与 prev_context 重复检测：同一短语在本批出现 2+ 次且存在于上下文中
    if prev_context:
        prev_set = set(prev_context)
        for text, cnt in counts.items():
            if cnt >= 2 and text in prev_set:
                hallucinated_texts.add(text)
    return {i for i, t in non_empty if t in hallucinated_texts}


def _check_batch_alignment(batch: List[dict], translations: List[str]) -> List[int]:
    """检测批内跨段内容错位 — 通用方案，无领域专属词典。

    三层信号融合检测:
      1. 正向锚点: 英文中的数字/缩写/专有名词 → 检查出现在哪段译文
      2. 反向锚点: 中文译文中保留的英文单词 → 反向追溯到哪段英文
      3. 长度比异常: EN-ZH 字符比率偏离局部中位数
    得分 ≥ 1.5 判定为错位，返回需重试的 index 列表。
    """
    if len(batch) < 3:
        return []

    # ── 通用句首词排除集 (避免句首大写被误判为专有名词) ──
    _COMMON_CAPS = {
        'The', 'This', 'That', 'These', 'Those', 'What', 'When', 'Where',
        'Which', 'How', 'And', 'But', 'For', 'Not', 'You', 'They', 'His',
        'Her', 'Its', 'Our', 'Are', 'Can', 'Will', 'Has', 'Have', 'Had',
        'Was', 'Were', 'May', 'Let', 'Now', 'Here', 'There', 'Also', 'Just',
        'Some', 'All', 'Any', 'Each', 'Every', 'Much', 'Many', 'More', 'Most',
        'Such', 'Very', 'Well', 'Then', 'Than', 'Once', 'Only', 'Even',
        'Still', 'About', 'After', 'Before', 'Over', 'Under', 'So', 'If',
    }

    def _extract_anchors(en_text):
        """通用锚点提取: 数字、大写缩写、专有名词 (纯正则，无词典)"""
        anchors = set()
        # 数字 (含小数、百分比)
        for m in re.findall(r'\d+(?:\.\d+)?%?', en_text):
            if len(m) >= 2:  # 跳过单个数字 (太模糊)
                anchors.add(('num', m))
        # 全大写缩写 (>=2字符): GPU, DNA, API, HTTP
        for m in re.findall(r'\b[A-Z]{2,}\b', en_text):
            anchors.add(('acr', m))
        # 非句首大写词 = 专有名词 (>2字符, 排除常见词)
        words = en_text.split()
        for k, w in enumerate(words):
            clean = re.sub(r'[^a-zA-Z]', '', w)
            if clean and clean[0].isupper() and k > 0 and len(clean) > 2:
                if clean not in _COMMON_CAPS:
                    anchors.add(('name', clean))
        return anchors

    def _anchor_in_zh(anchor, zh_text):
        """检查锚点是否出现在中文译文中 (通用匹配)"""
        kind, val = anchor
        if kind == 'num':
            return val in zh_text
        # 缩写和专有名词: 中文常保留英文原词
        return val.lower() in zh_text.lower()

    en_texts = [b.get("text", "") for b in batch]
    zh_texts = [t if t else "" for t in translations]
    scores = [0.0] * len(batch)

    # ── 信号 1: 正向锚点 (EN 关键词 → 检查 ZH 位置) ──
    for i in range(len(batch)):
        anchors = _extract_anchors(en_texts[i])
        if len(anchors) < 2:
            continue
        hits_self = sum(1 for a in anchors if _anchor_in_zh(a, zh_texts[i]))
        for delta in [-1, 1]:
            j = i + delta
            if j < 0 or j >= len(zh_texts):
                continue
            hits_nb = sum(1 for a in anchors if _anchor_in_zh(a, zh_texts[j]))
            if hits_self == 0 and hits_nb >= 2:
                scores[i] += 2.0
            elif hits_nb >= hits_self + 2 and hits_nb >= 3:
                scores[i] += 1.5

    # ── 信号 2: 反向锚点 (ZH 中保留的英文 → 反向追溯 EN) ──
    _trivial = {'the', 'a', 'an', 'of', 'in', 'to', 'and', 'or', 'is', 'it',
                'at', 'on', 'by', 'as', 'so', 'if', 'no', 'do', 'be', 'we', 'he'}
    for i, zh in enumerate(zh_texts):
        preserved = {m.lower() for m in re.findall(r'[A-Za-z]{2,}', zh)
                     if m.lower() not in _trivial}
        if not preserved:
            continue
        en_lower = en_texts[i].lower()
        hits_self = sum(1 for w in preserved if w in en_lower)
        for delta in [-1, 1]:
            j = i + delta
            if 0 <= j < len(en_texts):
                hits_nb = sum(1 for w in preserved if w in en_texts[j].lower())
                if hits_nb > hits_self and hits_nb >= 2:
                    scores[i] += 1.5

    # ── 信号 3: 长度比异常 (EN-ZH 字符比率偏离局部中位数) ──
    def _zh_chars(text):
        return sum(1 for c in text if '\u4e00' <= c <= '\u9fff')

    ratios = [_zh_chars(zh) / max(len(en), 1) for en, zh in zip(en_texts, zh_texts)]
    for i in range(len(ratios)):
        window = sorted(ratios[max(0, i - 2):min(len(ratios), i + 3)])
        if len(window) >= 3:
            median = window[len(window) // 2]
            if median > 0.05:
                if ratios[i] > median * 3.0 or ratios[i] < median * 0.2:
                    scores[i] += 1.0

    return [i for i, s in enumerate(scores) if s >= 1.5]


def _translate_llm(segments: List[dict], llm_config: dict, video_title: str = "", output_dir: Path = None) -> List[dict]:
    """LLM 大模型翻译引擎 (OpenAI 兼容 API)"""
    import httpx

    api_url = llm_config["api_url"].rstrip("/")
    api_key = llm_config["api_key"]
    model = llm_config["model"]
    system_prompt = llm_config.get("system_prompt", "将英文翻译为中文，只输出翻译结果。")
    batch_size = llm_config.get("batch_size", 15)
    temperature = llm_config.get("temperature", 0.3)
    style = llm_config.get("style", "")
    # 外部提示词模板优先于内置 system_prompt
    template_content = _load_prompt_template(llm_config.get("prompt_template", ""))
    if template_content:
        system_prompt = template_content
        print(f"     📄 使用外部提示词模板: {llm_config['prompt_template']}")
    if style:
        system_prompt += f"\n翻译风格要求：{style}"

    if not api_key:
        print("  ⚠️  LLM api_key 未设置，降级为 Google 翻译")
        return _translate_google(segments)

    print(f"  🤖 LLM 翻译 ({model}, {len(segments)} 段)...")

    # 确定 chat completions endpoint
    if "/chat/completions" not in api_url:
        endpoint = f"{api_url}/chat/completions"
    else:
        endpoint = api_url

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ── 翻译前: 自动识别主题和翻译风格 ──
    # 用视频标题 + 前几段内容让 LLM 判断专业领域，注入术语保护规则
    if not style:
        topic_guide = _detect_translation_style(
            segments, video_title, endpoint, headers, model, temperature,
            output_dir=output_dir
        )
        if topic_guide:
            system_prompt += f"\n{topic_guide}"
            print(f"     📋 自动识别翻译风格已注入")

    result = []
    prev_context = None
    for batch_idx, i in enumerate(range(0, len(segments), batch_size)):
        batch = segments[i:i + batch_size]
        # 构造批量翻译请求：每行一句，用编号标记
        CHARS_PER_SEC = 4.5  # TTS 中文语速：约 4.5 字/秒
        lines = []
        char_hints = []
        for j, seg in enumerate(batch):
            lines.append(f"[{j+1}] {seg['text']}")
            dur_sec = seg.get("end", 0) - seg.get("start", 0)
            target_chars = max(2, int(dur_sec * CHARS_PER_SEC))
            char_hints.append(f"[{j+1}]≈{target_chars}字")
        user_msg = "\n".join(lines)
        hint_line = f"各句参考字数：{', '.join(char_hints)}"

        # 构造上下文提示（滑动窗口：6句回看 + 3句前瞻）
        context_hint = ""
        if video_title:
            context_hint += f"视频主题：{video_title}\n"
        if prev_context:
            # 上下文毒化检测：同一短语占比过高 → 重置，防止幻觉级联
            from collections import Counter as _Counter
            _pc_counts = _Counter(prev_context)
            _top_phrase, _top_cnt = _pc_counts.most_common(1)[0]
            if _top_cnt >= max(2, int(len(prev_context) * 0.4)):
                print(f"     ⚠️  上下文窗口污染 ('{_top_phrase[:12]}...' ×{_top_cnt})，重置")
                prev_context = None
        if prev_context:
            context_hint += f"前文：{'；'.join(prev_context)}\n"
        # 前瞻：取当前批次之后 3 句英文原文供理解语境
        next_start = i + batch_size
        if next_start < len(segments):
            next_preview = [segments[k]["text"][:60] for k in range(next_start, min(next_start + 3, len(segments)))]
            context_hint += f"下文预览：{'；'.join(next_preview)}\n"

        batch_prompt = (
            f"{system_prompt}\n\n"
            + (f"{context_hint}\n" if context_hint else "")
            + f"前文和下文仅供理解语境，翻译只针对当前批次编号内容。\n"
            f"请翻译以下 {len(batch)} 句话，每句保持 [编号] 格式，"
            f"一行一句，不要合并或拆分。\n"
            f"{hint_line}\n"
            f"注意：参考字数仅供控制译文长度，不要在译文中输出字数标注。"
            f"计算机缩写保留英文原词，不加括号注音。"
            f"忠实原文语义，不要曲解也不要过度扩充。\n\n{user_msg}"
        )

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": batch_prompt},
            ],
            "temperature": temperature,
            "max_tokens": 4096,
        }

        # ── 批量翻译请求（带重试） ──
        translations = None
        batch_max_retries = 2  # 批量请求最多重试 2 次
        for batch_attempt in range(batch_max_retries + 1):
            try:
                with httpx.Client(timeout=60.0) as client:
                    resp = client.post(endpoint, json=payload, headers=headers)
                    resp.raise_for_status()
                    content = resp.json()["choices"][0]["message"]["content"].strip()

                # 解析返回的编号格式
                translations = _parse_numbered_translations(content, len(batch))
                # 对齐保证：验证解析结果数量是否匹配
                if len([t for t in translations if t.strip()]) < len(batch) * 0.7:
                    if batch_attempt < batch_max_retries:
                        print(f"     ⚠️  LLM 批次对齐失败 ({len([t for t in translations if t.strip()])} vs {len(batch)})，"
                              f"重试 {batch_attempt+1}/{batch_max_retries}...")
                        time.sleep(2 * (batch_attempt + 1))
                        continue
                    print(f"     ⚠️  LLM 批次对齐仍失败，降级逐条翻译")
                    translations = _translate_llm_single(batch, endpoint, headers, model, system_prompt, temperature)
                # 批内幻觉检测：相同译文重复出现 → 标记为空，走逐段重试
                halluc_indices = _detect_batch_hallucination(translations, prev_context)
                if halluc_indices:
                    halluc_sample = translations[list(halluc_indices)[0]] if halluc_indices else ""
                    print(f"     ⚠️  检测到批内幻觉 ({len(halluc_indices)} 段, "
                          f"'{halluc_sample[:15]}...')，降级逐段翻译")
                    for hi in halluc_indices:
                        translations[hi] = ""
                break  # 成功则退出重试循环
            except Exception as e:
                if batch_attempt < batch_max_retries:
                    print(f"     ⚠️  LLM 批次 {batch_idx+1} 请求失败: {e}，重试 {batch_attempt+1}/{batch_max_retries}...")
                    time.sleep(2 * (batch_attempt + 1))
                else:
                    print(f"     ⚠️  LLM 批次 {batch_idx+1} 重试耗尽: {e}，降级逐条翻译")
                    translations = _translate_llm_single(batch, endpoint, headers, model, system_prompt, temperature)

        batch_results = []
        failed_indices = []  # 记录翻译失败的段（需要回退 Google）
        for j, (seg, zh) in enumerate(zip(batch, translations)):
            # 校验：翻译过短（<2字符且原文>10字符）视为解析失败
            if zh and len(zh.strip()) >= 2:
                text_zh = zh.strip()
            else:
                text_zh = None  # 标记为失败
                failed_indices.append(j)
            if text_zh:
                # 安全网：确保 text_zh 没有 [N] 前缀泄漏
                if re.match(r"^\[\d+\]", text_zh):
                    text_zh = _strip_numbered_prefix(text_zh)
                # 清除 LLM 可能输出的 markdown 格式标记
                text_zh = _strip_markdown(text_zh, seg["text"])
                # 深度安全网：清除文本中间任何残留的 [N] 标号
                text_zh = re.sub(r'\[\d+\]\s*', '', text_zh)
            batch_results.append({
                "start": seg["start"], "end": seg["end"],
                "text_en": seg["text"], "text_zh": text_zh or seg["text"],
            })

        # 对失败的段：先用 LLM 逐条重试，仍失败才回退 Google
        if failed_indices:
            # ── 第一层兜底：LLM 逐条重试（3次） ──
            print(f"     ⚠️  {len(failed_indices)} 段批量解析失败，LLM 逐条重试中...")
            retry_batch = [batch[j] for j in failed_indices]
            retry_results = _translate_llm_single(
                retry_batch, endpoint, headers, model, system_prompt, temperature
            )
            still_failed = []
            for k, j in enumerate(failed_indices):
                zh = retry_results[k]
                if zh and len(zh.strip()) >= 2:
                    batch_results[j]["text_zh"] = zh.strip()
                    print(f"       ✅ LLM 逐条重试成功: \"{batch[j]['text'][:30]}\"")
                else:
                    still_failed.append(j)

            # ── 第二层兜底：Google Translate（仅 LLM 逐条也失败的段） ──
            if still_failed:
                print(f"     ⚠️  {len(still_failed)} 段 LLM 逐条重试仍失败，回退 Google Translate")
                try:
                    from deep_translator import GoogleTranslator
                    gt = GoogleTranslator(source="en", target="zh-CN")
                    for j in still_failed:
                        seg = batch[j]
                        try:
                            gt_zh = gt.translate(seg["text"])
                            if gt_zh and len(gt_zh.strip()) >= 2:
                                batch_results[j]["text_zh"] = gt_zh.strip()
                                print(f"       ✅ Google 翻译成功: \"{seg['text'][:30]}\"")
                            else:
                                print(f"       ⚠️  Google 翻译也为空，保留原文: \"{seg['text'][:30]}\"")
                        except Exception as e:
                            print(f"       ⚠️  Google 翻译失败 ({e})，保留原文: \"{seg['text'][:30]}\"")
                        time.sleep(0.3)
                except ImportError:
                    print(f"       ⚠️  deep_translator 未安装，无法回退 Google 翻译")

        # ── 跨段错位检测: 锚定术语交叉相似度 ──
        batch_zhs = [br["text_zh"] for br in batch_results]
        misaligned = _check_batch_alignment(batch, batch_zhs)
        if misaligned:
            print(f"     ⚠️  检测到 {len(misaligned)} 段跨段错位，逐段重译修复...")
            retry_segs = [batch[j] for j in misaligned]
            retry_zhs = _translate_llm_single(
                retry_segs, endpoint, headers, model, system_prompt, temperature
            )
            for k, j in enumerate(misaligned):
                zh = retry_zhs[k]
                if zh and len(zh.strip()) >= 2:
                    batch_results[j]["text_zh"] = zh.strip()
                    print(f"       ✅ 错位修复: #{i+j} \"{batch[j]['text'][:30]}\"")

        for br in batch_results:
            result.append(br)

        # 保存最后几句作为下一批的上下文（6句滑动窗口提升跨批次连贯性）
        if batch_results:
            prev_context = [r["text_zh"] for r in batch_results[-6:]]

        print(f"     进度: {min(i+batch_size, len(segments))}/{len(segments)}")

    print(f"  ✅ LLM 翻译完成")

    # ── 等时翻译：多候选长度优化 ──
    isometric_n = llm_config.get("isometric", 0)
    if isometric_n > 0 and llm_config.get("api_key"):
        cps_threshold = llm_config.get("isometric_cps_threshold", 5.5)
        high_cps = _identify_high_cps_segments(result, cps_threshold)
        if high_cps:
            print(f"  🎯 等时翻译: {len(high_cps)}/{len(result)} 段估算 CPS > {cps_threshold}，生成多候选...")
            result = _isometric_translate_batch(result, high_cps, llm_config)
        else:
            print(f"  ✅ 等时翻译: 全部段 CPS 合规，无需多候选")

        # ── 等时扩展：低 CPS 段多候选扩展 ──
        expand_threshold = llm_config.get("isometric_expand_cps_threshold", 3.5)
        low_cps = _identify_low_cps_segments(result, expand_threshold)
        if low_cps:
            print(f"  📐 等时扩展: {len(low_cps)}/{len(result)} 段估算 CPS < {expand_threshold}，生成多候选...")
            result = _isometric_expand_batch(result, low_cps, llm_config)

    return result


def _translate_llm_two_pass(segments: List[dict], llm_config: dict, video_title: str = "", output_dir: Path = None) -> List[dict]:
    """两步翻译: Pass 1 忠实直译 → Pass 2 配音改编 (参考 VideoLingo 三步法)"""
    import httpx
    import copy

    print(f"  🔄 两步翻译模式 (Pass 1: 忠实直译 → Pass 2: 配音改编)")

    # ── Pass 1: 忠实直译 ──
    # 使用修改后的 system_prompt 强调忠实性
    pass1_config = copy.deepcopy(llm_config)
    pass1_config["system_prompt"] = (
        "你是专业的英中翻译引擎。请逐句忠实翻译以下英文，要求：\n"
        "1) 保留原文所有信息点，不遗漏不添加\n"
        "2) 直译为主，保持与原文的一一对应关系\n"
        "3) 计算机缩写保留英文原词（如 API、SDK、HTTP、GPU 等），不加括号注音\n"
        "4) 只输出翻译结果，不要解释"
    )
    # 清除外部模板，Pass 1 使用固定 prompt
    pass1_config["prompt_template"] = ""
    pass1_config["style"] = ""

    print(f"  📝 Pass 1: 忠实直译...")
    pass1_results = _translate_llm(segments, pass1_config, video_title, output_dir=output_dir)

    # ── Pass 2: 配音改编 ──
    # 输入 = 英文原文 + Pass 1 直译，输出 = 适合配音的自然中文
    api_url = llm_config["api_url"].rstrip("/")
    api_key = llm_config["api_key"]
    model = llm_config["model"]
    batch_size = llm_config.get("batch_size", 8)
    temperature = llm_config.get("temperature", 0.3)
    endpoint = api_url if "/chat/completions" in api_url else f"{api_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    CHARS_PER_SEC = 4.5
    adapt_system = (
        "你是视频配音翻译改编专家。将直译版改写为适合口语配音朗读的自然中文。\n"
        "要求：\n"
        "1) 保持原文语义完整，不得遗漏信息\n"
        "2) 使表达更口语化、节奏更适合朗读\n"
        "3) 计算机缩写保留英文原词（如 API、SDK、HTTP、GPU 等），不加括号注音\n"
        "4) 忠实原文语义，不要为凑字数曲解也不要过度扩充\n"
        "5) 译文长度尽量匹配参考字数（用于控制配音时长）\n"
        "6) 每句保持 [编号] 格式，一行一句\n"
        "7) 不要输出解释，只输出改编结果"
    )

    print(f"  🎙️  Pass 2: 配音改编...")
    final_results = list(pass1_results)  # 复制 Pass 1 结果作为 fallback
    pass2_adapted = 0
    pass2_fallback = 0

    for i in range(0, len(pass1_results), batch_size):
        batch = pass1_results[i:i + batch_size]
        lines = []
        for j, seg in enumerate(batch):
            dur_sec = seg.get("end", 0) - seg.get("start", 0)
            target_chars = max(2, int(dur_sec * CHARS_PER_SEC))
            lines.append(
                f"[{j+1}] EN: {seg['text_en']}\n"
                f"     直译: {seg['text_zh']}\n"
                f"     目标≈{target_chars}字"
            )

        user_msg = (
            f"请将以下 {len(batch)} 段直译改编为配音用自然中文，"
            f"每句用 [编号] 格式输出：\n\n" + "\n\n".join(lines)
        )

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": adapt_system},
                {"role": "user", "content": user_msg},
            ],
            "temperature": temperature,
            "max_tokens": 4096,
        }

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"].strip()
                content = _strip_think_block(content)

            adaptations = _parse_numbered_translations(content, len(batch))

            # 幻觉检测: Pass 2 也可能幻觉
            halluc = _detect_batch_hallucination(adaptations)

            for j, (seg, adapted) in enumerate(zip(batch, adaptations)):
                if j in halluc:
                    pass2_fallback += 1
                    continue  # 保持 Pass 1 结果
                if adapted and len(adapted.strip()) >= 2:
                    clean = _strip_numbered_prefix(adapted) if re.match(r"^\[\d+\]", adapted) else adapted
                    clean = _strip_markdown(clean, seg["text_en"])
                    # 语义校验: 改编结果不应与直译完全无关
                    if len(clean) >= 2:
                        final_results[i + j]["text_zh"] = clean
                        pass2_adapted += 1
                    else:
                        pass2_fallback += 1
                else:
                    pass2_fallback += 1
        except Exception as e:
            print(f"     ⚠️  Pass 2 批次失败: {e}，保留 Pass 1 结果")
            pass2_fallback += len(batch)

    print(f"     Pass 2 采纳: {pass2_adapted} 段, 回退 Pass 1: {pass2_fallback} 段")
    print(f"  ✅ 两步翻译完成 ({len(final_results)} 段)")

    # ── 等时翻译：多候选长度优化（Pass 2 之后）──
    isometric_n = llm_config.get("isometric", 0)
    if isometric_n > 0 and llm_config.get("api_key"):
        cps_threshold = llm_config.get("isometric_cps_threshold", 5.5)
        high_cps = _identify_high_cps_segments(final_results, cps_threshold)
        if high_cps:
            print(f"  🎯 等时翻译: {len(high_cps)}/{len(final_results)} 段估算 CPS > {cps_threshold}，生成多候选...")
            final_results = _isometric_translate_batch(final_results, high_cps, llm_config)
        else:
            print(f"  ✅ 等时翻译: 全部段 CPS 合规，无需多候选")

        # ── 等时扩展：低 CPS 段多候选扩展（Pass 2 之后）──
        expand_threshold = llm_config.get("isometric_expand_cps_threshold", 3.5)
        low_cps = _identify_low_cps_segments(final_results, expand_threshold)
        if low_cps:
            print(f"  📐 等时扩展: {len(low_cps)}/{len(final_results)} 段估算 CPS < {expand_threshold}，生成多候选...")
            final_results = _isometric_expand_batch(final_results, low_cps, llm_config)

    return final_results


def _translate_llm_single(batch, endpoint, headers, model, system_prompt, temperature,
                          max_retries: int = 3):
    """逐条 LLM 翻译（降级方案），带重试"""
    import httpx
    CHARS_PER_SEC = 4.5
    results = []
    for seg in batch:
        zh = None
        # 计算目标字数，作为前缀指令（不混入源文本）
        dur_sec = seg.get("end", 0) - seg.get("start", 0)
        target_chars = max(2, int(dur_sec * CHARS_PER_SEC))
        user_content = (
            f"（请将译文控制在约{target_chars}字，不要在译文中输出字数标注。"
            f"计算机缩写保留英文原词，不加括号注音。）\n{seg['text']}"
        )
        for attempt in range(max_retries):
            try:
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": temperature,
                    "max_tokens": 512,
                }
                with httpx.Client(timeout=30.0) as client:
                    resp = client.post(endpoint, json=payload, headers=headers)
                    resp.raise_for_status()
                    zh = resp.json()["choices"][0]["message"]["content"].strip()
                    zh = _strip_think_block(zh)
                    zh = _strip_markdown(zh, seg["text"])
                    if zh and len(zh.strip()) >= 2:
                        break
                    zh = None  # 空结果，重试
            except Exception:
                zh = None
            if attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))
        results.append(zh if zh else "")
    return results


def _strip_think_block(content: str) -> str:
    """去除 Qwen3 等模型返回的 <think>...</think> 推理块"""
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def _strip_markdown(text: str, original: str = "") -> str:
    """去除翻译文本中 LLM 额外添加的 Markdown 格式标记。

    只清除原文中不存在的 markdown 符号，保留原文本身就有的字符。
    例如原文 "3 * 4 = 12" 中的 * 是乘号，翻译后应保留。

    参数:
        text:     翻译后的中文文本
        original: 对应的英文原文（用于判断哪些符号是原文自带的）
    """
    if not text:
        return text
    # ── 兜底：清除 LLM 回显的字数提示和翻译指令泄漏 ──
    # 英文原文不可能包含中文字数提示，无需像 markdown 那样检查 original
    # 1. 括号包裹的字数提示（各种变体）:
    #    (≈26字) （约26个字） [≈26字] (目标约26字左右) (约20-30字) 等
    text = re.sub(
        r'[(\uff08\[]\s*(?:目标)?(?:约|≈)\s*\d+[\s\-~～]*(?:\d+)?\s*(?:个)?(?:中文)?字\s*(?:左右|以内)?\s*[)\uff09\]]',
        '', text)
    # 2. 行尾裸露的字数提示（无括号）: ...译文≈26字 / 约26字
    text = re.sub(r'\s*(?:约|≈)\s*\d+\s*(?:个)?字\s*$', '', text)
    # 3. 完整翻译指令句泄漏: （请将译文控制在约N字，不要在译文中输出字数标注）
    text = re.sub(r'[(\uff08]\s*请将译文控制在[^)\uff09]*[)\uff09]', '', text)
    # 4. 批量提示元数据泄漏: 各句参考字数：[1]≈26字, [2]≈8字 ...
    text = re.sub(r'各句参考字数[：:][^\n]*', '', text)
    # 5. 零散指令片段泄漏
    text = re.sub(r'[,，]?\s*不要在译文中输出字数标注[。.，,]?', '', text)
    # 反引号包裹的行内代码 `xxx` → xxx
    if '`' not in original:
        text = re.sub(r'`([^`]+)`', r'\1', text)
    # 加粗 **xxx** 或 __xxx__
    if '**' not in original:
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    if '__' not in original:
        text = re.sub(r'__(.+?)__', r'\1', text)
    # 斜体 *xxx*（但不匹配单独的 * 或乘号前后有空格的情况）
    if '*' not in original:
        text = re.sub(r'(?<!\*)\*([^\s*][^*]*[^\s*])\*(?!\*)', r'\1', text)
    # 斜体 _xxx_（仅匹配前后有空格或行首行尾的，避免破坏 snake_case 变量名）
    if '_' not in original:
        text = re.sub(r'(?<=\s)_([^_]+)_(?=\s|$)', r'\1', text)
        text = re.sub(r'^_([^_]+)_(?=\s|$)', r'\1', text)
    # 删除线 ~~xxx~~
    if '~' not in original:
        text = re.sub(r'~~(.+?)~~', r'\1', text)
    # 行首 # 标题标记
    if '#' not in original:
        text = re.sub(r'^#{1,6}\s+', '', text)
    return text.strip()


def _strip_numbered_prefix(line: str) -> str:
    """去除行首的 [N] 或 N. 编号前缀"""
    cleaned = re.sub(r"^\[?\d+\]?\s*\.?\s*", "", line.strip())
    return cleaned.strip()


def _parse_numbered_translations(content: str, expected_count: int) -> List[str]:
    """解析 LLM 返回的编号格式翻译 — 编号验证槽位放置

    核心改进: 按 [N] 编号放入对应槽位 (slot N-1)，而非按行序依次追加。
    防止 LLM 跳号/乱序时导致的跨段内容错位。
    """
    # 第一层：去除 <think> 推理块（qwen3-coder 等模型会输出）
    content = _strip_think_block(content)

    lines = content.strip().split("\n")

    # ── 预处理: 拆分同一行内的多个 [N] 标记 ──
    # LLM 有时将 "[1] 文本[2] 文本[3] 文本" 输出在同一行
    expanded_lines = []
    for line in lines:
        parts = re.split(r'(?=\[\d+\])', line)
        for part in parts:
            part = part.strip()
            if part:
                expanded_lines.append(part)
    lines = expanded_lines

    # ── 编号验证解析: 按编号放入对应槽位 ──
    slots = [""] * expected_count  # 预分配槽位
    numbered_entries = []  # [(number, text), ...]
    last_num = None  # 用于续行追加

    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 匹配 [N] 翻译内容 或 N. 翻译内容
        match = re.match(r"^\[(\d+)\]\s*(.+)$", line)
        if not match:
            match = re.match(r"^(\d+)\.\s*(.+)$", line)
        if match:
            num = int(match.group(1))
            text = match.group(2).strip()
            numbered_entries.append((num, text))
            last_num = len(numbered_entries) - 1
        elif last_num is not None:
            # 续行：追加到上一个编号条目
            num, prev_text = numbered_entries[last_num]
            numbered_entries[last_num] = (num, prev_text + line)

    # 按编号放入槽位
    if numbered_entries:
        for num, text in numbered_entries:
            idx = num - 1  # [1] → slot 0
            if 0 <= idx < expected_count:
                slots[idx] = text

        # 检查：如果编号验证填充率足够（≥50%），使用槽位结果
        filled = sum(1 for s in slots if s.strip())
        if filled >= expected_count * 0.5:
            # 最终安全检查：去除残留 [N] 前缀
            slots = [_strip_numbered_prefix(t) if re.match(r"^\[\d+\]", t) else t
                     for t in slots]
            return slots

    # ── 降级: 编号解析失败，按行序分割（兼容无编号输出） ──
    raw_lines = [l.strip() for l in content.strip().split("\n") if l.strip()]
    translations = [_strip_numbered_prefix(l) for l in raw_lines]

    # 最终安全检查：确保没有 [N] 前缀泄漏
    translations = [_strip_numbered_prefix(t) if re.match(r"^\[\d+\]", t) else t
                    for t in translations]

    # 补足或截断
    while len(translations) < expected_count:
        translations.append("")
    return translations[:expected_count]


# ─── Step 5: SRT 字幕 ─────────────────────────────────────────────
def format_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_srt_files(segments: List[dict], output_dir: Path):
    """生成三份 SRT 字幕"""
    paths = {
        "en": output_dir / "subtitle_en.srt",
        "zh": output_dir / "subtitle_zh.srt",
        "bi": output_dir / "subtitle_bilingual.srt",
    }
    with open(paths["en"], "w", encoding="utf-8") as fen, \
         open(paths["zh"], "w", encoding="utf-8") as fzh, \
         open(paths["bi"], "w", encoding="utf-8") as fbi:
        for idx, seg in enumerate(segments, 1):
            ts = f"{format_srt_time(seg['start'])} --> {format_srt_time(seg['end'])}"
            text_zh = _strip_markdown(seg['text_zh'], seg.get('text_en', ''))
            text_zh = _clean_refine_artifacts(text_zh)  # 清理 [轻]/[中]/[短] 等标签
            fen.write(f"{idx}\n{ts}\n{seg['text_en']}\n\n")
            fzh.write(f"{idx}\n{ts}\n{text_zh}\n\n")
            fbi.write(f"{idx}\n{ts}\n{text_zh}\n{seg['text_en']}\n\n")
    print(f"  ✅ SRT 字幕已生成 (英文/中文/双语)")
    return paths["en"], paths["zh"], paths["bi"]


# ─── Step 6: 中文配音 (可插拔 TTS 引擎) ─────────────────────────

# ── TTS 引擎抽象层 ──

class TTSFatalError(Exception):
    """不可恢复的 TTS 错误（认证失败/余额不足等），应立即切换引擎而非重试"""
    pass


class TTSEngine:
    """TTS 引擎基类"""
    name = "base"
    is_local = False  # 子类覆盖：本地引擎=True，远程引擎=False

    def resolve_voice(self, global_voice: str) -> str:
        """返回本引擎实际使用的语音标识。
        子类可覆盖；默认直接返回 global_voice（仅对 edge-tts 有意义）。"""
        return global_voice

    async def synthesize(self, text: str, path: str, voice: str, rate: float = 1.0):
        """将 text 合成为音频文件，保存到 path。rate 参数控制语速（1.0=默认）"""
        raise NotImplementedError

    async def synthesize_batch(self, items: List[dict], tts_dir: Path,
                               voice: str, concurrency: int = 5):
        """批量合成。items 是 [{"idx": N, "text_zh": "...", "rate": 1.0}, ...]
        rate 为可选参数，不指定时默认 1.0。
        遇到 TTSFatalError（认证/余额等不可恢复错误）时立即中止并向上传播。
        """
        resolved_voice = self.resolve_voice(voice)
        semaphore = asyncio.Semaphore(concurrency)
        fatal_error = None  # 记录首个致命错误

        async def _one(text, path, rate):
            nonlocal fatal_error
            if fatal_error:
                return  # 已有致命错误，跳过后续
            async with semaphore:
                try:
                    await self.synthesize(text, path, resolved_voice, rate)
                except TTSFatalError as e:
                    fatal_error = e
                    raise

        tasks = []
        for item in items:
            idx, text_zh = item["idx"], item["text_zh"]
            rate = item.get("rate", 1.0)
            p = tts_dir / f"seg_{idx:04d}.mp3"
            tasks.append(_one(text_zh, str(p), rate))

        if tasks:
            batch_n = max(1, concurrency * 2)
            for i in range(0, len(tasks), batch_n):
                await asyncio.gather(*tasks[i:i+batch_n], return_exceptions=True)
                if fatal_error:
                    raise fatal_error
                done = min(i + batch_n, len(tasks))
                print(f"     TTS 进度: {done}/{len(tasks)}")


class EdgeTTSEngine(TTSEngine):
    """edge-tts: 微软免费在线 TTS（默认引擎）"""
    name = "edge-tts"
    is_local = False

    async def synthesize(self, text: str, path: str, voice: str, rate: float = 1.0):
        """合成音频，支持 rate 参数调整语速（0.85~1.20 为安全区间）

        rate=1.0: 默认语速
        rate=1.15: 加速 15%
        rate=0.9: 减速 10%
        """
        import edge_tts
        # 使用 edge-tts 原生 rate 参数控制语速（比 SSML prosody 包裹更可靠，
        # 因为 Communicate 内部会转义 XML 特殊字符，导致 <prosody> 标签被朗读出来）
        rate_pct = int((rate - 1.0) * 100)
        rate_str = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"
        communicate = edge_tts.Communicate(text, voice, rate=rate_str)
        await asyncio.wait_for(communicate.save(path), timeout=30)


class GTTSEngine(TTSEngine):
    """gTTS: Google Translate TTS（免费，无需 API key）"""
    name = "gtts"
    is_local = False

    def resolve_voice(self, global_voice: str) -> str:
        """gTTS 使用语言代码 zh-cn，忽略全局 edge-tts voice"""
        return "zh-cn"

    async def synthesize(self, text: str, path: str, voice: str, rate: float = 1.0):
        from gtts import gTTS
        loop = asyncio.get_event_loop()
        def _gen():
            tts = gTTS(text=text, lang="zh-cn")
            tts.save(path)
        await loop.run_in_executor(None, _gen)


class PiperTTSEngine(TTSEngine):
    """Piper: 本地 ONNX 推理，无需 GPU，超轻量"""
    name = "piper"
    is_local = True

    def __init__(self, model_path: str = None):
        self.model_path = model_path

    def resolve_voice(self, global_voice: str) -> str:
        """Piper 使用本地模型路径，忽略全局 edge-tts voice"""
        return self.model_path or "zh_CN-huayan-medium"

    async def synthesize(self, text: str, path: str, voice: str, rate: float = 1.0):
        loop = asyncio.get_event_loop()
        model = voice  # 已由 resolve_voice 转为模型路径
        def _gen():
            import subprocess, shutil
            # 优先从当前 Python 所在的 venv/bin 找 piper，其次 PATH
            piper_bin = os.path.join(os.path.dirname(sys.executable), "piper")
            if not os.path.isfile(piper_bin):
                piper_bin = shutil.which("piper") or "piper"
            proc = subprocess.run(
                [piper_bin, "--model", model, "--output_file", path],
                input=text, capture_output=True, text=True, timeout=30,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"piper failed: {proc.stderr[:200]}")
        await loop.run_in_executor(None, _gen)


class SherpaOnnxEngine(TTSEngine):
    """sherpa-onnx: 本地 ONNX 推理（含 MeloTTS 中文模型），无需 GPU"""
    name = "sherpa-onnx"
    is_local = True

    def __init__(self, model_config: dict = None):
        self.model_config = model_config or {}

    def resolve_voice(self, global_voice: str) -> str:
        """sherpa-onnx 使用 speaker_id，忽略全局 edge-tts voice"""
        return str(self.model_config.get("speaker_id", 0))

    async def synthesize(self, text: str, path: str, voice: str, rate: float = 1.0):
        loop = asyncio.get_event_loop()
        cfg = self.model_config
        def _gen():
            import sherpa_onnx
            tts_config = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                        model=cfg.get("model", ""),
                        lexicon=cfg.get("lexicon", ""),
                        tokens=cfg.get("tokens", ""),
                        dict_dir=cfg.get("dict_dir", ""),
                    ),
                ),
                max_num_sentences=1,
            )
            tts = sherpa_onnx.OfflineTts(tts_config)
            audio = tts.generate(text, sid=int(cfg.get("speaker_id", 0)), speed=1.0)
            import wave
            wav_path = path.replace(".mp3", ".wav")
            with wave.open(wav_path, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(audio.sample_rate)
                import struct
                samples = struct.pack(f"<{len(audio.samples)}h",
                    *[int(max(-32768, min(32767, s * 32767))) for s in audio.samples])
                wf.writeframes(samples)
            # 转 mp3
            from pydub import AudioSegment as PydubSegment
            PydubSegment.from_wav(wav_path).export(path, format="mp3")
            os.remove(wav_path)
        await loop.run_in_executor(None, _gen)


class Pyttsx3Engine(TTSEngine):
    """pyttsx3: 系统自带 TTS（macOS=NSSpeech, Windows=SAPI5），完全离线零依赖
    macOS 中文语音需在 系统设置 → 辅助功能 → 朗读内容 → 管理声音 中下载。
    默认使用 Ting-Ting（普通话女声），如未安装则回退到系统默认语音。
    """
    name = "pyttsx3"
    is_local = True

    def __init__(self, voice_name: str = None, rate: int = None):
        self.voice_name = voice_name  # 如 "Ting-Ting", "Mei-Jia"
        self.rate = rate or 180  # 默认语速

    def resolve_voice(self, global_voice: str) -> str:
        """pyttsx3 使用系统语音名，忽略全局 edge-tts voice"""
        return self.voice_name or "auto"

    async def synthesize(self, text: str, path: str, voice: str, rate: float = 1.0):
        loop = asyncio.get_event_loop()
        vname = self.voice_name
        rate = self.rate

        def _gen():
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty('rate', rate)

            # 尝试找中文语音
            if vname:
                voices = engine.getProperty('voices')
                for v in voices:
                    if vname.lower() in v.name.lower():
                        engine.setProperty('voice', v.id)
                        break
            else:
                # 自动查找中文语音
                voices = engine.getProperty('voices')
                for v in voices:
                    if any(k in v.name.lower() for k in ['ting-ting', 'mei-jia', 'chinese', 'zh']):
                        engine.setProperty('voice', v.id)
                        break

            # pyttsx3 只能保存为 aiff/wav，需要转 mp3
            wav_path = path.replace(".mp3", ".wav")
            engine.save_to_file(text, wav_path)
            engine.runAndWait()
            engine.stop()

            if os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
                from pydub import AudioSegment as PydubSegment
                PydubSegment.from_file(wav_path).export(path, format="mp3")
                os.remove(wav_path)
            else:
                raise RuntimeError(f"pyttsx3 生成失败: {wav_path} 为空或不存在")

        await loop.run_in_executor(None, _gen)


# ── TTS 引擎注册表 ──

TTS_ENGINES = {
    "edge-tts": EdgeTTSEngine,
    "gtts": GTTSEngine,
    "piper": PiperTTSEngine,
    "sherpa-onnx": SherpaOnnxEngine,
    "pyttsx3": Pyttsx3Engine,
}


def _create_tts_engine(config: dict) -> TTSEngine:
    """根据配置创建 TTS 引擎实例"""
    engine_name = config.get("tts_engine", "edge-tts")
    engine_config = config.get(engine_name.replace("-", "_"), {})

    if engine_name not in TTS_ENGINES:
        print(f"  ⚠️  未知 TTS 引擎 '{engine_name}'，回退到 edge-tts")
        engine_name = "edge-tts"

    engine_cls = TTS_ENGINES[engine_name]
    if engine_name == "piper":
        return engine_cls(model_path=engine_config.get("model_path"))
    elif engine_name == "sherpa-onnx":
        return engine_cls(model_config=engine_config)
    elif engine_name == "pyttsx3":
        return engine_cls(
            voice_name=engine_config.get("voice_name"),
            rate=engine_config.get("rate"),
        )
    else:
        return engine_cls()


# ── TTS 生成主函数 ──

async def _generate_tts_segments(
    segments: List[dict], tts_dir: Path,
    voice: str = "zh-CN-YunxiNeural", concurrency: int = 5,
    config: dict = None,
):
    """阶段 A: 生成 TTS .mp3 文件（整体回退 + 智能重试 + 断点恢复）

    策略:
      远程引擎: 正常并发 → 降并发 → 并发=1 后持续重试，连续 3 轮无改善才放弃
      本地引擎: 不限重试轮数（本地不存在网络抖动问题，失败即真失败，1 轮即可）
      切换引擎前: 备份当前产出到 tts_backup_{engine}/ + 写 tts_failure.json
      支持从 tts_failure.json 断点恢复（跳过已失败的引擎，从下一个开始）
    """
    tts_dir.mkdir(exist_ok=True)
    config = config or {}
    failure_json = _audit_dir(tts_dir.parent) / "tts_failure.json"

    # ── 解析引擎链 ──
    tts_chain = config.get("tts_chain", None)
    if tts_chain:
        if isinstance(tts_chain, str):
            tts_chain = [tts_chain]
        chain_names = list(tts_chain)
    else:
        primary = config.get("tts_engine", "edge-tts")
        fallback_names = config.get("tts_fallback", [])
        if isinstance(fallback_names, str):
            fallback_names = [fallback_names]
        chain_names = [primary] + fallback_names

    chain_names = [n for n in chain_names if n in TTS_ENGINES]
    if not chain_names:
        chain_names = ["edge-tts"]

    # 收集所有需要合成的片段（必须在断点恢复前构建，resume 逻辑依赖 all_items）
    # TTS rate 优化：根据目标时长和中文字数计算语速调节参数
    CHARS_PER_SEC = 4.5  # edge-tts 中文默认语速：约 4.5 字/秒
    MS_PER_CHAR = 1000 / CHARS_PER_SEC  # ≈222ms/字
    all_items = []
    skipped_placeholder = 0
    for idx, seg in enumerate(segments):
        text_zh = seg.get("text_zh", seg.get("text", ""))
        # 最后防线：清除可能残留的格式标记
        text_zh = _strip_markdown(text_zh, seg.get("text_en", seg.get("text", "")))
        text_zh = _clean_refine_artifacts(text_zh)  # 清理 [轻]/[中]/[短] 等标签
        text_zh = _fix_polyphones(text_zh)  # 多音字同音替换，纠正 TTS 误读
        if len(text_zh.strip()) >= 2:
            # 防御：跳过纯标点/占位符文本（如 "---"、"..."），TTS 引擎无法合成
            if not re.search(r'[\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]', text_zh):
                skipped_placeholder += 1
                continue
            # 计算 TTS rate 参数：根据目标时长调节语速
            rate = 1.0
            raw_ratio = 1.0
            start_ms = int(seg.get("start", 0) * 1000)
            end_ms = int(seg.get("end", 0) * 1000)
            target_dur_ms = end_ms - start_ms
            if target_dur_ms > 0:
                # 使用 jieba 分词估算（含 URL 检测），比纯字符计数更准确
                estimated_tts_ms = _estimate_duration_jieba(text_zh) * 1.3  # 韵律/停顿修正
                if estimated_tts_ms > 0:
                    raw_ratio = estimated_tts_ms / target_dur_ms
                    rate = raw_ratio
            # 应用全局语速倍率，TTS 原生 rate 区间由 alignment 配置控制
            global_speed = (config or {}).get("global_speed", 1.0)
            align_cfg = (config or {}).get("alignment", {})
            rate_range = align_cfg.get("tts_rate_range", [0.80, 1.35])
            rate = max(rate_range[0], min(rate_range[1], rate * global_speed))
            all_items.append({
                "idx": idx, "text_zh": text_zh, "rate": rate,
                "raw_ratio": raw_ratio, "target_dur_ms": target_dur_ms
            })
    if skipped_placeholder:
        print(f"     ⚠️  跳过 {skipped_placeholder} 个无可发音内容的片段"
              f"（纯标点/占位符如 '---'）")

    if not all_items:
        print(f"     无需生成 TTS 片段")
        return

    # ── Phase 3: 预检自适应 —— ratio 超标时先调整译文 ──
    # 只处理 ratio 超出 (0.75, 1.30) 的片段，这些片段即使用 TTS rate 也无法完全补偿
    outliers = [item for item in all_items
                if item["raw_ratio"] < 0.70 or item["raw_ratio"] > 1.35]
    if outliers and config.get("llm"):
        print(f"     🔄 预检：{len(outliers)} 个片段 ratio 超标，调整译文中...")
        llm_config = config["llm"]
        api_url = llm_config["api_url"].rstrip("/")
        api_key = llm_config.get("api_key", "")
        model = llm_config.get("model", "")
        if api_key and model:
            import httpx
            endpoint = f"{api_url}/chat/completions" if "/chat/completions" not in api_url else api_url
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            adjusted_count = 0
            # URL 模式：含域名的文本 TTS 会逐字母朗读，LLM 无法有效精简
            _url_pat = re.compile(
                r'(?:https?://)?(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}'
            )
            for item in outliers:
                idx = item["idx"]
                seg = segments[idx]
                old_zh = item["text_zh"]
                # 含 URL 且超速的片段：LLM 无法有效缩短 URL 发音，跳过
                if item["raw_ratio"] > 1.35 and _url_pat.search(old_zh):
                    continue
                target_chars = max(2, int(item["target_dur_ms"] / MS_PER_CHAR))
                current_chars = len(re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf]', old_zh))
                # 判断调整方向
                if item["raw_ratio"] > 1.35:
                    action = "精简"
                    prompt = f"请将以下中文译文精简到约 {target_chars} 字，保持核心语义：\n{old_zh}"
                else:
                    action = "扩展"
                    prompt = f"请将以下中文译文扩展到约 {target_chars} 字，使表达更完整自然：\n{old_zh}"
                try:
                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": "你是配音字幕调整助手。根据要求精简或扩展译文，输出纯文本，不要添加任何解释。"},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.3,
                        "max_tokens": 256,
                    }
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.post(endpoint, json=payload, headers=headers)
                        resp.raise_for_status()
                        new_zh = resp.json()["choices"][0]["message"]["content"].strip()
                        new_zh = _strip_markdown(new_zh, seg.get("text_en", seg.get("text", "")))
                        if new_zh and len(new_zh) >= 2:
                            # 更新 segments 和 item
                            segments[idx]["text_zh"] = new_zh
                            item["text_zh"] = new_zh
                            # 重新计算 rate（使用 jieba 估算，含 URL 检测）
                            new_est_ms = _estimate_duration_jieba(new_zh)
                            if new_est_ms > 0 and item["target_dur_ms"] > 0:
                                new_ratio = new_est_ms / item["target_dur_ms"]
                                item["raw_ratio"] = new_ratio
                                item["rate"] = max(0.85, min(1.20, new_ratio))
                            adjusted_count += 1
                except Exception as e:
                    print(f"        ⚠️ 段落 {idx} {action}失败: {e}")
            if adjusted_count:
                print(f"        ✅ 成功调整 {adjusted_count}/{len(outliers)} 个片段")

    # ── 断点恢复：先用上次失败引擎重试失败片段 ──
    start_idx = 0
    resume_retry_only = False  # True=仅重试失败片段，不全量生成
    resume_items = None
    if failure_json.exists():
        try:
            with open(failure_json) as f:
                prev = json.load(f)
            failed_engine = prev.get("engine")
            failed_segs = set(prev.get("failed_segments", []))
            if failed_engine in chain_names and failed_segs:
                start_idx = chain_names.index(failed_engine)
                resume_retry_only = True
                # 只重试上次记录的失败片段
                resume_items = [item for item in all_items
                                if item["idx"] in failed_segs]
                print(f"     📋 断点恢复: 读取 {failure_json}")
                print(f"        上次 [{failed_engine}] 有"
                      f" {len(resume_items)} 个片段失败，先重试这些片段...")
        except Exception:
            pass

    chain_desc = " → ".join(chain_names[start_idx:])
    print(f"     TTS 引擎链: {chain_desc}")

    # 检查是否已有完整缓存
    cached_count = sum(1 for item in all_items
                       if (tts_dir / f"seg_{item['idx']:04d}.mp3").exists()
                       and (tts_dir / f"seg_{item['idx']:04d}.mp3").stat().st_size > 0)
    if cached_count == len(all_items):
        print(f"     TTS 片段已缓存（{cached_count} 个），跳过生成")
        if failure_json.exists():
            failure_json.unlink()
        return

    # ── 辅助：统计失败片段 ──
    def _count_failed():
        failed = []
        for item in all_items:
            p = tts_dir / f"seg_{item['idx']:04d}.mp3"
            if not p.exists() or p.stat().st_size == 0:
                failed.append(item)
        return failed

    # ── 整体回退：逐引擎尝试 ──
    success_engine = None

    for eng_pos in range(start_idx, len(chain_names)):
        eng_name = chain_names[eng_pos]
        engine = _create_tts_engine({**config, "tts_engine": eng_name})
        resolved_voice = engine.resolve_voice(voice)
        fatal_hit = False  # 标记本引擎是否遇到不可恢复错误

        # ── 断点恢复首轮：仅重试上次失败的片段 ──
        if resume_retry_only and eng_pos == start_idx and resume_items:
            print(f"     [{engine.name}] 断点重试 {len(resume_items)} 个失败片段"
                  f" (voice={resolved_voice})...")
            # 清掉失败片段的残留（0字节或静音占位）
            for item in resume_items:
                p = tts_dir / f"seg_{item['idx']:04d}.mp3"
                if p.exists():
                    p.unlink()
            try:
                await engine.synthesize_batch(resume_items, tts_dir, voice, concurrency)

                # 对失败片段做智能重试
                def _count_resume_failed():
                    return [item for item in resume_items
                            if not (tts_dir / f"seg_{item['idx']:04d}.mp3").exists()
                            or (tts_dir / f"seg_{item['idx']:04d}.mp3").stat().st_size == 0]
                await _smart_retry_engine(engine, _count_resume_failed, tts_dir,
                                          voice, concurrency)
            except TTSFatalError as e:
                print(f"     ⚠️  [{engine.name}] 不可恢复错误: {e}")
                print(f"     [{engine.name}] 跳过（认证/余额等致命问题），尝试下一个引擎...")
                fatal_hit = True
                resume_retry_only = False
                continue

            # 检查断点重试结果（用 _count_failed 检查全量，而非仅 resume 片段）
            still_failed_resume = _count_resume_failed()
            overall_failed = _count_failed()
            if not overall_failed:
                success_engine = engine.name
                print(f"     ✅ [{engine.name}] 断点重试成功，"
                      f"全部 {len(all_items)} 个片段完整")
                if failure_json.exists():
                    failure_json.unlink()
                break
            elif not still_failed_resume and overall_failed:
                # resume 片段全成功，但有其他片段缺失 → 走正常全量补齐
                print(f"     [{engine.name}] 断点重试片段已恢复，"
                      f"但仍有 {len(overall_failed)} 个其他片段缺失，继续补齐...")
                resume_retry_only = False
                # 不 continue，直接往下走正常全量流程
            else:
                print(f"     [{engine.name}] 断点重试后仍有"
                      f" {len(still_failed_resume)} 个片段失败，"
                      f"继续下一个引擎...")
                resume_retry_only = False  # 后续走正常全量流程
                # 不 continue —— 下面的 eng_pos > start_idx 判断会处理整体回退
                eng_pos_next = eng_pos + 1
                if eng_pos_next >= len(chain_names):
                    # 写失败 JSON 并退出循环
                    _write_failure_json(failure_json, engine, all_items,
                                        still_failed_resume, chain_names,
                                        eng_pos, resolved_voice)
                    print(f"     ❌ [{engine.name}] 仍有"
                          f" {len(still_failed_resume)} 个片段失败"
                          f"，引擎链已用尽")
                    print(f"     📄 失败记录: {failure_json}")
                continue

        if eng_pos > start_idx or (resume_retry_only is False and eng_pos == start_idx and eng_pos > 0):
            # 整体回退：备份 → 清空
            prev_eng = chain_names[eng_pos - 1] if eng_pos > 0 else "unknown"
            backup_dir = tts_dir.parent / f"tts_backup_{prev_eng}"
            _backup_tts(tts_dir, backup_dir, all_items)
            print(f"     🔄 整体回退到 [{engine.name}]"
                  f"（上轮备份在 {backup_dir.name}/）")
            for item in all_items:
                p = tts_dir / f"seg_{item['idx']:04d}.mp3"
                if p.exists():
                    p.unlink()

        print(f"     [{engine.name}] 生成 {len(all_items)} 个 TTS 片段"
              f" (voice={resolved_voice},"
              f" {'本地' if engine.is_local else '远程'})...")

        # 首轮：正常并发
        try:
            pending = _count_failed()
            if pending:
                await engine.synthesize_batch(pending, tts_dir, voice, concurrency)

            # ── 智能重试 ──
            await _smart_retry_engine(engine, _count_failed, tts_dir, voice,
                                      concurrency)
        except TTSFatalError as e:
            print(f"     ⚠️  [{engine.name}] 不可恢复错误: {e}")
            print(f"     [{engine.name}] 跳过（认证/余额等致命问题），尝试下一个引擎...")
            _write_failure_json(failure_json, engine, all_items,
                                _count_failed(), chain_names, eng_pos,
                                resolved_voice)
            print(f"     📄 失败记录: {failure_json}")
            continue

        # 检查最终结果
        final_failed = _count_failed()

        if not final_failed:
            success_engine = engine.name
            print(f"     ✅ [{engine.name}] 全部 {len(all_items)} 个片段生成成功")
            if failure_json.exists():
                failure_json.unlink()
            break
        else:
            _write_failure_json(failure_json, engine, all_items,
                                final_failed, chain_names, eng_pos,
                                resolved_voice)
            is_last = eng_pos >= len(chain_names) - 1
            print(f"     ❌ [{engine.name}] 仍有 {len(final_failed)} 个片段失败"
                  + (f"，尝试下一个引擎..." if not is_last else "，引擎链已用尽"))
            print(f"     📄 失败记录: {failure_json}")

    # ── 时长反馈闭环：测量实际 TTS 时长，偏差大的段用精确 rate 重生成 ──
    if success_engine:
        feedback_tol = (config or {}).get("alignment", {}).get("feedback_tolerance", 0.15)
        await _tts_with_duration_feedback(
            all_items, segments, tts_dir, engine, resolved_voice,
            config=config, concurrency=concurrency, tolerance=feedback_tol,
        )
        # ── LLM 闭环：rate 仍无法补偿的段，用实测时长精确调整译文 ──
        await _llm_duration_feedback(
            all_items, segments, tts_dir, engine, resolved_voice,
            config=config, concurrency=concurrency, deviation_threshold=0.20,
        )

    # 最终兜底：所有引擎都失败的片段填充静音
    if not success_engine:
        silence_count = 0
        for item in _count_failed():
            idx = item["idx"]
            p = tts_dir / f"seg_{idx:04d}.mp3"
            seg = segments[idx]
            target_ms = int((seg.get("end", 0) - seg.get("start", 0)) * 1000)
            if target_ms > 0:
                from pydub import AudioSegment as PydubSegment
                silence = PydubSegment.silent(
                    duration=target_ms, frame_rate=16000)
                silence.export(str(p), format="mp3")
                silence_count += 1
                print(f"     ⚠️  seg_{idx:04d}.mp3 所有引擎均失败，填充静音")
        if silence_count:
            print(f"     共 {silence_count} 个片段填充静音（所有引擎均已尝试）")
            print(f"     📄 下次可断点重试: {failure_json}")
            print(f"        修改 tts_chain 或删除静音片段后重新运行即可恢复")


def _backup_tts(tts_dir: Path, backup_dir: Path, all_items: List[dict]):
    """备份当前 TTS 产出到 backup_dir/"""
    import shutil
    backup_dir.mkdir(exist_ok=True)
    count = 0
    for item in all_items:
        src = tts_dir / f"seg_{item['idx']:04d}.mp3"
        if src.exists() and src.stat().st_size > 0:
            shutil.copy2(str(src), str(backup_dir / src.name))
            count += 1
    if count:
        print(f"     💾 已备份 {count} 个 TTS 片段到 {backup_dir.name}/")


async def _tts_with_duration_feedback(
    items: List[dict], segments: List[dict], tts_dir: Path,
    engine, voice: str, config: dict = None,
    concurrency: int = 5, tolerance: float = 0.15,
):
    """对偏差较大的段进行 TTS 时长反馈闭环。

    测量每段 TTS 实际时长，偏离目标 > tolerance 的段用精确 rate 重新生成。
    仅重试 1 次，避免 API 过载。
    """
    from pydub import AudioSegment as PydubSegment

    align_cfg = (config or {}).get("alignment", {})
    if not align_cfg.get("feedback_loop", True):
        return
    rate_range = align_cfg.get("tts_rate_range", [0.80, 1.35])

    # 测量实际时长 vs 目标时长
    retry_items = []
    for item in items:
        idx = item["idx"]
        tts_path = tts_dir / f"seg_{idx:04d}.mp3"
        if not tts_path.exists() or tts_path.stat().st_size == 0:
            continue
        target_dur_ms = item.get("target_dur_ms", 0)
        if target_dur_ms <= 0:
            continue
        try:
            tts_audio = PydubSegment.from_mp3(str(tts_path))
            actual_ms = len(tts_audio)
        except Exception:
            continue
        if actual_ms <= 0:
            continue
        deviation = abs(actual_ms - target_dur_ms) / target_dur_ms
        if deviation > tolerance:
            # 计算精确 rate: actual/target = 当前倍率，需要 rate 来补偿
            corrected_rate = actual_ms / target_dur_ms
            corrected_rate = max(rate_range[0], min(rate_range[1], corrected_rate))
            retry_items.append({
                "idx": idx,
                "text_zh": item["text_zh"],
                "rate": corrected_rate,
                "raw_ratio": item.get("raw_ratio", 1.0),
                "target_dur_ms": target_dur_ms,
                "_feedback_actual_ms": actual_ms,
                "_feedback_deviation": round(deviation, 3),
            })

    if not retry_items:
        return

    print(f"     🔄 时长反馈闭环: {len(retry_items)} 段偏差 >{tolerance*100:.0f}%，精确 rate 重生成...")

    # 删除旧文件，重新生成
    for item in retry_items:
        old_path = tts_dir / f"seg_{item['idx']:04d}.mp3"
        if old_path.exists():
            old_path.unlink()

    try:
        await engine.synthesize_batch(retry_items, tts_dir, voice, concurrency)
    except Exception as e:
        print(f"     ⚠️  闭环重生成部分失败: {e}")

    # 统计改善效果
    improved = 0
    for item in retry_items:
        tts_path = tts_dir / f"seg_{item['idx']:04d}.mp3"
        if not tts_path.exists() or tts_path.stat().st_size == 0:
            continue
        try:
            new_audio = PydubSegment.from_mp3(str(tts_path))
            new_ms = len(new_audio)
            new_deviation = abs(new_ms - item["target_dur_ms"]) / item["target_dur_ms"]
            if new_deviation < item["_feedback_deviation"]:
                improved += 1
        except Exception:
            pass

    print(f"     ✅ 闭环完成: {improved}/{len(retry_items)} 段时长改善")

    # 保存反馈审计日志
    try:
        audit_dir = tts_dir.parent / "audit"
        audit_dir.mkdir(exist_ok=True)
        feedback_log = [{
            "idx": item["idx"],
            "target_ms": item["target_dur_ms"],
            "before_ms": item["_feedback_actual_ms"],
            "deviation": item["_feedback_deviation"],
            "corrected_rate": round(item["rate"], 3),
        } for item in retry_items]
        with open(audit_dir / "tts_feedback_log.json", "w", encoding="utf-8") as f:
            json.dump(feedback_log, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


async def _llm_duration_feedback(
    items: List[dict], segments: List[dict], tts_dir: Path,
    engine, voice: str, config: dict = None,
    concurrency: int = 5, deviation_threshold: float = 0.20,
):
    """闭环 LLM 反馈：用 TTS 实测时长驱动译文调整。

    在 rate 反馈闭环之后，对仍偏差 > deviation_threshold 的段：
    1. 测量实际 TTS 时长（精确值，非估算）
    2. 计算目标字数 = 当前字数 × (target_ms / actual_ms)
    3. 让 LLM 调整译文到目标字数
    4. 用调整后的译文重新生成 TTS

    比 Phase 3 预检（基于 jieba 估算）精确得多。
    """
    from pydub import AudioSegment as PydubSegment

    align_cfg = (config or {}).get("alignment", {})
    if not align_cfg.get("feedback_loop", True):
        return

    llm_config = (config or {}).get("llm")
    if not llm_config:
        return
    api_url = llm_config.get("api_url", "").rstrip("/")
    api_key = llm_config.get("api_key", "")
    model = llm_config.get("model", "")
    if not api_key or not model:
        return

    rate_range = align_cfg.get("tts_rate_range", [0.80, 1.35])

    # URL 模式：含域名的文本不适合 LLM 精简
    _url_pat = re.compile(
        r'(?:https?://)?(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}'
    )

    # 重新测量所有段的实际 TTS 时长，找出仍偏差大的
    outliers = []
    for item in items:
        idx = item["idx"]
        tts_path = tts_dir / f"seg_{idx:04d}.mp3"
        if not tts_path.exists() or tts_path.stat().st_size == 0:
            continue
        target_dur_ms = item.get("target_dur_ms", 0)
        if target_dur_ms <= 0:
            continue
        try:
            tts_audio = PydubSegment.from_mp3(str(tts_path))
            actual_ms = len(tts_audio)
        except Exception:
            continue
        if actual_ms <= 0:
            continue
        deviation = abs(actual_ms - target_dur_ms) / target_dur_ms
        if deviation > deviation_threshold:
            text_zh = item["text_zh"]
            # 跳过含 URL 的超速段
            if actual_ms > target_dur_ms and _url_pat.search(text_zh):
                continue
            zh_chars = len(re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf]', text_zh))
            if zh_chars < 2:
                continue
            # 根据实测时长精确计算目标字数
            target_chars = max(2, int(zh_chars * target_dur_ms / actual_ms))
            outliers.append({
                "idx": idx,
                "text_zh": text_zh,
                "actual_ms": actual_ms,
                "target_dur_ms": target_dur_ms,
                "deviation": round(deviation, 3),
                "current_chars": zh_chars,
                "target_chars": target_chars,
                "action": "精简" if actual_ms > target_dur_ms else "扩展",
            })

    if not outliers:
        return

    print(f"     🔄 LLM 时长闭环: {len(outliers)} 段偏差 >{deviation_threshold*100:.0f}%，"
          f"用实测时长调整译文...")

    import httpx
    endpoint = f"{api_url}/chat/completions" if "/chat/completions" not in api_url else api_url
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    adjusted_count = 0
    regen_items = []
    for out in outliers:
        idx = out["idx"]
        seg = segments[idx]
        old_zh = out["text_zh"]
        action = out["action"]
        actual = out["actual_ms"]
        target = out["target_dur_ms"]
        target_chars = out["target_chars"]

        prompt = (
            f"当前中文译文合成语音时长为 {actual}ms，目标时长为 {target}ms。\n"
            f"请将以下译文{action}到约 {target_chars} 字，保持核心语义准确：\n{old_zh}"
        )
        try:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": (
                        "你是配音字幕调整助手。根据实际语音时长反馈调整译文长度，"
                        "输出纯文本，不要添加任何解释、引号或标点符号以外的内容。"
                    )},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 256,
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                new_zh = resp.json()["choices"][0]["message"]["content"].strip()
                # 多层清洗 LLM 输出 (README 踩坑 #12)
                new_zh = _strip_think_block(new_zh)
                new_zh = _strip_markdown(new_zh, seg.get("text_en", seg.get("text", "")))
                if new_zh and len(new_zh) >= 2 and new_zh != old_zh:
                    # 忠实度校验: 防止 LLM 编造内容 (devlog/2026-03-29-expand-llm-garbage.md)
                    if not _check_refine_fidelity(old_zh, new_zh, min_overlap=0.25):
                        print(f"        ⚠️ 段落 {idx} 忠实度不足，跳过")
                        continue
                    # 邻段去重: 防止 LLM 偷懒复制相邻段
                    if _is_duplicate_of_neighbors(new_zh, idx, segments):
                        print(f"        ⚠️ 段落 {idx} 与相邻段重复，跳过")
                        continue
                    # 更新 segments 和对应 item
                    segments[idx]["text_zh"] = new_zh
                    for item in items:
                        if item["idx"] == idx:
                            item["text_zh"] = new_zh
                            break
                    adjusted_count += 1
                    # 准备重新生成 TTS
                    new_est_ms = _estimate_duration_jieba(new_zh)
                    rate = new_est_ms / target if target > 0 and new_est_ms > 0 else 1.0
                    rate = max(rate_range[0], min(rate_range[1], rate))
                    regen_items.append({
                        "idx": idx,
                        "text_zh": new_zh,
                        "rate": rate,
                        "target_dur_ms": target,
                    })
        except Exception as e:
            print(f"        ⚠️ 段落 {idx} LLM {action}失败: {e}")

    if not regen_items:
        return

    # 删除旧 TTS 并重新生成
    for item in regen_items:
        old_path = tts_dir / f"seg_{item['idx']:04d}.mp3"
        if old_path.exists():
            old_path.unlink()
    try:
        await engine.synthesize_batch(regen_items, tts_dir, voice, concurrency)
    except Exception as e:
        print(f"     ⚠️ LLM 闭环重生成部分失败: {e}")

    print(f"     ✅ LLM 闭环完成: {adjusted_count} 段译文调整并重新合成")

    # 保存审计日志
    try:
        audit_dir = tts_dir.parent / "audit"
        audit_dir.mkdir(exist_ok=True)
        llm_log = [{
            "idx": out["idx"],
            "action": out["action"],
            "target_ms": out["target_dur_ms"],
            "actual_ms": out["actual_ms"],
            "deviation": out["deviation"],
            "current_chars": out["current_chars"],
            "target_chars": out["target_chars"],
        } for out in outliers]
        with open(audit_dir / "llm_duration_feedback_log.json", "w", encoding="utf-8") as f:
            json.dump(llm_log, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


async def _smart_retry_engine(engine, count_failed_fn, tts_dir, voice,
                               concurrency):
    """单引擎内智能重试。
    远程引擎: 阶梯降并发(正常→半→1)，并发=1后持续重试，连续3轮无改善才放弃
    本地引擎: 失败即真失败，1轮重试即可
    TTSFatalError (认证/余额不足等): 立即放弃，不做无效重试
    """
    if engine.is_local:
        failed = count_failed_fn()
        if failed:
            print(f"     [{engine.name}] 本地引擎重试: {len(failed)} 个失败片段...")
            for item in failed:
                p = tts_dir / f"seg_{item['idx']:04d}.mp3"
                if p.exists():
                    p.unlink()
            try:
                await engine.synthesize_batch(failed, tts_dir, voice, 1)
            except TTSFatalError:
                raise
    else:
        no_improve_count = 0
        prev_fail_count = len(count_failed_fn())
        retry_round = 0

        while True:
            failed = count_failed_fn()
            if not failed:
                break
            cur_fail_count = len(failed)

            if retry_round == 0:
                retry_c = max(1, concurrency // 2)
            else:
                retry_c = 1

            if retry_c == 1 and retry_round > 0:
                if cur_fail_count >= prev_fail_count:
                    no_improve_count += 1
                else:
                    no_improve_count = 0
                if no_improve_count >= 3:
                    print(f"     [{engine.name}] 连续 3 轮无改善"
                          f" (仍有 {cur_fail_count} 个失败)，放弃当前引擎")
                    break

            prev_fail_count = cur_fail_count
            retry_round += 1
            print(f"     [{engine.name}] 重试第{retry_round}轮:"
                  f" {cur_fail_count} 个失败, 并发={retry_c}"
                  + (f", 无改善={no_improve_count}/3"
                     if retry_c == 1 and retry_round > 1 else "")
                  + "...")
            await asyncio.sleep(2 * min(retry_round, 5))
            for item in failed:
                p = tts_dir / f"seg_{item['idx']:04d}.mp3"
                if p.exists():
                    p.unlink()
            try:
                await engine.synthesize_batch(failed, tts_dir, voice, retry_c)
            except TTSFatalError:
                raise


def _write_failure_json(failure_json, engine, all_items, failed_items,
                        chain_names, eng_pos, resolved_voice):
    """写入 tts_failure.json 供断点恢复"""
    import datetime
    fail_info = {
        "engine": engine.name,
        "total_segments": len(all_items),
        "failed_count": len(failed_items),
        "failed_segments": [item["idx"] for item in failed_items],
        "chain": chain_names,
        "chain_position": eng_pos,
        "voice": resolved_voice,
        "timestamp": datetime.datetime.now().isoformat(),
    }
    with open(failure_json, "w", encoding="utf-8") as f:
        json.dump(fail_info, f, ensure_ascii=False, indent=2)


def _estimate_speed_ratios(
    segments: List[dict], threshold: float = 1.5,
    ms_per_zh_char: float = 250.0, ms_per_en_char: float = 100.0,
) -> List[dict]:
    """基于分词的语速估算（不需要 TTS 文件），用于迭代优化阶段。

    改进:
      - 用 jieba 分词后按词粒度估算（双字词~400ms, 单字词~250ms, 三字词~550ms）
      - 比按字符数估算更接近实际发音时长
      - jieba 不可用时降级到字符级估算
    """
    # 尝试加载 jieba
    try:
        import jieba
        _use_jieba = True
    except ImportError:
        _use_jieba = False

    results = []
    underslow_threshold = 0.7
    for idx, seg in enumerate(segments):
        text_zh = seg.get("text_zh", "")
        target_ms = int((seg["end"] - seg["start"]) * 1000)
        if target_ms <= 0 or not text_zh.strip():
            results.append({
                "idx": idx, "speed_ratio": 0.0,
                "estimated_ms": 0, "target_ms": target_ms,
                "status": "skipped", "skip_reason": "no_text",
                "text_en": seg.get("text_en", ""),
                "text_zh": text_zh,
            })
            continue

        if _use_jieba:
            # 分词级估算：按词长分配时长（更接近实际发音）
            estimated_ms = _estimate_duration_jieba(text_zh)
        else:
            # 降级：字符级估算
            zh_chars = sum(1 for c in text_zh if '\u4e00' <= c <= '\u9fff')
            other_chars = sum(1 for c in text_zh if c.isalnum() and not ('\u4e00' <= c <= '\u9fff'))
            estimated_ms = zh_chars * ms_per_zh_char + other_chars * ms_per_en_char

        # 标点停顿 + 语句韵律延长 + 特殊符号朗读，约占 30% 额外时间
        estimated_ms *= 1.3
        ratio = estimated_ms / target_ms
        if ratio > threshold:
            status = "overfast"
        elif ratio < underslow_threshold:
            status = "underslow"
        else:
            status = "ok"
        results.append({
            "idx": idx, "speed_ratio": round(ratio, 3),
            "estimated_ms": int(estimated_ms), "target_ms": target_ms,
            "text_en": seg.get("text_en", ""),
            "text_zh": text_zh,
            "status": status,
        })
    return results


def _estimate_duration_jieba(text_zh: str) -> float:
    """用 jieba 分词后按词粒度估算朗读时长（毫秒）。

    经验值（基于 edge-tts zh-CN-YunxiNeural 实测）：
      单字词（如"的""是"）: ~200ms
      双字词（如"今天""学习"）: ~380ms
      三字词（如"计算机""互联网"）: ~530ms
      四字及以上（如"人工智能"）: ~150ms/字
      英文单词: ~150ms/字符
      URL/域名（逐字母朗读）: ~280ms/字符
      数字: ~120ms/字符
    """
    import jieba
    import unicodedata

    # 先检测 URL/域名模式：TTS 会逐字母朗读这些内容
    _URL_PATTERN = re.compile(
        r'(?:https?://)?(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}'
        r'(?:/[^\s]*)?'
    )

    # 预处理：找出 URL 并计算其独立时长，然后从文本中移除
    url_ms = 0.0
    clean_text = text_zh
    for m in _URL_PATTERN.finditer(text_zh):
        url_str = m.group()
        # URL 逐字母朗读：每个字母/符号约 280ms
        url_chars = sum(1 for c in url_str if c.isalnum() or c in './-_:')
        url_ms += url_chars * 280
        clean_text = clean_text.replace(url_str, '', 1)

    words = jieba.lcut(clean_text)
    total_ms = url_ms
    for word in words:
        # 跳过纯标点/空白
        meaningful = [c for c in word if not unicodedata.category(c).startswith(('P', 'Z', 'C'))]
        if not meaningful:
            total_ms += 50  # 标点停顿
            continue

        zh_count = sum(1 for c in meaningful if '\u4e00' <= c <= '\u9fff')
        other_count = len(meaningful) - zh_count

        if zh_count > 0:
            # 中文词：按词长分配
            if zh_count == 1:
                total_ms += 200
            elif zh_count == 2:
                total_ms += 380
            elif zh_count == 3:
                total_ms += 530
            else:
                total_ms += zh_count * 150
        if other_count > 0:
            # 英文/数字：比纯中文慢（TTS 需要切换语言）
            digits = sum(1 for c in meaningful if c.isdigit())
            letters = other_count - digits
            total_ms += letters * 150 + digits * 120

    return total_ms


def _identify_high_cps_segments(
    segments: List[dict], cps_threshold: float = 5.5,
) -> List[int]:
    """识别估算 CPS 超标的段索引（用于等时翻译多候选优化）

    使用 _estimate_duration_jieba 估算 TTS 时长，识别翻译过长的段。
    双条件触发：CPS 超标 或 估算时长/目标时长 > 1.2。
    """
    high_cps = []
    for i, seg in enumerate(segments):
        text_zh = seg.get("text_zh", "")
        zh_chars = sum(1 for c in text_zh if '\u4e00' <= c <= '\u9fff')
        if zh_chars < 3:
            continue
        target_ms = int((seg.get("end", 0) - seg.get("start", 0)) * 1000)
        if target_ms <= 500:
            continue
        target_sec = target_ms / 1000.0
        estimated_cps = zh_chars / target_sec
        estimated_ms = _estimate_duration_jieba(text_zh) * 1.1  # 含停顿
        ratio = estimated_ms / target_ms if target_ms > 0 else 0
        if estimated_cps > cps_threshold or ratio > 1.2:
            high_cps.append(i)
    return high_cps


def _identify_low_cps_segments(
    segments: List[dict], cps_threshold: float = 3.5,
) -> List[int]:
    """识别估算 CPS 过低的段索引（用于等时扩展多候选优化）

    使用 _estimate_duration_jieba 估算 TTS 时长，识别翻译过短的段。
    双条件触发：CPS 低于阈值 或 估算时长/目标时长 < 0.7。
    """
    low_cps = []
    for i, seg in enumerate(segments):
        text_zh = seg.get("text_zh", "")
        zh_chars = sum(1 for c in text_zh if '\u4e00' <= c <= '\u9fff')
        if zh_chars < 3:
            continue
        target_ms = int((seg.get("end", 0) - seg.get("start", 0)) * 1000)
        if target_ms <= 500:
            continue
        target_sec = target_ms / 1000.0
        estimated_cps = zh_chars / target_sec
        estimated_ms = _estimate_duration_jieba(text_zh) * 1.1
        ratio = estimated_ms / target_ms if target_ms > 0 else 0
        if estimated_cps < cps_threshold or ratio < 0.7:
            low_cps.append(i)
    return low_cps


def _measure_speed_ratios(
    segments: List[dict], tts_dir: Path, threshold: float = 1.5,
) -> List[dict]:
    """阶段 B: 测量每个片段 TTS 时长 vs 原始时间窗口的比率（需要 TTS 文件）"""
    from pydub import AudioSegment as PydubSegment
    results = []
    for idx, seg in enumerate(segments):
        tts_path = tts_dir / f"seg_{idx:04d}.mp3"
        target_ms = int((seg["end"] - seg["start"]) * 1000)
        if target_ms <= 0 or not tts_path.exists() or tts_path.stat().st_size == 0:
            reason = "no_tts" if (not tts_path.exists() or tts_path.stat().st_size == 0) else "zero_duration"
            results.append({
                "idx": idx, "speed_ratio": 0.0,
                "tts_ms": 0, "target_ms": target_ms, "status": "skipped",
                "skip_reason": reason,
            })
            continue
        tts_audio = PydubSegment.from_mp3(str(tts_path))
        tts_ms = len(tts_audio)
        ratio = tts_ms / target_ms
        # underslow: TTS 远短于目标时长，降速播放会不自然
        underslow_threshold = 0.7
        if ratio > threshold:
            status = "overfast"
        elif ratio < underslow_threshold and tts_ms > 0:
            status = "underslow"
        else:
            status = "ok"
        results.append({
            "idx": idx, "speed_ratio": round(ratio, 3),
            "tts_ms": tts_ms, "target_ms": target_ms,
            "text_en": seg.get("text_en", ""),
            "text_zh": seg.get("text_zh", ""),
            "status": status,
        })
    return results


async def _post_tts_calibrate(
    segments: List[dict], tts_dir: Path, config: dict,
) -> List[dict]:
    """P1.3 TTS 后校准: 测量实际 TTS 时长，对超速段精简译文并重新生成 TTS（最多 1 轮）。

    流程:
      1. 用 _measure_speed_ratios 测量实际 TTS vs 目标时长
      2. 筛选 ratio > calibration_threshold 的段
      3. 对这些段用 _refine_with_llm 精简译文
      4. 仅对改动段重新生成 TTS
    """
    import copy

    refine_cfg = config.get("refine", {})
    threshold = refine_cfg.get("calibration_threshold", 1.30)
    llm_config = config.get("llm", {})

    if not llm_config.get("api_key"):
        print("  ⚠️  后校准需要 LLM 配置，跳过")
        return segments

    # 1. 测量实际 TTS 时长
    speed_results = _measure_speed_ratios(segments, tts_dir, threshold=1.5)
    overfast = [r for r in speed_results if r["speed_ratio"] > threshold]

    if not overfast:
        print(f"  ✅ 后校准: 无超速段 (阈值={threshold}x)")
        return segments

    print(f"  🔧 后校准: {len(overfast)} 段超 {threshold}x，精简译文...")
    for item in overfast[:5]:
        print(f"     #{item['idx']:3d} ({item['speed_ratio']:.2f}x)"
              f" \"{item.get('text_zh', '')[:25]}...\"")
    if len(overfast) > 5:
        print(f"     ... 共 {len(overfast)} 段")

    # 2. 精简译文
    refined_segments = _refine_with_llm(segments, overfast, llm_config)

    # 3. 找到实际被修改的段
    changed_indices = []
    for item in overfast:
        idx = item["idx"]
        old_zh = segments[idx].get("text_zh", "")
        new_zh = refined_segments[idx].get("text_zh", "")
        if new_zh and new_zh != old_zh:
            changed_indices.append(idx)

    if not changed_indices:
        print(f"  ⚠️  后校准: LLM 未能精简任何段")
        return segments

    # 打印变更 diff (学习 run_refinement_loop 变更打印模式)
    for idx in changed_indices[:3]:
        old = segments[idx]["text_zh"][:18] + ("..." if len(segments[idx]["text_zh"]) > 18 else "")
        new = refined_segments[idx]["text_zh"][:18] + ("..." if len(refined_segments[idx]["text_zh"]) > 18 else "")
        ratio = next(r["speed_ratio"] for r in overfast if r["idx"] == idx)
        print(f"     #{idx:3d} ({ratio:.1f}x) \"{old}\" → \"{new}\"")
    if len(changed_indices) > 3:
        print(f"     ... 共 {len(changed_indices)} 处变更")

    # 保存校准结果 JSON (便于 review/debug)
    import json as _json_cal
    cal_path = _audit_dir(tts_dir.parent) / "calibration_results.json"
    cal_data = {
        "threshold": threshold,
        "overfast_count": len(overfast),
        "changed_count": len(changed_indices),
        "changes": [{
            "idx": idx,
            "old_zh": segments[idx]["text_zh"],
            "new_zh": refined_segments[idx]["text_zh"],
            "old_ratio": next(r["speed_ratio"] for r in overfast if r["idx"] == idx),
        } for idx in changed_indices],
    }
    with open(cal_path, "w", encoding="utf-8") as _f:
        _json_cal.dump(cal_data, _f, ensure_ascii=False, indent=2)

    # 4. 仅对改动段重新生成 TTS
    voice = config.get("voice", "zh-CN-YunxiNeural")
    print(f"  🔄 后校准: 重新生成 {len(changed_indices)} 段 TTS...")

    tts_items = []
    for idx in changed_indices:
        seg = refined_segments[idx]
        text_zh = seg.get("text_zh", "")
        target_dur_ms = int((seg["end"] - seg["start"]) * 1000)
        # 计算新的 rate
        rate = 1.0
        if target_dur_ms > 0:
            estimated_tts_ms = _estimate_duration_jieba(text_zh) * 1.3
            if estimated_tts_ms > 0:
                rate = estimated_tts_ms / target_dur_ms
        global_speed = config.get("global_speed", 1.0)
        align_cfg = config.get("alignment", {})
        rate_range = align_cfg.get("tts_rate_range", [0.80, 1.35])
        rate = max(rate_range[0], min(rate_range[1], rate * global_speed))
        tts_items.append({"idx": idx, "text_zh": text_zh, "rate": rate})

    # 使用 edge-tts 逐段重新生成
    import edge_tts
    for item in tts_items:
        idx = item["idx"]
        text = item["text_zh"]
        rate_pct = f"{int((item['rate'] - 1) * 100):+d}%"
        tts_path = tts_dir / f"seg_{idx:04d}.mp3"
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate_pct)
            await communicate.save(str(tts_path))
        except Exception as e:
            print(f"    ⚠️  seg_{idx:04d} TTS 重新生成失败: {e}")

    print(f"  ✅ 后校准完成: {len(changed_indices)} 段已更新")
    return refined_segments


def _detect_silence_regions(audio_path: Path, min_silence_len: int = 200,
                            silence_thresh: int = -40) -> List[tuple]:
    """检测音频中的静音区间，返回 [(start_ms, end_ms), ...] 列表。

    用于 gap borrowing: 确认段间间隙确实是静音后才允许借用。
    """
    from pydub import AudioSegment as PydubSegment
    from pydub.silence import detect_silence

    if not audio_path.exists():
        return []
    audio = PydubSegment.from_wav(str(audio_path))
    # detect_silence 返回 [[start, end], ...] 格式
    silent_ranges = detect_silence(audio, min_silence_len=min_silence_len,
                                   silence_thresh=silence_thresh)
    return [(s, e) for s, e in silent_ranges]


def _is_in_silence(position_ms: int, duration_ms: int,
                   silence_regions: List[tuple]) -> bool:
    """检查给定时间区间是否处于静音区域内（至少 70% 重叠）。"""
    if not silence_regions:
        return True  # 无静音数据时默认允许借用
    overlap = 0
    for s_start, s_end in silence_regions:
        if s_start >= position_ms + duration_ms:
            break
        if s_end <= position_ms:
            continue
        o_start = max(position_ms, s_start)
        o_end = min(position_ms + duration_ms, s_end)
        overlap += max(0, o_end - o_start)
    return overlap >= duration_ms * 0.7


def _align_tts_to_timeline(segments: List[dict], output_dir: Path,
                           cpu_threads: int = 0, global_speed: float = 1.0,
                           config: dict = None) -> Path:
    """阶段 C: 时间线对齐 → chinese_dub.wav

    两种模式（由 alignment.atempo_disabled 控制）:
      atempo_disabled=True (默认):
        TTS 原生 rate 已在生成阶段补偿时长偏差，这里直接叠加:
        - 偏短段: 居中静音填充
        - 轻微超时 (≤overflow_tolerance): 允许溢出，不截断
        - 超时 > tolerance: Gap Borrowing → Video Slowdown → 截断
      atempo_disabled=False (旧模式):
        ffmpeg atempo 后处理调速，与之前行为一致
    """
    from pydub import AudioSegment as PydubSegment

    tts_dir = output_dir / "tts_segments"
    audio_path = output_dir / "audio.wav"
    original_audio = PydubSegment.from_wav(str(audio_path))
    total_ms = len(original_audio)

    # ── 配置 ──
    align_cfg = (config or {}).get("alignment", {})
    atempo_disabled = align_cfg.get("atempo_disabled", True)
    overflow_tolerance = align_cfg.get("overflow_tolerance", 0.10)
    gap_borrowing = align_cfg.get("gap_borrowing", False)
    max_borrow_ms = align_cfg.get("max_borrow_ms", 300)
    silence_regions = []
    if gap_borrowing:
        silence_regions = _detect_silence_regions(audio_path)
        print(f"     间隙借用已启用 (上限 {max_borrow_ms}ms, 检测到 {len(silence_regions)} 个静音区间)")

    video_slowdown = align_cfg.get("video_slowdown", False)
    max_slowdown_factor = align_cfg.get("max_slowdown_factor", 0.85)
    slowdown_segments = []
    borrow_events = []
    slowdown_rejected = 0

    # 计算段间间隙 (用于 gap borrowing)
    gap_after = []
    for idx in range(len(segments)):
        if idx < len(segments) - 1:
            gap = int(segments[idx + 1]["start"] * 1000) - int(segments[idx]["end"] * 1000)
            gap_after.append(max(0, gap))
        else:
            gap_after.append(0)

    mode_label = "直接叠加(无atempo)" if atempo_disabled else "atempo调速"
    print(f"     时间线对齐中 ({mode_label})...")
    final_audio = PydubSegment.silent(duration=total_ms, frame_rate=16000)
    stats = {"adjusted": 0, "skipped": 0, "padded": 0, "truncated": 0,
             "clamped_fast": 0, "clamped_slow": 0, "borrowed": 0,
             "within_tolerance": 0, "atempo_fallback": 0}

    # ── 第一遍: 收集 raw_ratio 用于报告 ──
    raw_ratios = []
    for idx, seg in enumerate(segments):
        tts_path = tts_dir / f"seg_{idx:04d}.mp3"
        target_start = int(seg["start"] * 1000)
        target_dur = int(seg["end"] * 1000) - target_start
        if not tts_path.exists() or tts_path.stat().st_size == 0 or target_dur <= 0:
            raw_ratios.append(None)
            continue
        tts_audio = PydubSegment.from_mp3(str(tts_path))
        raw_ratios.append(len(tts_audio) / target_dur)

    # 统计 raw_ratio 分布
    raw_valid = [r for r in raw_ratios if r is not None]
    if raw_valid:
        import statistics as _stats_mod
        raw_mean = sum(raw_valid) / len(raw_valid)
        raw_std = round(_stats_mod.stdev(raw_valid), 4) if len(raw_valid) > 1 else 0.0
    else:
        raw_mean = 1.0
        raw_std = 0.0

    if atempo_disabled:
        # ══════════════════════════════════════════════════════════
        # 新模式: 直接叠加，不做 atempo 后处理
        # ══════════════════════════════════════════════════════════
        within_115 = sum(1 for r in raw_valid if 0.85 <= r <= 1.15)
        within_115_pct = round(within_115 / max(1, len(raw_valid)) * 100, 1)
        print(f"     raw_ratio 分布: 均值 {raw_mean:.3f}, 标准差 {raw_std},"
              f" [0.85-1.15] 合规 {within_115_pct}%")

        for idx, seg in enumerate(segments):
            tts_path = tts_dir / f"seg_{idx:04d}.mp3"
            if not tts_path.exists() or tts_path.stat().st_size == 0:
                stats["skipped"] += 1
                continue

            tts_audio = PydubSegment.from_mp3(str(tts_path))
            target_start = int(seg["start"] * 1000)
            target_dur = int(seg["end"] * 1000) - target_start

            if target_dur <= 0:
                stats["skipped"] += 1
                continue

            tts_len = len(tts_audio)

            if tts_len <= target_dur:
                # TTS 偏短或刚好: 居中填充静音
                gap = target_dur - tts_len
                if gap > 0:
                    pad_front = gap // 2
                    padded = PydubSegment.silent(duration=target_dur, frame_rate=16000)
                    padded = padded.overlay(tts_audio, position=pad_front)
                    tts_audio = padded
                    stats["padded"] += 1
                else:
                    stats["within_tolerance"] += 1
            else:
                # TTS 超时
                overflow_ms = tts_len - target_dur
                overflow_ratio = overflow_ms / target_dur

                if overflow_ratio <= overflow_tolerance:
                    # 轻微超时在容忍范围内，不截断
                    stats["within_tolerance"] += 1
                else:
                    # 超出容忍范围，尝试 Gap Borrowing → Video Slowdown → 截断
                    handled = False
                    if gap_borrowing and overflow_ms <= max_borrow_ms:
                        available_gap = gap_after[idx]
                        borrow_amount = min(overflow_ms, int(available_gap * 0.6), max_borrow_ms)
                        if borrow_amount >= overflow_ms:
                            gap_start_ms = int(seg["end"] * 1000)
                            if _is_in_silence(gap_start_ms, borrow_amount, silence_regions):
                                handled = True
                                stats["borrowed"] += 1
                                borrow_events.append({"idx": idx, "overflow_ms": overflow_ms,
                                                      "borrow_ms": borrow_amount})
                    if not handled:
                        if video_slowdown and overflow_ratio <= 0.15:
                            factor = target_dur / tts_len
                            if factor >= max_slowdown_factor:
                                slowdown_segments.append({
                                    "idx": idx, "start": seg["start"],
                                    "end": seg["end"], "factor": round(factor, 3),
                                    "overflow_ms": overflow_ms,
                                })
                                handled = True
                            else:
                                slowdown_rejected += 1
                        if not handled:
                            # 最后手段分级: per-segment atempo 降级 → 截断
                            speed_needed = tts_len / target_dur
                            if speed_needed <= 1.35:
                                # 溢出在 atempo 安全范围内，仅对该段做 atempo 调速
                                adj_path = tts_dir / f"seg_{idx:04d}_adj.wav"
                                filt = _build_atempo_filter(speed_needed)
                                try:
                                    subprocess.run([
                                        "ffmpeg", "-i", str(tts_path),
                                        "-filter:a", filt, "-ar", "16000",
                                        "-ac", "1", str(adj_path), "-y"
                                    ], capture_output=True, check=True, timeout=30)
                                    tts_audio = PydubSegment.from_wav(str(adj_path))
                                    stats["atempo_fallback"] += 1
                                    handled = True
                                except Exception:
                                    pass
                            if not handled:
                                tts_audio = tts_audio[:target_dur]
                                stats["truncated"] += 1

            # 平滑过渡
            CROSSFADE_MS = 30
            if len(tts_audio) > CROSSFADE_MS * 2:
                tts_audio = tts_audio.fade_in(CROSSFADE_MS).fade_out(CROSSFADE_MS)

            if target_start < total_ms:
                final_audio = final_audio.overlay(tts_audio, position=target_start)

        # 保存速度报告 (无 atempo 模式)
        outlier_count = sum(1 for r in raw_valid if r > 1.4)
        speed_report = {
            "atempo_disabled": True,
            "baseline": round(raw_mean, 4),
            "avg_clamped": round(raw_mean, 4),  # 无钳制，等于 raw
            "std_clamped": 0.0,  # 无 atempo 调速
            "std_raw": raw_std,
            "raw_ratio_mean": round(raw_mean, 4),
            "raw_ratio_within_115_pct": within_115_pct,
            "outliers_gt_1.4": outlier_count,
            "overflow_tolerance": overflow_tolerance,
            "speed_range": [0, 0],  # 无 atempo
            "total_segments": len(segments),
            "clamped_fast": 0,
            "clamped_slow": 0,
            "padded": stats["padded"],
            "truncated": stats["truncated"],
            "atempo_fallback": stats["atempo_fallback"],
            "within_tolerance": stats["within_tolerance"],
            "gap_borrowing": gap_borrowing,
            "borrow_events": borrow_events if borrow_events else [],
            "video_slowdown": video_slowdown,
            "slowdown_rejected": slowdown_rejected,
        }
        with open(_audit_dir(output_dir) / "speed_report.json", "w", encoding="utf-8") as f:
            json.dump(speed_report, f, ensure_ascii=False, indent=2)

    else:
        # ══════════════════════════════════════════════════════════
        # 旧模式: ffmpeg atempo 后处理调速 (atempo_disabled=False)
        # ══════════════════════════════════════════════════════════
        SPEED_MIN = 1.00
        SPEED_MAX = 1.25

        # Compute global baseline (trimmed mean)
        if raw_valid:
            valid_sorted = sorted(raw_valid)
            trim = max(1, len(valid_sorted) // 10)
            trimmed = valid_sorted[trim:-trim] if len(valid_sorted) > 4 else valid_sorted
            baseline = sum(trimmed) / len(trimmed)
        else:
            baseline = 1.0
        median_ratio = baseline

        # Adaptive blend toward baseline
        SMOOTH_ALPHA = 0.3
        blended_ratios = []
        for r in raw_ratios:
            if r is None:
                blended_ratios.append(None)
            else:
                deviation = abs(r - baseline)
                weight = 0.2 if deviation < 0.15 else (0.6 if deviation > 0.3 else 0.4)
                blended_ratios.append(r * (1 - weight) + baseline * weight)

        # Bidirectional exponential smoothing
        forward = list(blended_ratios)
        prev_valid = None
        for i, r in enumerate(forward):
            if r is not None:
                if prev_valid is not None:
                    forward[i] = SMOOTH_ALPHA * prev_valid + (1 - SMOOTH_ALPHA) * r
                prev_valid = forward[i]
        backward = list(blended_ratios)
        prev_valid = None
        for i in range(len(backward) - 1, -1, -1):
            r = backward[i]
            if r is not None:
                if prev_valid is not None:
                    backward[i] = SMOOTH_ALPHA * prev_valid + (1 - SMOOTH_ALPHA) * r
                prev_valid = backward[i]
        smoothed_ratios = []
        for f, b in zip(forward, backward):
            if f is None:
                smoothed_ratios.append(None)
            else:
                smoothed_ratios.append((f + b) / 2)

        # Clamp to [SPEED_MIN, SPEED_MAX]
        clamped_ratios = []
        for r in smoothed_ratios:
            if r is None:
                clamped_ratios.append(None)
            else:
                clamped_ratios.append(max(SPEED_MIN, min(SPEED_MAX, r)))
        if global_speed != 1.0:
            clamped_ratios = [
                max(SPEED_MIN, min(SPEED_MAX, r * global_speed)) if r is not None else None
                for r in clamped_ratios
            ]

        for i, (sm, cl) in enumerate(zip(smoothed_ratios, clamped_ratios)):
            if sm is not None and cl is not None:
                if sm > SPEED_MAX:
                    stats["clamped_fast"] += 1
                elif sm < SPEED_MIN:
                    stats["clamped_slow"] += 1

        clamped_valid = [r for r in clamped_ratios if r is not None]
        avg_speed = sum(clamped_valid) / max(1, len(clamped_valid))
        print(f"     全局语速基线: {median_ratio:.2f}x → 钳制后平均: {avg_speed:.2f}x"
              f" (自适应混合, 双向平滑α={SMOOTH_ALPHA})")
        if stats["clamped_fast"] or stats["clamped_slow"]:
            print(f"     钳制: {stats['clamped_fast']} 段过快被限速,"
                  f" {stats['clamped_slow']} 段过慢被提速")

        # Save speed report (legacy mode)
        import statistics as _stats_mod
        clamped_std = round(_stats_mod.stdev(clamped_valid), 4) if len(clamped_valid) > 1 else 0.0
        outlier_count = sum(1 for r in raw_valid if r > 1.4)
        speed_report = {
            "atempo_disabled": False,
            "baseline": round(median_ratio, 4),
            "avg_clamped": round(avg_speed, 4),
            "std_clamped": clamped_std,
            "std_raw": raw_std,
            "outliers_gt_1.4": outlier_count,
            "speed_range": [SPEED_MIN, SPEED_MAX],
            "total_segments": len(segments),
            "clamped_fast": stats["clamped_fast"],
            "clamped_slow": stats["clamped_slow"],
            "gap_borrowing": gap_borrowing,
            "borrow_events": borrow_events if borrow_events else [],
            "video_slowdown": video_slowdown,
            "slowdown_rejected": slowdown_rejected,
        }
        with open(_audit_dir(output_dir) / "speed_report.json", "w", encoding="utf-8") as f:
            json.dump(speed_report, f, ensure_ascii=False, indent=2)

        # Phase 1: ffmpeg atempo
        def _run_atempo(tts_path: Path, adjusted_path: Path, speed: float):
            if adjusted_path.exists():
                return True
            filt = _build_atempo_filter(speed)
            try:
                subprocess.run([
                    "ffmpeg", "-i", str(tts_path), "-filter:a", filt,
                    "-ar", "16000", "-ac", "1", str(adjusted_path), "-y"
                ], capture_output=True, check=True, timeout=30)
                return True
            except Exception:
                return False

        atempo_tasks = []
        for idx, seg in enumerate(segments):
            tts_path = tts_dir / f"seg_{idx:04d}.mp3"
            if not tts_path.exists() or tts_path.stat().st_size == 0:
                continue
            target_start = int(seg["start"] * 1000)
            target_dur = int(seg["end"] * 1000) - target_start
            if target_dur <= 0:
                continue
            speed_ratio = clamped_ratios[idx] if clamped_ratios[idx] is not None else 1.0
            raw_ratio = raw_ratios[idx] if raw_ratios[idx] is not None else speed_ratio
            adjusted = tts_dir / f"seg_{idx:04d}_adj.wav"
            if raw_ratio < SPEED_MIN and speed_ratio < 0.98:
                atempo_tasks.append((idx, tts_path, adjusted, speed_ratio))
            elif 0.5 < speed_ratio and speed_ratio != 1.0:
                atempo_tasks.append((idx, tts_path, adjusted, speed_ratio))

        max_threads = cpu_threads if cpu_threads > 0 else (os.cpu_count() or 4)
        atempo_workers = min(max_threads, len(atempo_tasks)) if atempo_tasks else 1
        if atempo_tasks:
            print(f"     并行调速: {len(atempo_tasks)} 段, {atempo_workers} 线程")
            with ThreadPoolExecutor(max_workers=atempo_workers) as pool:
                futures = {
                    pool.submit(_run_atempo, tp, ap, sp): idx
                    for idx, tp, ap, sp in atempo_tasks
                }
                for future in futures:
                    idx = futures[future]

        # Phase 2: overlay
        for idx, seg in enumerate(segments):
            tts_path = tts_dir / f"seg_{idx:04d}.mp3"
            if not tts_path.exists() or tts_path.stat().st_size == 0:
                stats["skipped"] += 1
                continue
            tts_audio = PydubSegment.from_mp3(str(tts_path))
            target_start = int(seg["start"] * 1000)
            target_dur = int(seg["end"] * 1000) - target_start
            if target_dur <= 0:
                stats["skipped"] += 1
                continue

            speed_ratio = clamped_ratios[idx] if clamped_ratios[idx] is not None else (len(tts_audio) / target_dur)
            raw_ratio = raw_ratios[idx] if raw_ratios[idx] is not None else speed_ratio

            if raw_ratio < SPEED_MIN and len(tts_audio) > 0:
                if speed_ratio < 0.98:
                    adjusted = tts_dir / f"seg_{idx:04d}_adj.wav"
                    if adjusted.exists():
                        tts_audio = PydubSegment.from_wav(str(adjusted))
                gap = target_dur - len(tts_audio)
                if gap > 0:
                    pad_front = gap // 2
                    padded = PydubSegment.silent(duration=target_dur, frame_rate=16000)
                    padded = padded.overlay(tts_audio, position=pad_front)
                    tts_audio = padded
                stats["padded"] += 1
            elif 0.5 < speed_ratio and speed_ratio != 1.0:
                adjusted = tts_dir / f"seg_{idx:04d}_adj.wav"
                if adjusted.exists():
                    tts_audio = PydubSegment.from_wav(str(adjusted))
                    stats["adjusted"] += 1

            if len(tts_audio) > target_dur:
                overflow_ms = len(tts_audio) - target_dur
                borrowed = False
                if gap_borrowing and overflow_ms <= max_borrow_ms:
                    available_gap = gap_after[idx]
                    borrow_amount = min(overflow_ms, int(available_gap * 0.6), max_borrow_ms)
                    if borrow_amount >= overflow_ms:
                        gap_start_ms = int(seg["end"] * 1000)
                        if _is_in_silence(gap_start_ms, borrow_amount, silence_regions):
                            borrowed = True
                            stats["borrowed"] += 1
                            borrow_events.append({"idx": idx, "overflow_ms": overflow_ms,
                                                  "borrow_ms": borrow_amount})
                if not borrowed:
                    overflow_ratio = overflow_ms / target_dur if target_dur > 0 else 1.0
                    if video_slowdown and overflow_ratio <= 0.15:
                        factor = target_dur / len(tts_audio)
                        if factor >= max_slowdown_factor:
                            slowdown_segments.append({
                                "idx": idx, "start": seg["start"],
                                "end": seg["end"], "factor": round(factor, 3),
                                "overflow_ms": overflow_ms,
                            })
                        else:
                            slowdown_rejected += 1
                            tts_audio = tts_audio[:target_dur]
                    else:
                        tts_audio = tts_audio[:target_dur]

            CROSSFADE_MS = 30
            if len(tts_audio) > CROSSFADE_MS * 2:
                tts_audio = tts_audio.fade_in(CROSSFADE_MS).fade_out(CROSSFADE_MS)
            if target_start < total_ms:
                final_audio = final_audio.overlay(tts_audio, position=target_start)

    # ── 写出最终配音 ──
    dub_path = output_dir / "chinese_dub.wav"
    final_audio.export(str(dub_path), format="wav")

    # 间隙借用汇总
    if borrow_events:
        total_borrow_ms = sum(e["borrow_ms"] for e in borrow_events)
        max_e = max(borrow_events, key=lambda e: e["borrow_ms"])
        print(f"     间隙借用: {len(borrow_events)} 段, "
              f"总 {total_borrow_ms}ms, 最大 #{max_e['idx']} {max_e['borrow_ms']}ms")

    if atempo_disabled:
        print(f"  ✅ 配音完成 (填充:{stats['padded']}, 容忍:{stats['within_tolerance']},"
              f" atempo降级:{stats['atempo_fallback']}, 截断:{stats['truncated']},"
              f" 借用:{stats['borrowed']}, 跳过:{stats['skipped']}, 总:{len(segments)})")
    else:
        print(f"  ✅ 配音完成 (调速:{stats['adjusted']}, 填充:{stats['padded']},"
              f" 限速:{stats['clamped_fast']}, 提速:{stats['clamped_slow']},"
              f" 借用:{stats['borrowed']}, 跳过:{stats['skipped']}, 总:{len(segments)})")

    # ── Video Slowdown 报告 ──
    if slowdown_segments:
        slowdown_path = _audit_dir(output_dir) / "slowdown_segments.json"
        with open(slowdown_path, "w", encoding="utf-8") as f:
            json.dump(slowdown_segments, f, ensure_ascii=False, indent=2)
        print(f"  🐢 视频减速标记: {len(slowdown_segments)} 段 → {slowdown_path.name}")
    if slowdown_rejected:
        print(f"     (另有 {slowdown_rejected} 段超减速上限 {max_slowdown_factor}x, 已截断)")

    return dub_path


async def generate_chinese_dub(
    segments: List[dict], output_dir: Path,
    voice: str = "zh-CN-YunxiNeural", concurrency: int = 5,
    config: dict = None,
) -> Path:
    """生成中文配音 (A→C 全流程，向后兼容)"""
    print(f"  🗣  生成配音 (voice={voice}, 并发={concurrency})...")
    tts_dir = output_dir / "tts_segments"
    await _generate_tts_segments(segments, tts_dir, voice, concurrency,
                                 config=config)
    cpu_threads = (config or {}).get("cpu_threads", 0)
    global_speed = (config or {}).get("global_speed", 1.0)
    return _align_tts_to_timeline(segments, output_dir, cpu_threads=cpu_threads,
                                  global_speed=global_speed, config=config)


def _build_atempo_filter(speed_ratio: float) -> str:
    if speed_ratio < 0.5:
        filters = []
        r = speed_ratio
        while r < 0.5:
            filters.append("atempo=0.5")
            r /= 0.5
        filters.append(f"atempo={r:.4f}")
        return ",".join(filters)
    return f"atempo={min(speed_ratio, 100.0):.4f}"


# ─── 迭代优化引擎 ──────────────────────────────────────────────────

async def run_refinement_loop(
    segments: List[dict], output_dir: Path, config: dict,
) -> List[dict]:
    """
    迭代优化主循环 (纯文本阶段，不依赖 TTS):
      字符数估算语速比 → 筛选超速片段 → LLM 精简翻译 → 重复
    TTS 在所有翻译定稿后由主流程统一生成。
    返回优化后的 segments (同时更新 segments_cache.json)
    """
    import copy

    refine_cfg = config.get("refine", {})
    max_iter = refine_cfg.get("max_iterations", 3)
    threshold = refine_cfg.get("speed_threshold", 1.5)
    resume_iter = refine_cfg.get("resume_iteration")
    llm_config = config.get("llm", {})

    if not llm_config.get("api_key"):
        print("  ⚠️  迭代优化需要 LLM 翻译引擎 (llm.api_key 为空)，跳过")
        return segments

    iter_dir = _audit_dir(output_dir) / "iterations"
    iter_dir.mkdir(exist_ok=True)

    segments = copy.deepcopy(segments)

    # ── 断点恢复 ──
    start_iter = 0
    if resume_iter is not None and resume_iter > 0:
        snap = iter_dir / f"iter_{resume_iter}_segments.json"
        if snap.exists():
            with open(snap, "r", encoding="utf-8") as f:
                segments = json.load(f)
            start_iter = resume_iter
            print(f"  ♻️  从第 {resume_iter} 轮迭代恢复 ({len(segments)} 段)")
        else:
            print(f"  ⚠️  第 {resume_iter} 轮快照不存在，从头开始")

    # 保存初始快照 (iter_0)
    if start_iter == 0:
        with open(iter_dir / "iter_0_segments.json", "w", encoding="utf-8") as f:
            json.dump(segments, f, ensure_ascii=False, indent=2)

    converged_indices = set()  # Segments that are already within threshold
    run_refinement_loop._expand_done = False  # 扩展只做一次

    for it in range(start_iter, max_iter):
        print(f"\n  {'─'*50}")
        print(f"  🔄 迭代优化 第 {it+1}/{max_iter} 轮 (阈值: >{threshold}x, 字符估算)")
        print(f"  {'─'*50}")

        # 1) 基于字符数估算语速（无需 TTS 文件）
        speed_data = _estimate_speed_ratios(segments, threshold)
        overfast = [d for d in speed_data if d["status"] == "overfast" and d["idx"] not in converged_indices]
        underslow = [d for d in speed_data if d["status"] == "underslow" and d["idx"] not in converged_indices]

        # Mark newly converged
        for d in speed_data:
            if d["status"] == "ok" and d["idx"] not in converged_indices:
                converged_indices.add(d["idx"])

        active = [d for d in speed_data if d["status"] != "skipped"]
        avg_ratio = (sum(d["speed_ratio"] for d in active) / max(1, len(active)))
        max_ratio = max((d["speed_ratio"] for d in active), default=0)
        min_ratio = min((d["speed_ratio"] for d in active if d["speed_ratio"] > 0), default=0)

        # 保存速度报告
        report = {
            "iteration": it, "threshold": threshold,
            "mode": "estimate",
            "total": len(segments), "overfast_count": len(overfast),
            "underslow_count": len(underslow),
            "max_ratio": round(max_ratio, 3),
            "min_ratio": round(min_ratio, 3),
            "avg_ratio": round(avg_ratio, 3),
            "details": speed_data,
        }
        with open(iter_dir / f"iter_{it}_speed_report.json", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        print(f"     超速片段: {len(overfast)}/{len(active)}"
              f" (最大: {max_ratio:.2f}x, 平均: {avg_ratio:.2f}x)")
        if underslow:
            print(f"     过短片段: {len(underslow)}/{len(active)}"
                  f" (最小: {min_ratio:.2f}x)")
        if converged_indices:
            print(f"     已收敛片段: {len(converged_indices)}/{len(segments)} (本轮跳过)")
        skipped = [d for d in speed_data if d["status"] == "skipped"]
        if skipped:
            print(f"     跳过片段: {len(skipped)} (无文本)")
        if overfast:
            top = sorted(overfast, key=lambda x: x["speed_ratio"], reverse=True)[:5]
            for d in top:
                zh_pre = d["text_zh"][:25] + ("..." if len(d["text_zh"]) > 25 else "")
                print(f"       #{d['idx']:3d}  {d['speed_ratio']:.2f}x  \"{zh_pre}\"")

        # 2) 无超速 → 检查过短片段（最多尝试扩展一次，避免反复扩展引入噪声）
        if not overfast:
            if underslow and not getattr(run_refinement_loop, '_expand_done', False):
                print(f"     🔄 扩展 {len(underslow)} 个过短片段...")
                underslow_indices = [item["idx"] for item in underslow]
                new_segments = _isometric_expand_batch(segments, underslow_indices, llm_config)
                # 统计扩展变更
                expand_changed = 0
                for item in underslow:
                    idx = item["idx"]
                    if segments[idx]["text_zh"] != new_segments[idx]["text_zh"]:
                        expand_changed += 1
                run_refinement_loop._expand_done = True  # 标记已尝试扩展
                if expand_changed:
                    segments = new_segments
                    print(f"     ✅ 扩展了 {expand_changed}/{len(underslow)} 个过短片段")
                    continue  # 重新估算一次，检查是否产生新的超速
                else:
                    print(f"\n  ✅ 翻译优化完成! ({len(underslow)} 个过短片段无法进一步扩展，将由时间线对齐阶段静音填充)")
            elif underslow:
                print(f"\n  ✅ 翻译优化完成! ({len(underslow)} 个过短片段将由时间线对齐阶段静音填充)")
            else:
                print(f"\n  ✅ 所有片段语速均在合理范围内，优化完成!")
            break

        new_segments = segments

        # 3) LLM 精简过长翻译
        print(f"     调用 LLM 精简 {len(overfast)} 个超速片段...")
        new_segments = _refine_with_llm(new_segments, overfast, llm_config)

        # 3.5) 过短翻译不在 refine 阶段同时扩展
        # 避免扩展产生的内容变超速后被下一轮 refine 误改，造成越改越偏
        # 过短片段统一在无超速时（步骤 2）单独处理

        # 4) 统计变更
        changes = []
        changed_indices = []
        for item in overfast:
            idx = item["idx"]
            old_zh = segments[idx]["text_zh"]
            new_zh = new_segments[idx]["text_zh"]
            if old_zh != new_zh:
                changes.append({
                    "idx": idx, "old_zh": old_zh, "new_zh": new_zh,
                    "old_ratio": item["speed_ratio"],
                })
                changed_indices.append(idx)

        if not changes:
            print(f"     翻译未发生变化，停止迭代")
            break

        print(f"     已精简 {len(changes)}/{len(overfast)} 个片段:")
        for c in changes[:3]:
            o = c["old_zh"][:18] + ("..." if len(c["old_zh"]) > 18 else "")
            n = c["new_zh"][:18] + ("..." if len(c["new_zh"]) > 18 else "")
            print(f"       #{c['idx']:3d} ({c['old_ratio']:.1f}x)"
                  f" \"{o}\" → \"{n}\"")
        if len(changes) > 3:
            print(f"       ... 共 {len(changes)} 处变更")

        # 5) 更新 segments
        segments = new_segments

        # 保存迭代快照
        next_it = it + 1
        with open(iter_dir / f"iter_{next_it}_segments.json", "w", encoding="utf-8") as f:
            json.dump(segments, f, ensure_ascii=False, indent=2)
        with open(iter_dir / f"iter_{next_it}_changes.json", "w", encoding="utf-8") as f:
            json.dump({"changed_indices": changed_indices, "changes": changes},
                      f, ensure_ascii=False, indent=2)

    # 写回主缓存
    cache_file = output_dir / "segments_cache.json"
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)

    # 输出估算汇总
    final_speed = _estimate_speed_ratios(segments, threshold)
    final_overfast = [d for d in final_speed if d["status"] == "overfast"]
    final_active = [d for d in final_speed if d["status"] != "skipped"]
    final_max = max((d["speed_ratio"] for d in final_active), default=0)
    final_min = min((d["speed_ratio"] for d in final_active if d["speed_ratio"] > 0), default=0)
    final_avg = sum(d["speed_ratio"] for d in final_active) / max(1, len(final_active))

    print(f"\n  {'━'*50}")
    print(f"  📊 迭代优化完成（字符估算）:")
    print(f"     总片段: {len(final_active)}, 仍超速: {len(final_overfast)}")
    print(f"     最大: {final_max:.2f}x, 最小: {final_min:.2f}x, 平均: {final_avg:.2f}x")
    if final_overfast:
        print(f"  💡 建议：播放 final.mp4 实际审听，若仍有语速问题可再次运行:")
        print(f"     python pipeline.py --resume-from {output_dir} --refine 5")
    else:
        print(f"  ✅ 翻译已优化，接下来将生成 TTS 并校验实际语速")
    print(f"  {'━'*50}")

    return segments


def _is_duplicate_of_neighbors(
    new_zh: str, idx: int, segments: List[dict],
    similarity_threshold: float = 0.6,
) -> bool:
    """
    检查 new_zh 是否与相邻段的中文翻译重复或高度相似。
    用于防止 LLM 精简/扩展时偷懒复制相邻段内容。
    """
    new_zh_stripped = new_zh.strip()
    if len(new_zh_stripped) < 4:
        return False

    neighbors = []
    if idx > 0:
        neighbors.append(segments[idx - 1].get("text_zh", ""))
    if idx < len(segments) - 1:
        neighbors.append(segments[idx + 1].get("text_zh", ""))

    for nb in neighbors:
        nb_stripped = nb.strip()
        if not nb_stripped:
            continue
        # 完全相同
        if new_zh_stripped == nb_stripped:
            return True
        # 子串包含
        if new_zh_stripped in nb_stripped or nb_stripped in new_zh_stripped:
            return True
        # 基于字符重叠率的相似度（对中文近义改写更有效）
        shorter_len = min(len(new_zh_stripped), len(nb_stripped))
        if shorter_len >= 4:
            overlap = _char_overlap_ratio(new_zh_stripped, nb_stripped)
            if overlap > similarity_threshold:
                return True
    return False


def _char_overlap_ratio(s1: str, s2: str) -> float:
    """
    计算两个字符串的字符重叠率 (公共字符数 / 较短串长度)。
    忽略标点和空白，适合中文近义句子的相似度判断。
    """
    # 过滤掉标点和空白
    import unicodedata
    def _meaningful_chars(s):
        return [c for c in s if not unicodedata.category(c).startswith(('P', 'Z'))]

    chars1 = _meaningful_chars(s1)
    chars2 = _meaningful_chars(s2)

    if not chars1 or not chars2:
        return 0.0

    # 使用多重集合（Counter）计算共有字符数
    from collections import Counter
    c1 = Counter(chars1)
    c2 = Counter(chars2)
    common = sum((c1 & c2).values())
    shorter = min(sum(c1.values()), sum(c2.values()))
    return common / shorter if shorter > 0 else 0.0


def _check_refine_fidelity(original_zh: str, candidate_zh: str,
                            min_overlap: float = 0.25) -> bool:
    """检查精简候选是否保持了与原文的语义忠实度。

    通过字符重叠率判断：合法精简应与原文共享关键字符，
    而错误的跨段污染则几乎没有重叠。

    Args:
        original_zh: 原始中文翻译
        candidate_zh: 精简候选
        min_overlap: 最低字符重叠率阈值（默认 0.25）

    Returns:
        True 表示候选通过忠实度检查
    """
    if not original_zh or not candidate_zh:
        return False
    overlap = _char_overlap_ratio(original_zh, candidate_zh)
    return overlap >= min_overlap


def _refine_with_llm(
    segments: List[dict], overfast_items: List[dict], llm_config: dict,
) -> List[dict]:
    """使用 LLM 精简过长的翻译（多候选 + 分词估算选优）

    改进:
      1. 上下文只传截断摘要（30字）+ 明确标注"仅供参考，不要重复"
      2. 每段生成 3 个梯度候选（轻度/中度/大幅精简）
      3. 用 jieba 分词估算每个候选的朗读时长，选最接近目标的
      4. 语义忠实度检查：排除与原文字符重叠率过低的候选
      5. 小批次处理（默认 5）：减少 LLM 跨段内容混淆
    """
    import httpx
    import copy

    refined = copy.deepcopy(segments)

    api_url = llm_config["api_url"].rstrip("/")
    api_key = llm_config["api_key"]
    model = llm_config["model"]
    temperature = llm_config.get("temperature", 0.3)
    # 精简专用小批次，避免 LLM 在大批量中混淆不同段落内容
    batch_size = min(llm_config.get("batch_size", 15), 5)

    endpoint = (api_url if "/chat/completions" in api_url
                else f"{api_url}/chat/completions")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    CONTEXT_TRUNCATE = 30  # 上下文截断长度

    system_prompt = (
        "你是专业的英中翻译优化器。以下中文翻译将用于视频配音，"
        "但当前翻译朗读时间超出原始时间窗口，需要精简。\n"
        "为每段生成 3 个精简版本：\n"
        "  [轻] 轻度精简（约缩短 15%）：去除冗余修饰，保留完整信息\n"
        "  [中] 中度精简（约缩短 30%）：简化句式，保留核心信息\n"
        "  [短] 大幅精简（约缩短 45%）：只保留最核心语义\n\n"
        "规则：\n"
        "1) 必须忠实翻译英文原文，不得偏离原文含义\n"
        "2) 每个 [编号] 的精简版本必须严格对应该编号的英文原文，严禁混用其他编号的内容\n"
        "3) 精简是缩短同一句话，不是替换成其他句子——精简结果必须与当前翻译含义一致\n"
        "4) 计算机缩写保留英文原词（如 API、SDK、HTTP、GPU 等），不加括号注音\n"
        "5) 忠实原文语义，不要为凑字数而曲解原意\n"
        "6) 严禁重复上下文内容——上下文摘要仅供避免重复参考，不要从中取内容\n"
        "7) 适合配音朗读，语句自然\n"
        "8) 输出格式：每段先 [编号]，然后分行输出 [轻]/[中]/[短] 三个版本"
    )

    for i in range(0, len(overfast_items), batch_size):
        batch = overfast_items[i:i + batch_size]
        lines = []
        for j, item in enumerate(batch):
            idx = item["idx"]
            ratio = item["speed_ratio"]
            reduction = int((1 - 1.0 / ratio) * 100)
            # 上下文只取截断摘要，明确标注仅供参考
            prev_zh = segments[idx - 1]["text_zh"][:CONTEXT_TRUNCATE] if idx > 0 else ""
            next_zh = (segments[idx + 1]["text_zh"][:CONTEXT_TRUNCATE]
                       if idx < len(segments) - 1 else "")
            context_hint = ""
            if prev_zh:
                context_hint += f"  上文摘要（仅供避免重复，不要从中取内容）: {prev_zh}...\n"
            if next_zh:
                context_hint += f"  下文摘要（仅供避免重复，不要从中取内容）: {next_zh}..."
            lines.append(
                f"[{j+1}]\n"
                f"  英文: {item['text_en']}\n"
                f"  当前翻译: {item['text_zh']}\n"
                f"  需缩短约 {reduction}% (当前需 {ratio:.1f}x 加速)\n"
                + (context_hint if context_hint else "")
            )

        user_msg = (
            f"请为以下 {len(batch)} 段翻译各生成 [轻]/[中]/[短] 三个精简版本：\n\n"
            + "\n\n".join(lines)
        )

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            "temperature": temperature,
            "max_tokens": 4096,
        }

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"].strip()

            # 解析多候选结果
            candidates_per_item = _parse_multi_candidates(content, len(batch))

            for item, candidates in zip(batch, candidates_per_item):
                idx = item["idx"]
                target_ms = int((segments[idx]["end"] - segments[idx]["start"]) * 1000)

                # 从候选中选最接近目标时长的
                best_zh = _select_best_candidate(
                    candidates, target_ms, item["text_zh"], idx, refined)

                if best_zh and len(best_zh) < len(item["text_zh"]):
                    refined[idx]["text_zh"] = best_zh
        except Exception as e:
            print(f"     ⚠️  LLM 精简批次 {i//batch_size+1} 失败: {e}")

        print(f"     精简进度: {min(i + batch_size, len(overfast_items))}/{len(overfast_items)}")

    total_refined = sum(1 for item in overfast_items
                        if refined[item["idx"]]["text_zh"] != segments[item["idx"]]["text_zh"])
    print(f"     精简完成: {total_refined}/{len(overfast_items)} 段已更新")
    return refined


# ── 多音字同音替换词典 ──────────────────────────────────────────
# edge-tts 不支持 SSML phoneme 标签，只能通过文本级同音字替换
# 来纠正高频误读。格式: (正则模式, 替换文本)
# 仅覆盖 TTS 高频误读场景，不做全量多音字处理。
_POLYPHONE_RULES: List[tuple] = [
    # ── 了 (le/liǎo) ──
    # "了解/了然/了不起/了无/了如指掌/了结/了事/了断/了却" → liǎo
    # edge-tts 默认读 le，需替换为同音字 "瞭"
    (re.compile(r"了(解|然|不起|无|如指掌|结|事|断|却|得|望)"), r"瞭\1"),
    # ── 得 (de/dé/děi) ──
    # "获得/取得/得到/得出/得益/得分/得力/心得" → dé
    # edge-tts 在这些词中可能误读为轻声 de
    (re.compile(r"(获|取|觉|值|舍|记)(得)"), r"\1\2"),  # 这些通常读对，保留
    (re.compile(r"得(亏|靠)"), r"得\1"),  # děi，通常读对
    # ── 行 (háng/xíng) ──
    # "行业/银行/行列/行情/行号/行距/同行/内行/外行" → háng
    (re.compile(r"(银|央|商|投|同|内|外|在)(行)(?![动进走为驶驰了])"), r"\1杭"),
    (re.compile(r"行(业|列|情|号|距|家|当|规)"), r"杭\1"),
    # ── 数 (shù/shǔ) ──
    # "数据/数量/数字/数组/数值/参数/变数/常数/函数" → shù (名词)
    # "数一数二/数不胜数" → shǔ (动词)
    (re.compile(r"数(一数二|不胜数|落|说)"), r"属\1"),
    # ── 重 (zhòng/chóng) ──
    # "重新/重复/重建/重启/重来/重试/重置/重写" → chóng
    (re.compile(r"重(新|复|建|启|来|试|置|写|装|做|现|返|叠|演|申|审|组|设|排|整|定义|命名|构|塑|回|制|载|开|发|拨|提|归)"), r"虫\1"),
    # ── 处 (chù/chǔ) ──
    # "处理/处于/相处/处置/处罚/处分" → chǔ
    # "到处/各处/用处/好处/坏处/长处/短处/何处/深处" → chù
    (re.compile(r"(到|各|用|好|坏|长|短|何|深|远|近|随|四|别|妙|益)(处)"), r"\1触"),
    # ── 调 (diào/tiáo) ──
    # "调用/调试/调度/调配" → diào
    # "调整/调节/调解/调和/协调" → tiáo
    (re.compile(r"(协)(调)"), r"\1条"),
    (re.compile(r"调(整|节|解|和|配|谐|制|控|理|频|幅)"), r"条\1"),
    # ── 率 (lǜ/shuài) ──
    # "效率/频率/概率/比率/速率/利率/税率/汇率" → lǜ
    # "率领/率先/率队" → shuài
    (re.compile(r"率(领|先|队|部|军|众|性|直|真)"), r"帅\1"),
    # ── 量 (liàng/liáng) ──
    # "测量/丈量/量体裁衣" → liáng (动词)
    # "数量/质量/流量" → liàng (名词，通常读对)
    (re.compile(r"(测|丈|衡|估|计|度|称)(量)"), r"\1良"),
    # ── 传 (chuán/zhuàn) ──
    # "传记/自传/外传/列传/经传" → zhuàn
    (re.compile(r"(自|外|列|内|经|别|正|小|评)(传)(?![输送播递达承感染导])"), r"\1撰"),
    # ── 应 (yīng/yìng) ──
    # "应该/应当/应有" → yīng
    # "应用/应对/响应/适应/反应" → yìng (通常读对)
    (re.compile(r"应(该|当|有尽有|许|属|予)"), r"英\1"),
    # ── 乐 (lè/yuè) ──
    # "音乐/乐器/乐曲/乐队/乐谱/乐章" → yuè
    (re.compile(r"(音)(乐)(?!趣|观)"), r"\1月"),
    (re.compile(r"乐(器|曲|队|谱|章|团|坛|理|律|感|手)"), r"月\1"),
    # ── 的 (de/dí/dì) ──
    # "的确/的当" → dí，edge-tts 常误读为 de
    (re.compile(r"的(确|当)"), r"滴\1"),
    # ── 差 (chā/chà/chāi) ──
    # "差异/差距/差别/偏差/误差/温差/时差/落差" → chā
    # "差不多/差点" → chà (通常读对)
    # "出差/差事/差遣" → chāi
    (re.compile(r"(出)(差)(?!异|距|别|值|额|价|分|评|错)"), r"\1拆"),
    (re.compile(r"差(遣|事|役|使)"), r"拆\1"),
]


def _fix_polyphones(text: str) -> str:
    """对 TTS 输入文本做多音字同音替换，纠正 edge-tts 高频误读。

    仅处理有明确上下文规则的高频多音字，不做全量替换。
    替换字为同音字（读音一致、字形不同），不影响语义理解。
    """
    if not text:
        return text
    for pattern, repl in _POLYPHONE_RULES:
        text = pattern.sub(repl, text)
    return text


def _clean_refine_artifacts(text: str) -> str:
    """清理翻译文本中残留的 refine 格式标签。

    处理 LLM 输出中可能泄漏的标签格式：
      **[轻]** xxx → xxx
      - [中] xxx   → xxx
      [短] xxx     → xxx
    以及 LLM 回显的系统指令文本。
    """
    if not text:
        return text
    # 去除行首的 markdown/列表标记 + [轻]/[中]/[短]/[轻扩]/[中扩]/[重扩] 标签
    text = re.sub(r"^[-*]*\s*\*{0,2}\[(轻|中|短|轻扩|中扩|重扩)\]\*{0,2}\s*", "", text.strip())
    # 如果整行都是系统指令回显（如"以下为每段翻译的三个精简版本..."），返回空
    if re.search(r"(轻扩?|中扩?|短|重扩).*[/／].*(轻扩?|中扩?|短|重扩)", text):
        return ""
    return text.strip()


def _parse_multi_candidates(content: str, expected_count: int) -> List[List[str]]:
    """解析 LLM 多候选精简结果。

    预期格式：
      [1]
      [轻] xxx
      [中] xxx
      [短] xxx
      [2]
      ...

    也兼容 LLM 的非标准变体：
      **[轻]** xxx  (markdown 加粗)
      - [轻] xxx    (列表符号)

    返回: [[候选1, 候选2, 候选3], [候选1, ...], ...]
    """
    results = []
    current_candidates = []

    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue

        # 跳过 LLM 回显系统指令的行（如 "以下为每段翻译的三个精简版本..."）
        if re.search(r"[轻中短].*[/／].*[轻中短]", line):
            continue

        # 新段落标记 [N] 或 **[N]**
        if re.match(r"^\*{0,2}\[(\d+)\]\*{0,2}$", line) or re.match(r"^(\d+)\.", line):
            if current_candidates:
                results.append(current_candidates)
            current_candidates = []
            continue

        # 匹配 [轻]/[中]/[短] 标签，兼容 markdown 和列表前缀
        # 覆盖: [轻] xxx, **[轻]** xxx, - [轻] xxx, * [轻] xxx
        tag_match = re.match(
            r"^[-*]*\s*\*{0,2}\[([轻中短])\]\*{0,2}\s*(.+)$", line)
        if tag_match:
            text = tag_match.group(2).strip()
            # 清理可能的 think block 残留
            text = _strip_think_block(text) if '<think>' in text else text
            # 最终清理残留标签
            text = _clean_refine_artifacts(text)
            if text:
                current_candidates.append(text)
            continue

        # 降级：没有标签的行，可能是单候选格式
        clean = _strip_numbered_prefix(line) if re.match(r"^\[\d+\]", line) else line
        clean = _clean_refine_artifacts(clean)
        if clean and len(clean) >= 2:
            current_candidates.append(clean)

    if current_candidates:
        results.append(current_candidates)

    # 补齐不足的
    while len(results) < expected_count:
        results.append([])

    return results[:expected_count]


def _parse_expand_candidates(content: str, expected_count: int) -> List[List[str]]:
    """解析 LLM 多候选扩展结果。

    预期格式：
      [1]
      [轻扩] xxx
      [中扩] xxx
      [重扩] xxx
      [2]
      ...

    也兼容 markdown 加粗、列表符号等变体。
    返回: [[候选1, 候选2, 候选3], [候选1, ...], ...]
    """
    results = []
    current_candidates = []

    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue

        # 跳过 LLM 回显系统指令的行
        if re.search(r"(轻扩|中扩|重扩).*[/／].*(轻扩|中扩|重扩)", line):
            continue

        # 新段落标记 [N] 或 **[N]**
        if re.match(r"^\*{0,2}\[(\d+)\]\*{0,2}$", line) or re.match(r"^(\d+)\.", line):
            if current_candidates:
                results.append(current_candidates)
            current_candidates = []
            continue

        # 匹配 [轻扩]/[中扩]/[重扩] 标签
        tag_match = re.match(
            r"^[-*]*\s*\*{0,2}\[(轻扩|中扩|重扩)\]\*{0,2}\s*(.+)$", line)
        if tag_match:
            text = tag_match.group(2).strip()
            text = _strip_think_block(text) if '<think>' in text else text
            text = _clean_refine_artifacts(text)
            if text:
                current_candidates.append(text)
            continue

        # 降级：没有标签的行
        clean = _strip_numbered_prefix(line) if re.match(r"^\[\d+\]", line) else line
        clean = _clean_refine_artifacts(clean)
        if clean and len(clean) >= 2:
            current_candidates.append(clean)

    if current_candidates:
        results.append(current_candidates)

    while len(results) < expected_count:
        results.append([])

    return results[:expected_count]


def _select_best_candidate(
    candidates: List[str], target_ms: int, original_zh: str,
    idx: int, segments: List[dict],
    allow_same_length: bool = False,
    mode: str = "shrink",
    fidelity_threshold: float = 0.25,
) -> str:
    """从多个候选中选最接近目标时长的，同时排除不合格候选。

    mode:
      "shrink" — 精简方向：候选须比原文短（或同等长度），选最接近 target 的
      "fill"   — 扩展方向：候选须比原文长，选最长且不超 target 的

    选择策略：
      1. 排除与相邻段重复的候选
      2. 长度过滤（取决于 mode）
      3. 排除与原文语义忠实度过低的候选（防止跨段内容污染）
      4. 用 jieba 分词估算每个候选的朗读时长
      5. shrink: 选时长最接近 target_ms 且不超出的；都超出则选最短的
         fill:   选时长最接近 target_ms 且不超出的**最长**候选；都超出则选最短的
    """
    if not candidates:
        return ""

    try:
        _use_jieba = True
        import jieba  # noqa: F401
    except ImportError:
        _use_jieba = False

    valid = []
    for cand in candidates:
        if not cand or len(cand.strip()) < 2:
            continue
        # 清理残留的 refine 格式标签
        cand = _clean_refine_artifacts(cand)
        if not cand or len(cand) < 2:
            continue
        # 长度过滤
        if mode == "fill":
            # 扩展模式：候选必须比原文长，且不超过 2 倍
            if len(cand) <= len(original_zh):
                continue
            if len(cand) > len(original_zh) * 2.0:
                continue
        elif allow_same_length:
            if len(cand) > len(original_zh) * 1.1:
                continue
        else:
            if len(cand) >= len(original_zh):
                continue
        # 排除与邻段重复的
        if _is_duplicate_of_neighbors(cand, idx, segments):
            continue
        # 排除与原文语义忠实度过低的候选（防止 LLM 跨段内容混淆）
        if not _check_refine_fidelity(original_zh, cand, min_overlap=fidelity_threshold):
            continue
        valid.append(cand)

    if not valid:
        return ""

    if _use_jieba:
        # 分词估算时长，选最接近目标的
        scored = []
        for cand in valid:
            est_ms = _estimate_duration_jieba(cand) * 1.1  # 含停顿
            diff = abs(est_ms - target_ms)
            over = est_ms > target_ms  # 是否超出目标
            scored.append((cand, est_ms, diff, over))

        not_over = [s for s in scored if not s[3]]
        if mode == "fill":
            # 扩展方向：选不超出目标的最长候选（尽量填满时间窗）
            if not_over:
                best = max(not_over, key=lambda s: s[1])
            else:
                best = min(scored, key=lambda s: s[1])
        else:
            # 精简方向：选不超出目标的最接近候选
            if not_over:
                best = min(not_over, key=lambda s: s[2])
            else:
                best = min(scored, key=lambda s: s[1])
        return best[0]
    else:
        # 降级：选字符长度最接近 target 的（粗估 250ms/字）
        scored = []
        for cand in valid:
            zh_chars = sum(1 for c in cand if '\u4e00' <= c <= '\u9fff')
            est_ms = zh_chars * 250 * 1.1
            diff = abs(est_ms - target_ms)
            scored.append((cand, diff))
        return min(scored, key=lambda s: s[1])[0]


def _isometric_translate_batch(
    segments: List[dict], high_cps_indices: List[int], llm_config: dict,
) -> List[dict]:
    """等时翻译：为高 CPS 段生成多候选长度变体并选优

    在翻译完成后运行，对估算 CPS 超标的段生成 [轻]/[中]/[短] 三个长度变体，
    用 jieba 分词估算时长选最接近目标的候选。

    复用现有基础设施:
      - _parse_multi_candidates() 解析 [轻]/[中]/[短]
      - _select_best_candidate() 候选选择（allow_same_length=True）
      - _estimate_duration_jieba() 时长估算
    """
    import httpx
    import copy

    result = copy.deepcopy(segments)
    CHARS_PER_SEC = 4.5

    api_url = llm_config["api_url"].rstrip("/")
    api_key = llm_config["api_key"]
    model = llm_config["model"]
    temperature = llm_config.get("temperature", 0.3)
    batch_size = min(llm_config.get("batch_size", 15), 5)

    endpoint = (api_url if "/chat/completions" in api_url
                else f"{api_url}/chat/completions")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    CONTEXT_TRUNCATE = 30

    system_prompt = (
        "你是专业的英中视频配音翻译专家。以下英文已有初始中文翻译，"
        "但翻译朗读时长可能不匹配原始时间窗口。\n"
        "请为每段生成 3 个不同长度的中文翻译版本：\n"
        "  [轻] 标准版（接近初始翻译长度，微调表达使其更适合朗读）\n"
        "  [中] 紧凑版（缩短约 15-20%，精简表达但保留完整信息）\n"
        "  [短] 精简版（缩短约 30-40%，只保留核心语义）\n\n"
        "规则：\n"
        "1) 三个版本都必须忠实翻译英文原文，不得偏离原文含义\n"
        "2) 每个 [编号] 的版本必须严格对应该编号的英文原文，严禁混用其他编号的内容\n"
        "3) 计算机缩写保留英文原词（如 API、SDK、HTTP、GPU 等），不加括号注音\n"
        "4) 忠实原文语义，不要为缩短而曲解原意\n"
        "5) 严禁重复上下文内容——上下文摘要仅供避免重复参考，不要从中取内容\n"
        "6) 适合配音朗读，语句自然，短句为主\n"
        "7) 输出格式：每段先 [编号]，然后分行输出 [轻]/[中]/[短] 三个版本"
    )

    # 构建待处理列表
    items = []
    for idx in high_cps_indices:
        seg = segments[idx]
        dur_sec = seg.get("end", 0) - seg.get("start", 0)
        target_chars = max(2, int(dur_sec * CHARS_PER_SEC))
        zh_chars = sum(1 for c in seg.get("text_zh", "") if '\u4e00' <= c <= '\u9fff')
        items.append({
            "idx": idx,
            "text_en": seg.get("text_en", seg.get("text", "")),
            "text_zh": seg.get("text_zh", ""),
            "zh_chars": zh_chars,
            "target_chars": target_chars,
            "dur_sec": dur_sec,
        })

    adopted = 0
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        lines = []
        for j, item in enumerate(batch):
            idx = item["idx"]
            prev_zh = segments[idx - 1]["text_zh"][:CONTEXT_TRUNCATE] if idx > 0 else ""
            next_zh = (segments[idx + 1]["text_zh"][:CONTEXT_TRUNCATE]
                       if idx < len(segments) - 1 else "")
            context_hint = ""
            if prev_zh:
                context_hint += f"  上文摘要（仅供参考，不要从中取内容）: {prev_zh}...\n"
            if next_zh:
                context_hint += f"  下文摘要（仅供参考，不要从中取内容）: {next_zh}..."
            lines.append(
                f"[{j+1}]\n"
                f"  英文: {item['text_en']}\n"
                f"  当前翻译({item['zh_chars']}字): {item['text_zh']}\n"
                f"  目标≈{item['target_chars']}字 (时间窗口 {item['dur_sec']:.1f}秒)\n"
                + (context_hint if context_hint else "")
            )

        user_msg = (
            f"请为以下 {len(batch)} 段翻译各生成 [轻]/[中]/[短] 三个长度版本：\n\n"
            + "\n\n".join(lines)
        )

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            "temperature": temperature,
            "max_tokens": 4096,
        }

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"].strip()

            candidates_per_item = _parse_multi_candidates(content, len(batch))

            for item, candidates in zip(batch, candidates_per_item):
                idx = item["idx"]
                target_ms = int((segments[idx]["end"] - segments[idx]["start"]) * 1000)

                best_zh = _select_best_candidate(
                    candidates, target_ms, item["text_zh"], idx, result,
                    allow_same_length=True)

                if best_zh:
                    result[idx]["text_zh"] = best_zh
                    adopted += 1
        except Exception as e:
            print(f"     ⚠️  等时翻译批次 {i//batch_size+1} 失败: {e}")

        print(f"     等时进度: {min(i + batch_size, len(items))}/{len(items)}")

    print(f"     等时翻译完成: {adopted}/{len(items)} 段已优化")
    return result


def _isometric_expand_batch(
    segments: List[dict], low_cps_indices: List[int], llm_config: dict,
) -> List[dict]:
    """等时扩展：为低 CPS 段生成多候选扩展变体并选优

    在翻译完成后运行，对估算 CPS 过低的段生成 [轻扩]/[中扩]/[重扩] 三个扩展变体，
    用 jieba 分词估算时长选最接近目标的候选。

    复用现有基础设施:
      - _parse_expand_candidates() 解析 [轻扩]/[中扩]/[重扩]
      - _select_best_candidate(mode="fill") 候选选择（扩展方向）
      - _estimate_duration_jieba() 时长估算
    """
    import httpx
    import copy

    result = copy.deepcopy(segments)
    CHARS_PER_SEC = 4.5

    api_url = llm_config["api_url"].rstrip("/")
    api_key = llm_config["api_key"]
    model = llm_config["model"]
    temperature = llm_config.get("temperature", 0.3)
    batch_size = min(llm_config.get("batch_size", 15), 5)

    endpoint = (api_url if "/chat/completions" in api_url
                else f"{api_url}/chat/completions")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    CONTEXT_TRUNCATE = 30

    system_prompt = (
        "你是专业的英中视频配音翻译专家。以下英文已有初始中文翻译，"
        "但翻译朗读时长远短于原始时间窗口，需要适度扩展。\n"
        "请为每段生成 3 个不同长度的扩展版本：\n"
        "  [轻扩] 轻度扩展（约增加 15-20%）：补充修饰语使表达更完整自然\n"
        "  [中扩] 中度扩展（约增加 30-40%）：基于英文原文补充细节和修饰\n"
        "  [重扩] 重度扩展（约增加 50-60%）：充分展开英文原文的含义\n\n"
        "规则：\n"
        "1) 扩展必须严格基于对应编号的英文原文含义，严禁引入英文中没有的信息\n"
        "2) 每个 [编号] 的扩展版本必须严格对应该编号的英文原文，严禁混用其他编号的内容\n"
        "3) 必须保留原译文的核心词汇，只在原译文基础上补充修饰语或使表达更完整\n"
        "4) 计算机缩写保留英文原词（如 API、SDK、HTTP、GPU 等），不加括号注音\n"
        "5) 不要为凑字数而加入原文没有的信息\n"
        "6) 严禁重复上下文内容——上下文摘要仅供避免重复参考，不要从中取内容\n"
        "7) 适合配音朗读，语句自然，短句为主\n"
        "8) 输出格式：每段先 [编号]，然后分行输出 [轻扩]/[中扩]/[重扩] 三个版本"
    )

    # 构建待处理列表
    items = []
    for idx in low_cps_indices:
        seg = segments[idx]
        dur_sec = seg.get("end", 0) - seg.get("start", 0)
        target_chars = max(2, int(dur_sec * CHARS_PER_SEC))
        zh_chars = sum(1 for c in seg.get("text_zh", "") if '\u4e00' <= c <= '\u9fff')
        items.append({
            "idx": idx,
            "text_en": seg.get("text_en", seg.get("text", "")),
            "text_zh": seg.get("text_zh", ""),
            "zh_chars": zh_chars,
            "target_chars": target_chars,
            "dur_sec": dur_sec,
        })

    adopted = 0
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        lines = []
        for j, item in enumerate(batch):
            idx = item["idx"]
            prev_zh = segments[idx - 1]["text_zh"][:CONTEXT_TRUNCATE] if idx > 0 else ""
            next_zh = (segments[idx + 1]["text_zh"][:CONTEXT_TRUNCATE]
                       if idx < len(segments) - 1 else "")
            context_hint = ""
            if prev_zh:
                context_hint += f"  上文摘要（仅供参考，不要从中取内容）: {prev_zh}...\n"
            if next_zh:
                context_hint += f"  下文摘要（仅供参考，不要从中取内容）: {next_zh}..."
            lines.append(
                f"[{j+1}]\n"
                f"  英文: {item['text_en']}\n"
                f"  当前翻译({item['zh_chars']}字): {item['text_zh']}\n"
                f"  目标≈{item['target_chars']}字 (时间窗口 {item['dur_sec']:.1f}秒)\n"
                + (context_hint if context_hint else "")
            )

        user_msg = (
            f"请为以下 {len(batch)} 段翻译各生成 [轻扩]/[中扩]/[重扩] 三个扩展版本：\n\n"
            + "\n\n".join(lines)
        )

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            "temperature": temperature,
            "max_tokens": 4096,
        }

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"].strip()

            candidates_per_item = _parse_expand_candidates(content, len(batch))

            for item, candidates in zip(batch, candidates_per_item):
                idx = item["idx"]
                target_ms = int((segments[idx]["end"] - segments[idx]["start"]) * 1000)

                best_zh = _select_best_candidate(
                    candidates, target_ms, item["text_zh"], idx, result,
                    mode="fill", fidelity_threshold=0.15)

                if best_zh:
                    result[idx]["text_zh"] = best_zh
                    adopted += 1
        except Exception as e:
            print(f"     ⚠️  等时扩展批次 {i//batch_size+1} 失败: {e}")

        print(f"     扩展进度: {min(i + batch_size, len(items))}/{len(items)}")

    print(f"     等时扩展完成: {adopted}/{len(items)} 段已优化")
    return result


def _expand_with_llm(
    segments: List[dict], underslow_items: List[dict], llm_config: dict,
) -> List[dict]:
    """使用 LLM 扩展过短的翻译，使配音时长更接近原始时间窗口"""
    import httpx
    import copy

    expanded = copy.deepcopy(segments)

    api_url = llm_config["api_url"].rstrip("/")
    api_key = llm_config["api_key"]
    model = llm_config["model"]
    temperature = llm_config.get("temperature", 0.3)
    batch_size = llm_config.get("batch_size", 15)

    endpoint = (api_url if "/chat/completions" in api_url
                else f"{api_url}/chat/completions")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    system_prompt = (
        "你是专业的英中翻译优化器。以下中文翻译将用于视频配音，"
        "当前翻译太短，需要适当扩展。\n"
        "严格要求：\n"
        "1) 只能基于对应英文原文的含义来扩展，严禁引入英文中没有的信息\n"
        "2) 扩展后的中文必须保留原译文的核心词汇，只补充修饰语或使表达更完整\n"
        "3) 扩展幅度要适度，目标字数已标注，不要大幅超出\n"
        "4) 只输出扩展后的翻译，保持 [编号] 格式，一行一句\n"
        "5) 不要使用破折号连接长从句，保持短句结构"
    )

    for i in range(0, len(underslow_items), batch_size):
        batch = underslow_items[i:i + batch_size]
        lines = []
        for j, item in enumerate(batch):
            idx = item["idx"]
            ratio = item["speed_ratio"]
            expansion = int((1.0 / ratio - 1) * 100) if ratio > 0 else 50
            prev_zh = segments[idx - 1]["text_zh"] if idx > 0 else "(开头)"
            next_zh = (segments[idx + 1]["text_zh"]
                       if idx < len(segments) - 1 else "(结尾)")
            # 计算目标字数（基于时间窗口，约 4 字/秒）
            target_dur = segments[idx]["end"] - segments[idx]["start"]
            current_chars = sum(1 for c in item["text_zh"] if '\u4e00' <= c <= '\u9fff')
            target_chars = min(int(target_dur * 4), current_chars * 2)  # 不超过 2 倍
            lines.append(
                f"[{j+1}]\n"
                f"  英文: {item['text_en']}\n"
                f"  当前翻译({current_chars}字): {item['text_zh']}\n"
                f"  目标: 扩展到约 {target_chars} 个中文字\n"
                f"  上文: {prev_zh}"
            )

        user_msg = (
            f"请扩展以下 {len(batch)} 段翻译，使其朗读时长更接近英文原文。"
            f"每段用 [编号] 格式输出扩展后的译文，一行一句：\n\n"
            + "\n\n".join(lines)
        )

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            "temperature": temperature,
            "max_tokens": 4096,
        }

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"].strip()

            translations = _parse_numbered_translations(content, len(batch))
            for item, new_zh in zip(batch, translations):
                if new_zh and new_zh.strip():
                    clean_zh = _strip_numbered_prefix(new_zh) if re.match(r"^\[\d+\]", new_zh) else new_zh
                    original_zh = item["text_zh"]
                    # 只有确实变长了才采纳
                    if len(clean_zh) <= len(original_zh):
                        continue
                    # 防止过度扩展：短原文允许更大倍率（≤10字→3x，≤20字→2.5x，>20字→2x）
                    max_ratio = 3.0 if len(original_zh) <= 10 else (2.5 if len(original_zh) <= 20 else 2.0)
                    if len(clean_zh) > len(original_zh) * max_ratio:
                        print(f"       ⚠️  #{item['idx']} 扩展过长 ({len(clean_zh)}/{len(original_zh)}字，上限{max_ratio:.1f}x)，跳过")
                        continue
                    # 检查是否与相邻段重复
                    if _is_duplicate_of_neighbors(clean_zh, item["idx"], expanded):
                        print(f"       ⚠️  #{item['idx']} 扩展结果与相邻段重复，跳过")
                        continue
                    # 关键：检查扩展结果与原译文的语义忠实度（防止 LLM 跨段混淆）
                    if not _check_refine_fidelity(original_zh, clean_zh, min_overlap=0.3):
                        print(f"       ⚠️  #{item['idx']} 扩展结果偏离原译文过大，跳过")
                        continue
                    expanded[item["idx"]]["text_zh"] = clean_zh
        except Exception as e:
            print(f"     ⚠️  LLM 扩展批次 {i//batch_size+1} 失败: {e}")

    return expanded


def clean_iterations(output_dir: Path):
    """清理迭代中间数据，恢复到初始翻译状态"""
    iter_dir = _audit_dir(output_dir) / "iterations"
    tts_dir = output_dir / "tts_segments"
    cleaned = []

    # 恢复初始翻译
    init_snap = iter_dir / "iter_0_segments.json" if iter_dir.exists() else None
    if init_snap and init_snap.exists():
        cache = output_dir / "segments_cache.json"
        shutil.copy2(str(init_snap), str(cache))
        cleaned.append("segments_cache.json (已恢复初始翻译)")

    # 清理迭代快照
    if iter_dir.exists():
        n = len(list(iter_dir.iterdir()))
        shutil.rmtree(iter_dir)
        cleaned.append(f"iterations/ ({n} 个文件)")

    # 清理 TTS 缓存（可能对应已精简后的翻译）
    if tts_dir.exists():
        shutil.rmtree(tts_dir)
        cleaned.append("tts_segments/")

    if cleaned:
        print(f"  🗑  已清理: {', '.join(cleaned)}")
        print(f"     下次运行将从初始翻译重新开始")
    else:
        print(f"  ℹ️  无迭代数据需清理")


# ─── Step 7: 合成视频 ─────────────────────────────────────────────
def merge_final_video(video_path: Path, dub_path: Path,
                      output_dir: Path, volume: float = 0.15,
                      audio_sep_config: dict = None) -> Path:
    """合成最终视频

    当 audio_sep_config 启用时，使用分离后的人声/伴奏轨独立混音；
    否则回退到原始音频整体降音量的方式。
    """
    print(f"  🎬 合成最终视频...")
    final_path = output_dir / "final.mp4"

    sep_cfg = audio_sep_config or {}
    vocals_path = output_dir / "audio_vocals.wav"
    accomp_path = output_dir / "audio_accompaniment.wav"
    use_separation = (sep_cfg.get("enabled", False)
                      and vocals_path.exists()
                      and accomp_path.exists())

    if use_separation:
        vocal_vol = sep_cfg.get("vocal_volume", 0.0)
        bgm_vol = sep_cfg.get("bgm_volume", 1.0)
        print(f"  🎛  使用人声/伴奏分离混音 (vocal={vocal_vol}, bgm={bgm_vol})")
        # 输入: 0=video, 1=vocals, 2=accompaniment, 3=dub
        subprocess.run([
            "ffmpeg",
            "-i", str(video_path),
            "-i", str(vocals_path),
            "-i", str(accomp_path),
            "-i", str(dub_path),
            "-filter_complex",
            f"[1:a]volume={vocal_vol},aresample=44100[voc];"
            f"[2:a]volume={bgm_vol},aresample=44100[bgm];"
            f"[3:a]aresample=44100,loudnorm=I=-16:TP=-1.5:LRA=11[dub];"
            f"[voc][bgm][dub]amix=inputs=3:duration=first"
            f":dropout_transition=2[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(final_path), "-y"
        ], capture_output=True, check=True)
    else:
        if sep_cfg.get("enabled", False):
            print(f"  ⚠️  音频分离已启用但分离文件不存在，回退到整体降音量模式")
        subprocess.run([
            "ffmpeg",
            "-i", str(video_path), "-i", str(dub_path),
            "-filter_complex",
            f"[0:a]volume={volume}[bg];[1:a]aresample=44100,loudnorm=I=-16:TP=-1.5:LRA=11[dub];"
            f"[bg][dub]amix=inputs=2:duration=first:dropout_transition=2[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(final_path), "-y"
        ], capture_output=True, check=True)

    print(f"  ✅ 最终视频: {final_path}")

    # ── Video Slowdown 提示 ──
    slowdown_path = _audit_dir(output_dir) / "slowdown_segments.json"
    if slowdown_path.exists():
        with open(slowdown_path, "r", encoding="utf-8") as f:
            slowdown_segs = json.load(f)
        if slowdown_segs:
            print(f"  ⚠️  {len(slowdown_segs)} 段标记需要视频减速 (factor: "
                  f"{min(s['factor'] for s in slowdown_segs):.2f}~{max(s['factor'] for s in slowdown_segs):.2f})，"
                  f"当前版本暂未应用 setpts 减速滤镜")

    return final_path


# ─── 主流程 ────────────────────────────────────────────────────────
async def process_video(config: dict):
    """端到端处理"""
    global _logger
    t_start = time.time()
    skip = set(config.get("skip_steps", []))
    refine_enabled = config.get("refine", {}).get("enabled", False)
    sep_enabled = config.get("audio_separation", {}).get("enabled", False)
    base_steps = 7
    if refine_enabled:
        base_steps += 1
    if sep_enabled:
        base_steps += 1
    total_steps = base_steps

    # 确定输出目录
    if config["resume_from"]:
        output_dir = Path(config["resume_from"])
        if not output_dir.exists():
            print(f"❌ 目录不存在: {output_dir}")
            sys.exit(1)
        video_id = output_dir.name
        url = config.get("url", "")
        _migrate_audit_files(output_dir)
        print(f"\n{'='*60}")
        print(f"🔄 从已有目录恢复: {output_dir}")
    else:
        url = config["url"]
        video_id = extract_video_id(url) or _url_hash(url)
        output_dir = Path(config["output"]) / video_id
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n{'='*60}")
        print(f"🎯 处理视频: {url}")

    # 初始化日志
    _logger = PipelineLogger(output_dir)
    _log(f"   输出目录: {output_dir}")
    _log(f"   翻译引擎: {config['translator']}"
         + (f" ({config['llm']['model']})" if config['translator'] == 'llm' else ""))
    _log(f"   配音语音: {config['voice']}")
    _log(f"   Whisper:  {config['whisper_model']}")
    if refine_enabled:
        rcfg = config["refine"]
        _log(f"   迭代优化: {rcfg['max_iterations']} 轮 (阈值 >{rcfg['speed_threshold']}x)")
    if skip:
        _log(f"   跳过步骤: {', '.join(sorted(skip))}")
    _log(f"{'='*60}\n")

    # 记录完整配置到日志文件（脱敏）
    safe_config = {k: ("***" if "key" in k.lower() else v)
                   for k, v in config.items() if k != "llm"}
    if "llm" in config:
        safe_config["llm"] = {k: ("***" if "key" in k.lower() else v)
                              for k, v in config["llm"].items()}
    _logger.log(f"配置: {json.dumps(safe_config, ensure_ascii=False, indent=2)}",
                also_print=False)

    # ── 清理迭代数据 ──
    if config.get("clean_iterations"):
        clean_iterations(output_dir)
        print()

    cache_file = output_dir / "segments_cache.json"

    # ── 步骤计数器（因为 separate 步骤可选，后续编号动态偏移）──
    _step = 0

    def _next_step():
        nonlocal _step
        _step += 1
        return _step

    # Step 1: 下载
    _logger.step_begin("下载视频")
    video_path = output_dir / "original.mp4"
    title = "unknown"
    step_n = _next_step()
    if "download" not in skip:
        _log(f"[{step_n}/{total_steps}] 下载视频")
        if config["resume_from"] and video_path.exists():
            _log(f"  ⏭  使用已有视频")
            info_file = output_dir / "info.json"
            if info_file.exists():
                title = json.load(open(info_file))["title"]
        else:
            try:
                video_path, title = download_video(
                    url, output_dir, config["browser"],
                    config.get("download_quality", "best"))
            except Exception as e:
                _logger.log_error(
                    "下载失败", f"无法下载视频: {url}",
                    "1. 检查网络连接\n"
                    "2. 确认 URL 是有效的 YouTube 链接\n"
                    "3. 尝试: venv/bin/yt-dlp --cookies-from-browser chrome -F \"URL\" 查看可用格式\n"
                    "4. 如果报 'n challenge' 错误: pip install -U \"yt-dlp[default]\"\n"
                    "5. 如果视频有地区限制，尝试使用代理",
                    exception=e)
                _logger.close()
                return
        config["video_title"] = title
        _log("")
    elif not video_path.exists():
        _log(f"[{step_n}/{total_steps}] 下载视频 - 跳过")
        _log(f"  ⚠️  视频文件不存在: {video_path}")
        _log(f"     从 skip_steps 移除 'download' 或手动放置视频文件")
        _log("")
    _logger.step_end()

    # Step 2: 提取音频
    _logger.step_begin("提取音频")
    audio_path = output_dir / "audio.wav"
    step_n = _next_step()
    if "extract" not in skip:
        _log(f"[{step_n}/{total_steps}] 提取音频")
        if not video_path.exists():
            _logger.log_error(
                "前置条件缺失", f"视频文件不存在，无法提取音频: {video_path}",
                "1. 从 skip_steps 中移除 'download'，让 pipeline 先下载视频\n"
                f"2. 或手动将视频文件放到: {video_path}")
            _logger.close()
            return
        try:
            audio_path = extract_audio(video_path, output_dir)
        except Exception as e:
            _logger.log_error(
                "音频提取失败", f"ffmpeg 提取音频失败",
                "1. 确认 ffmpeg 已安装: which ffmpeg\n"
                f"2. 检查视频文件是否完整: file {video_path}",
                exception=e)
            _logger.close()
            return
        _log("")
    elif not audio_path.exists():
        _log(f"[{step_n}/{total_steps}] 提取音频 - 跳过")
        _log(f"  ⚠️  音频文件不存在: {audio_path}")
        _log("")
    _logger.step_end()

    # Step 2.5: 音频分离（可选）
    if sep_enabled:
        _logger.step_begin("音频分离")
        step_n = _next_step()
        if "separate" not in skip:
            _log(f"[{step_n}/{total_steps}] 人声/背景音分离")
            if not video_path.exists():
                _logger.log_error(
                    "前置条件缺失",
                    f"视频文件不存在，无法进行音频分离: {video_path}",
                    "确保 download 步骤已执行或视频文件已存在")
                _logger.close()
                return
            try:
                sep_result = separate_audio(
                    video_path, output_dir,
                    config.get("audio_separation", {}),
                    cpu_threads=config.get("cpu_threads", 0))
            except Exception as e:
                _logger.log_error(
                    "音频分离失败",
                    f"demucs 分离失败",
                    "1. 确认 demucs 已安装: pip install demucs\n"
                    "2. 首次运行需下载模型（~300MB），确保网络畅通\n"
                    "3. 如 GPU 内存不足，可设置 \"device\": \"cpu\"\n"
                    "4. 关闭此功能: \"audio_separation\": {\"enabled\": false}",
                    exception=e)
                _logger.close()
                return
            _log("")
        else:
            _log(f"[{step_n}/{total_steps}] 音频分离 - 跳过")
            vocals_path = output_dir / "audio_vocals.wav"
            accomp_path = output_dir / "audio_accompaniment.wav"
            if not vocals_path.exists() or not accomp_path.exists():
                _log(f"  ⚠️  分离文件不存在，合成阶段将回退到整体降音量模式")
            else:
                _log(f"  ⏭  使用已有分离结果")
            _log("")
        _logger.step_end()

    # Step 3+4: 转录 + 翻译
    _logger.step_begin("转录+翻译")
    transcribe_cache = output_dir / "transcribe_cache.json"  # 转录中间结果（纯英文）
    step_transcribe = _next_step()
    step_translate = _next_step()
    if cache_file.exists():
        _log(f"[{step_transcribe}/{total_steps}] 语音识别" + (" - 跳过" if "transcribe" in skip else ""))
        _log(f"[{step_translate}/{total_steps}] 翻译" + (" - 跳过" if "translate" in skip else ""))
        _log("  ⏭  使用缓存 (segments_cache.json)")
        with open(cache_file, "r", encoding="utf-8") as f:
            segments = json.load(f)
        _log(f"     {len(segments)} 个片段已加载")
        _log("")
    else:
        raw_segments = []
        if "transcribe" not in skip:
            _log(f"[{step_transcribe}/{total_steps}] 语音识别 (Whisper)")
            if not audio_path.exists():
                _logger.log_error(
                    "前置条件缺失", f"音频文件不存在: {audio_path}",
                    "1. 从 skip_steps 中移除 'download' 和 'extract'\n"
                    "2. 或手动准备音频文件")
                _logger.close()
                return
            try:
                raw_segments = transcribe_audio(
                    output_dir / "audio.wav", config["whisper_model"],
                    config.get("whisper_beam_size", 5),
                    cpu_threads=config.get("cpu_threads", 0))
                raw_segments = deduplicate_segments(raw_segments)
                # ── NLP 分句优化（可选） ──
                if config.get("nlp_segmentation", False):
                    raw_segments = _nlp_resegment(raw_segments)
                # ── 保存转录中间结果（支持单独跳过 transcribe） ──
                with open(transcribe_cache, "w", encoding="utf-8") as f:
                    json.dump(raw_segments, f, ensure_ascii=False, indent=2)
                _log(f"     转录缓存已保存: {transcribe_cache}")
            except Exception as e:
                _logger.log_error(
                    "语音识别失败", f"Whisper 转录失败 (model={config['whisper_model']})",
                    "1. 确认 faster-whisper 已安装: pip install faster-whisper\n"
                    "2. 检查音频文件是否完整\n"
                    "3. 尝试更小的模型: --whisper-model tiny",
                    exception=e)
                _logger.close()
                return
            _log("")
        else:
            _log(f"[{step_transcribe}/{total_steps}] 语音识别 - 跳过")
            # ── 从转录缓存加载（支持单独跳过 transcribe） ──
            if transcribe_cache.exists():
                with open(transcribe_cache, "r", encoding="utf-8") as f:
                    raw_segments = json.load(f)
                _log(f"  ⏭  使用转录缓存 ({len(raw_segments)} 段)")
            else:
                _logger.log_error(
                    "前置条件缺失",
                    f"transcribe 被跳过但转录缓存不存在: {transcribe_cache}",
                    "skip_steps 包含 'transcribe' 时，需要之前运行产生的转录缓存文件。\n\n"
                    "方案 A: 从 skip_steps 中移除 'transcribe'，让 pipeline 执行 Whisper 转录:\n"
                    f'  修改 config 中 "skip_steps" 为: {json.dumps([s for s in config.get("skip_steps", []) if s != "transcribe"])}\n\n'
                    "方案 B: 确认之前运行已完成转录:\n"
                    f"  检查: ls {transcribe_cache}")
                _logger.close()
                return
            _log("")

        if "translate" not in skip:
            if not raw_segments:
                _logger.log_error(
                    "前置条件缺失",
                    "翻译需要转录结果，但转录数据为空",
                    "检查 Whisper 是否正确识别了语音内容，或手动检查音频文件")
                _logger.close()
                return
            _log(f"[{step_translate}/{total_steps}] 翻译")
            try:
                segments = translate_segments(raw_segments, config)
                segments = deduplicate_segments(segments)
                segments = merge_short_segments(segments)
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(segments, f, ensure_ascii=False, indent=2)
            except Exception as e:
                _logger.log_error(
                    "翻译失败",
                    f"翻译引擎 '{config['translator']}' 调用失败",
                    ("1. 检查 LLM API Key 是否有效\n"
                     f"2. 检查 API 地址是否可达: {config.get('llm', {}).get('api_url', 'N/A')}\n"
                     "3. 检查网络连接\n"
                     "4. 尝试切换翻译引擎: --translator google")
                    if config['translator'] == 'llm' else
                    ("1. 检查网络连接\n"
                     "2. Google 翻译可能被墙，尝试: --translator llm"),
                    exception=e)
                _logger.close()
                return
            _log("")
        else:
            _log(f"[{step_translate}/{total_steps}] 翻译 - 跳过")
            segments = []
            _log("")
    _logger.step_end()

    # ──────────── 前置条件检查 ────────────
    if not segments:
        missing = []
        fixes = []
        if "download" in skip and not video_path.exists():
            missing.append(f"skip_steps 包含 'download' 但视频不存在: {video_path}")
        if "extract" in skip and not (output_dir / "audio.wav").exists():
            missing.append(f"skip_steps 包含 'extract' 但音频不存在: {output_dir / 'audio.wav'}")
        if "transcribe" in skip and not cache_file.exists() and not transcribe_cache.exists():
            missing.append(f"skip_steps 包含 'transcribe' 但转录缓存不存在: {transcribe_cache}")
        if "translate" in skip and not cache_file.exists():
            missing.append(f"skip_steps 包含 'translate' 但翻译缓存不存在: {cache_file}")

        if missing:
            # 生成可直接使用的修复配置
            fix_config = dict(config)
            fix_config.pop("skip_steps", None)
            fix_skip = [s for s in config.get("skip_steps", [])
                        if s not in ("download", "extract", "transcribe", "translate")]
            suggestion_lines = [
                "这是配置问题: skip_steps 跳过了必要步骤但产出文件不存在。",
                "",
                "方案 A: 移除 skip_steps 中的这些项，让 pipeline 从头执行:",
                f'  修改 config.json 中 "skip_steps" 为: {json.dumps(fix_skip) if fix_skip else "删除此字段"}',
                "",
                "方案 B: 如果你想从之前的运行恢复，确保 segments_cache.json 存在:",
                f"  检查: ls {cache_file}",
            ]
            _logger.log_error(
                "配置错误",
                "segments 为空，skip_steps 跳过了关键步骤但产出文件不存在\n"
                + "\n".join(f"  - {m}" for m in missing),
                "\n".join(suggestion_lines))
            _logger.close()
            return
        else:
            _log(f"\n  ⚠️  未识别到任何语音片段，跳过后续处理\n")
            _logger.close()
            return

    # ──────────── 分支: 标准 vs 迭代优化 ────────────
    if refine_enabled and segments:
        # ── 迭代优化流程 ──

        # 迭代优化翻译（纯文本，基于字符数估算，不需要 TTS）
        _logger.step_begin("迭代优化")
        step_n = _next_step()
        if "refine" not in skip:
            _log(f"[{step_n}/{total_steps}] 迭代优化翻译（字符估算）")
            segments = await run_refinement_loop(segments, output_dir, config)
            _log("")
        _logger.step_end()

        # 翻译定稿后一次性生成 TTS
        _logger.step_begin("生成TTS")
        tts_dir = output_dir / "tts_segments"
        step_n = _next_step()
        if "tts" not in skip:
            _log(f"[{step_n}/{total_steps}] 生成 TTS 片段")
            _log(f"  🗣  voice={config['voice']}, 并发={config.get('tts_concurrency', 5)}")
            # 清除旧的 TTS 缓存（翻译已变更，旧文件内容可能不匹配）
            # 但如果存在 tts_failure.json（断点恢复场景），保留已有文件，只重试失败片段
            failure_json_path = _audit_dir(output_dir) / "tts_failure.json"
            if tts_dir.exists() and not failure_json_path.exists():
                for f in tts_dir.iterdir():
                    f.unlink()
            elif failure_json_path.exists():
                _log(f"  📋 检测到断点恢复文件，保留已有 TTS 缓存")
            await _generate_tts_segments(
                segments, tts_dir, config["voice"],
                config.get("tts_concurrency", 5),
                config=config)
            _log("")
        else:
            _log(f"[{step_n}/{total_steps}] 生成 TTS - 跳过")
            tts_files = list(tts_dir.glob("seg_*.mp3")) if tts_dir.exists() else []
            if not tts_files:
                _logger.log_error(
                    "前置条件缺失",
                    f"tts 被跳过但 TTS 片段不存在: {tts_dir}",
                    "skip_steps 包含 'tts' 时，需要之前运行已生成 TTS 音频片段。\n\n"
                    f"方案: 从 skip_steps 中移除 'tts':\n"
                    f'  修改 config 中 "skip_steps" 为: {json.dumps([s for s in config.get("skip_steps", []) if s != "tts"])}')
                _logger.close()
                return
            _log(f"  ⏭  使用已有 TTS 片段 ({len(tts_files)} 个)")
            _log("")
        _logger.step_end()

        # ── TTS 后校准（可选）──
        if config.get("refine", {}).get("post_tts_calibration", False) and tts_dir.exists():
            segments = await _post_tts_calibrate(segments, tts_dir, config)
            # 更新 cache
            with open(output_dir / "segments_cache.json", "w", encoding="utf-8") as f:
                json.dump(segments, f, ensure_ascii=False, indent=2)

        # 字幕 + 时间线对齐
        _logger.step_begin("字幕+对齐")
        step_n = _next_step()
        if segments:
            _log(f"[{step_n}/{total_steps}] 生成字幕 + 时间线对齐")
            if "subtitle" not in skip:
                generate_srt_files(segments, output_dir)
            dub_path = _align_tts_to_timeline(segments, output_dir,
                                                cpu_threads=config.get("cpu_threads", 0),
                                                global_speed=config.get("global_speed", 1.0))
            _log("")
        _logger.step_end()

        # 合成
        _logger.step_begin("合成视频")
        dub_path = output_dir / "chinese_dub.wav"
        step_n = _next_step()
        if "merge" not in skip:
            if not dub_path.exists():
                _logger.log_error(
                    "前置条件缺失",
                    f"配音文件不存在: {dub_path}",
                    "合成视频需要 chinese_dub.wav，但该文件未生成。\n"
                    "确认 tts 步骤未被跳过，或之前运行已生成该文件。")
                _logger.close()
                return
            _log(f"[{step_n}/{total_steps}] 合成最终视频")
            final_path = merge_final_video(
                video_path, dub_path, output_dir, config["volume"],
                audio_sep_config=config.get("audio_separation"))
            _log("")
        else:
            _log(f"[{step_n}/{total_steps}] 合成视频 - 跳过")
            _log("")
        _logger.step_end()
    else:
        # ── 标准流程 ──

        # 字幕
        _logger.step_begin("生成字幕")
        step_n = _next_step()
        if "subtitle" not in skip and segments:
            _log(f"[{step_n}/{total_steps}] 生成字幕")
            srt_en, srt_zh, srt_bi = generate_srt_files(segments, output_dir)
            _log("")
        _logger.step_end()

        # 配音
        _logger.step_begin("生成配音")
        step_n = _next_step()
        if "tts" not in skip and segments:
            _log(f"[{step_n}/{total_steps}] 生成中文配音")
            dub_path = await generate_chinese_dub(
                segments, output_dir, config["voice"],
                config.get("tts_concurrency", 5),
                config=config)
            _log("")
        elif "tts" in skip:
            _log(f"[{step_n}/{total_steps}] 生成配音 - 跳过")
            tts_dir = output_dir / "tts_segments"
            tts_files = list(tts_dir.glob("seg_*.mp3")) if tts_dir.exists() else []
            if not tts_files:
                _logger.log_error(
                    "前置条件缺失",
                    f"tts 被跳过但 TTS 片段不存在: {tts_dir}",
                    "skip_steps 包含 'tts' 时，需要之前运行已生成 TTS 音频片段。\n\n"
                    f"方案: 从 skip_steps 中移除 'tts':\n"
                    f'  修改 config 中 "skip_steps" 为: {json.dumps([s for s in config.get("skip_steps", []) if s != "tts"])}')
                _logger.close()
                return
            _log(f"  ⏭  使用已有 TTS 片段 ({len(tts_files)} 个)")
            _log("")
        _logger.step_end()

        # ── TTS 后校准（可选，标准流程）──
        if config.get("refine", {}).get("post_tts_calibration", False) and segments:
            _tts_dir = output_dir / "tts_segments"
            if _tts_dir.exists():
                segments = await _post_tts_calibrate(segments, _tts_dir, config)
                with open(output_dir / "segments_cache.json", "w", encoding="utf-8") as f:
                    json.dump(segments, f, ensure_ascii=False, indent=2)

        # 合成
        _logger.step_begin("合成视频")
        dub_path = output_dir / "chinese_dub.wav"
        step_n = _next_step()
        if "merge" not in skip:
            if not dub_path.exists():
                _logger.log_error(
                    "前置条件缺失",
                    f"配音文件不存在: {dub_path}",
                    "合成视频需要 chinese_dub.wav，但该文件未生成。\n"
                    "确认 tts 步骤未被跳过，或之前运行已生成该文件。")
                _logger.close()
                return
            _log(f"[{step_n}/{total_steps}] 合成最终视频")
            final_path = merge_final_video(
                video_path, dub_path, output_dir, config["volume"],
                audio_sep_config=config.get("audio_separation"))
            _log("")
        else:
            _log(f"[{step_n}/{total_steps}] 合成视频 - 跳过")
            _log("")
        _logger.step_end()

    # ── 重命名输出目录 ──
    final_dir = output_dir
    if config.get("rename"):
        new_name = config["rename"]
        new_dir = output_dir.parent / new_name
        if new_dir.exists():
            _log(f"  ⚠️  目标目录已存在，跳过重命名: {new_dir}")
        else:
            output_dir.rename(new_dir)
            final_dir = new_dir
            _log(f"  📁 已重命名: {output_dir.name} → {new_name}")

    elapsed = time.time() - t_start
    _log(f"\n{'='*60}")
    _log(f"🎉 处理完成! (耗时 {elapsed:.0f}s)")
    _log(f"   输出目录: {final_dir}")
    _log(f"   最终视频: {final_dir}/final.mp4")
    _log(f"   双语字幕: {final_dir}/subtitle_bilingual.srt")
    if _logger and _logger.log_path:
        _log(f"   执行日志: {_logger.log_path}")
    _log(f"{'='*60}")

    if _logger:
        _logger.close()


def _url_hash(url: str) -> str:
    import hashlib
    return hashlib.md5(url.encode()).hexdigest()[:11]


# ─── CLI ───────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="YouTube 英文视频 → 中文配音 + 双语字幕 (v3)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 基础用法
  python pipeline.py "https://www.youtube.com/watch?v=XXXX"
  python pipeline.py --config config.json

  # LLM 翻译 + 3 轮迭代优化
  python pipeline.py "URL" --translator llm --llm-api-key sk-xxx --refine 3

  # 从第 2 轮迭代恢复
  python pipeline.py --resume-from output/VIDEO_ID --refine 5 --resume-iteration 2

  # 清理迭代数据重新开始
  python pipeline.py --resume-from output/VIDEO_ID --clean-iterations --refine 3

可用中文语音 (edge-tts):
  zh-CN-YunxiNeural     男声 (默认)
  zh-CN-YunyangNeural   男声 (播报)
  zh-CN-XiaoxiaoNeural  女声 (温暖)
  zh-CN-XiaoyiNeural    女声 (活泼)
        """,
    )
    parser.add_argument("url", nargs="?", default=None, help="YouTube 视频 URL")
    parser.add_argument("--config", "-c", default=None, help="JSON 配置文件路径")
    parser.add_argument("--output", "-o", default=None, help="输出根目录 (默认: output)")
    parser.add_argument("--voice", "-v", default=None, help="TTS 语音")
    parser.add_argument("--whisper-model", "-m", default=None, dest="whisper_model",
                        choices=["tiny", "base", "small", "medium"], help="Whisper 模型")
    parser.add_argument("--volume", type=float, default=None, help="原声背景音量 0.0-1.0")
    parser.add_argument("--browser", "-b", default=None, help="读取 cookies 的浏览器")
    parser.add_argument("--rename", default=None, help="完成后重命名输出目录")
    parser.add_argument("--resume-from", default=None, dest="resume_from",
                        help="从已有输出目录恢复（用于调试中间步骤）")

    # 翻译引擎
    parser.add_argument("--translator", default=None, choices=["google", "llm"],
                        help="翻译引擎: google (免费) 或 llm (大模型)")
    parser.add_argument("--llm-api-url", default=None, dest="llm_api_url",
                        help="LLM API 地址 (OpenAI 兼容)")
    parser.add_argument("--llm-api-key", default=None, dest="llm_api_key",
                        help="LLM API Key")
    parser.add_argument("--llm-model", default=None, dest="llm_model",
                        help="LLM 模型名")

    # 性能
    parser.add_argument("--tts-concurrency", type=int, default=None, dest="tts_concurrency",
                        help="TTS 并发数 (默认: 5)")

    # 迭代优化
    parser.add_argument("--refine", type=int, default=None, metavar="N",
                        help="启用迭代优化，N=最大轮次 (需 LLM api_key)")
    parser.add_argument("--refine-threshold", type=float, default=None,
                        dest="refine_threshold",
                        help="加速倍率阈值，超过则触发精简 (默认: 1.5)")
    parser.add_argument("--resume-iteration", type=int, default=None,
                        dest="resume_iteration",
                        help="从第 N 轮迭代恢复 (配合 --refine)")
    parser.add_argument("--clean-iterations", action="store_true", default=False,
                        dest="clean_iterations",
                        help="清理迭代中间数据，恢复初始翻译")

    args = parser.parse_args()
    config = load_config(args)

    check_dependencies(config)
    asyncio.run(process_video(config))


if __name__ == "__main__":
    try:
        main()
    finally:
        # 清理 multiprocessing resource_tracker 守护进程
        # ctranslate2/PyTorch 内部会 fork+exec 出 resource_tracker，
        # pipeline.py 退出后它会残留在进程列表中。
        try:
            import multiprocessing.resource_tracker as _rt
            _tracker_pid = getattr(_rt._resource_tracker, '_pid', None)
            if _tracker_pid:
                import signal
                os.kill(_tracker_pid, signal.SIGKILL)
        except (ProcessLookupError, OSError, AttributeError):
            pass
