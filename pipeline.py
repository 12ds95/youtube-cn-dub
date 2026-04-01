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
        self.log_path = output_dir / f"pipeline_{ts}.log"
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
            "2)保持技术术语的专业性；"
            "3)翻译要适合做视频配音朗读，语句通顺自然；"
            "4)只输出翻译结果，不要解释。"
        ),
        "batch_size": 15,            # 每批翻译的句子数（过大可能导致对齐问题）
        "temperature": 0.3,          # 生成温度: 0.0=确定性 / 0.3=推荐 / 1.0=多样性
        "style": "",                 # 翻译风格: ""(默认) / "口语化" / "正式" / "学术" 等
    },

    # ── TTS 配音引擎 ──
    #   tts_chain: 引擎优先级链（推荐方式），第一个为主引擎，后面按顺序整体回退
    #   也可用旧方式: tts_engine(主) + tts_fallback(回退列表)
    #   各引擎有独立语音配置（voice 字段仅影响 edge-tts），详见各引擎 resolve_voice()
    #   引擎分类:
    #     远程: edge-tts(免费), gtts(免费), siliconflow(免费额度)
    #     本地: pyttsx3(零依赖), piper(需下载~70MB), sherpa-onnx(需下载~110MB), cosyvoice(需GPU)
    "tts_chain": None,               # 引擎优先链: 如 ["edge-tts", "gtts", "pyttsx3"]
                                     # 为 null 时使用 tts_engine + tts_fallback 组合
    "tts_engine": "edge-tts",        # 主 TTS 引擎（tts_chain 为空时生效）
    "tts_fallback": [],              # 回退引擎列表（tts_chain 为空时生效）

    # 各 TTS 引擎专属配置（仅使用对应引擎时需要）
    "siliconflow": {                 # 硅基流动 CosyVoice2（注册 https://cloud.siliconflow.cn 送额度）
        "api_key": "",               # 硅基流动 API Key
        "model": "FunAudioLLM/CosyVoice2-0.5B",   # 模型 ID
        "voice": "FunAudioLLM/CosyVoice2-0.5B:alex",  # 音色: alex / benjamin / charles / cosmo
    },
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
    "cosyvoice": {                   # CosyVoice 阿里开源 TTS（需 GPU + 本地部署）
        "model_path": None,          # 模型路径: 如 "CosyVoice-300M"
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
    if shutil.which("node"):
        return
    import glob
    candidates = glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin"))
    candidates += ["/usr/local/bin", "/opt/homebrew/bin"]
    for path in candidates:
        if os.path.isfile(os.path.join(path, "node")):
            os.environ["PATH"] = path + ":" + os.environ.get("PATH", "")
            return
    print("  ⚠️  未找到 Node.js，YouTube 下载可能失败。请安装: brew install node")


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

    # 根据 download_quality 构建 format 选择器
    # "best" → 不限分辨率，优先最高帧率+最高画质
    # "1080p"/"720p"/"480p" → 限高但优先最高帧率
    quality = download_quality.lower().strip()
    if quality == "best":
        fmt = "bestvideo+bestaudio/best"
        print(f"  🎬 画质: 最高可用 (不限分辨率/帧率)")
    else:
        height = int(quality.replace("p", "")) if quality.replace("p", "").isdigit() else 720
        fmt = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
        print(f"  🎬 画质: ≤{height}p (最高帧率)")

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
                   config: dict = None) -> dict:
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
    demucs_script = f'''
import torch
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
print("DEMUCS_OK")
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
                     beam_size: int = 5) -> List[dict]:
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

    model = WhisperModel(model_path, device="cpu", compute_type="int8")
    segments_raw, info = model.transcribe(
        str(audio_path), language="en", beam_size=beam_size,
        word_timestamps=True, vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500, speech_pad_ms=200),
    )
    segments = [{"start": s.start, "end": s.end, "text": s.text.strip()}
                for s in segments_raw]
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


# ─── Step 4: 翻译 ──────────────────────────────────────────────────

def translate_segments(segments: List[dict], config: dict) -> List[dict]:
    """根据配置选择翻译引擎"""
    engine = config.get("translator", "google")
    if engine == "llm":
        return _translate_llm(segments, config["llm"], config.get("video_title", ""))
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
                               temperature: float) -> str:
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
        return result

    except Exception as e:
        print(f"     ⚠️  主题识别失败 ({e})，使用通用翻译规则")
        return _default_translation_rules()


def _default_translation_rules() -> str:
    """通用翻译保护规则，无论主题识别是否成功都会注入"""
    return (
        "\n通用翻译规则（始终遵守）:"
        "\n  - 数学符号（i, e, π, θ 等）在数学/科学语境中保持为专业术语，不可翻译为日常用语（如 i→'我'）"
        "\n  - 负号'-'在数学/科学语境中必须翻译为'负'，不可省略（字幕'-3'应读作'负三'）"
        "\n  - 英文倒装句（there be, 状语前置等）翻译时需调整为中文习惯语序"
        "\n  - 翻译结果用于语音配音朗读，需通顺自然，适合听觉理解"
        "\n  - 前后文语义连贯，避免相邻段之间出现语义断裂或内容重复"
    )


def _translate_llm(segments: List[dict], llm_config: dict, video_title: str = "") -> List[dict]:
    """LLM 大模型翻译引擎 (OpenAI 兼容 API)"""
    import httpx

    api_url = llm_config["api_url"].rstrip("/")
    api_key = llm_config["api_key"]
    model = llm_config["model"]
    system_prompt = llm_config.get("system_prompt", "将英文翻译为中文，只输出翻译结果。")
    batch_size = llm_config.get("batch_size", 15)
    temperature = llm_config.get("temperature", 0.3)
    style = llm_config.get("style", "")
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
            segments, video_title, endpoint, headers, model, temperature
        )
        if topic_guide:
            system_prompt += f"\n{topic_guide}"
            print(f"     📋 自动识别翻译风格已注入")

    result = []
    prev_context = None
    for batch_idx, i in enumerate(range(0, len(segments), batch_size)):
        batch = segments[i:i + batch_size]
        # 构造批量翻译请求：每行一句，用编号标记
        lines = []
        for j, seg in enumerate(batch):
            lines.append(f"[{j+1}] {seg['text']}")
        user_msg = "\n".join(lines)

        # 构造上下文提示
        context_hint = ""
        if video_title:
            context_hint += f"视频主题：{video_title}\n"
        if prev_context:
            context_hint += f"前文：{'；'.join(prev_context)}\n"

        batch_prompt = (
            f"{system_prompt}\n\n"
            + (f"{context_hint}\n" if context_hint else "")
            + f"请翻译以下 {len(batch)} 句话，每句保持 [编号] 格式，"
            f"一行一句，不要合并或拆分：\n\n{user_msg}"
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

        for br in batch_results:
            result.append(br)

        # 保存最后两句作为下一批的上下文
        if batch_results:
            prev_context = [r["text_zh"] for r in batch_results[-2:]]

        print(f"     进度: {min(i+batch_size, len(segments))}/{len(segments)}")

    print(f"  ✅ LLM 翻译完成")
    return result


def _translate_llm_single(batch, endpoint, headers, model, system_prompt, temperature,
                          max_retries: int = 3):
    """逐条 LLM 翻译（降级方案），带重试"""
    import httpx
    results = []
    for seg in batch:
        zh = None
        for attempt in range(max_retries):
            try:
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": seg["text"]},
                    ],
                    "temperature": temperature,
                    "max_tokens": 512,
                }
                with httpx.Client(timeout=30.0) as client:
                    resp = client.post(endpoint, json=payload, headers=headers)
                    resp.raise_for_status()
                    zh = resp.json()["choices"][0]["message"]["content"].strip()
                    zh = _strip_think_block(zh)
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


def _strip_numbered_prefix(line: str) -> str:
    """去除行首的 [N] 或 N. 编号前缀"""
    cleaned = re.sub(r"^\[?\d+\]?\s*\.?\s*", "", line.strip())
    return cleaned.strip()


def _parse_numbered_translations(content: str, expected_count: int) -> List[str]:
    """解析 LLM 返回的编号格式翻译"""
    # 第一层：去除 <think> 推理块（qwen3-coder 等模型会输出）
    content = _strip_think_block(content)

    lines = content.strip().split("\n")
    translations = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 匹配 [1] 翻译内容 或 1. 翻译内容
        match = re.match(r"^\[(\d+)\]\s*(.+)$", line)
        if not match:
            match = re.match(r"^(\d+)\.\s*(.+)$", line)
        if match:
            translations.append(match.group(2).strip())
        elif translations:
            # 可能是上一行的续行
            translations[-1] += line

    # 第二层：如果解析数量不对，按行分割并同样去除编号前缀
    if len(translations) != expected_count:
        raw_lines = [l.strip() for l in content.strip().split("\n") if l.strip()]
        translations = [_strip_numbered_prefix(l) for l in raw_lines]

    # 第三层：最终安全检查，确保没有 [N] 前缀泄漏
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
            fen.write(f"{idx}\n{ts}\n{seg['text_en']}\n\n")
            fzh.write(f"{idx}\n{ts}\n{seg['text_zh']}\n\n")
            fbi.write(f"{idx}\n{ts}\n{seg['text_zh']}\n{seg['text_en']}\n\n")
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

    async def synthesize(self, text: str, path: str, voice: str):
        """将 text 合成为音频文件，保存到 path"""
        raise NotImplementedError

    async def synthesize_batch(self, items: List[dict], tts_dir: Path,
                               voice: str, concurrency: int = 5):
        """批量合成。items 是 [(idx, text_zh), ...]，默认实现逐个调用 synthesize
        遇到 TTSFatalError（认证/余额等不可恢复错误）时立即中止并向上传播。
        """
        resolved_voice = self.resolve_voice(voice)
        semaphore = asyncio.Semaphore(concurrency)
        fatal_error = None  # 记录首个致命错误

        async def _one(text, path):
            nonlocal fatal_error
            if fatal_error:
                return  # 已有致命错误，跳过后续
            async with semaphore:
                try:
                    await self.synthesize(text, path, resolved_voice)
                except TTSFatalError as e:
                    fatal_error = e
                    raise

        tasks = []
        for item in items:
            idx, text_zh = item["idx"], item["text_zh"]
            p = tts_dir / f"seg_{idx:04d}.mp3"
            tasks.append(_one(text_zh, str(p)))

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

    async def synthesize(self, text: str, path: str, voice: str):
        import edge_tts
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(path)


class GTTSEngine(TTSEngine):
    """gTTS: Google Translate TTS（免费，无需 API key）"""
    name = "gtts"
    is_local = False

    def resolve_voice(self, global_voice: str) -> str:
        """gTTS 使用语言代码 zh-cn，忽略全局 edge-tts voice"""
        return "zh-cn"

    async def synthesize(self, text: str, path: str, voice: str):
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

    async def synthesize(self, text: str, path: str, voice: str):
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

    async def synthesize(self, text: str, path: str, voice: str):
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


class CosyVoiceEngine(TTSEngine):
    """CosyVoice: 阿里开源 TTS（需 GPU，中文最佳音质）"""
    name = "cosyvoice"
    is_local = True

    def __init__(self, model_path: str = None):
        self.model_path = model_path
        self._model = None

    def _load_model(self):
        if self._model is None:
            from cosyvoice import CosyVoice
            self._model = CosyVoice(self.model_path or "CosyVoice-300M")
        return self._model

    def resolve_voice(self, global_voice: str) -> str:
        """CosyVoice 使用中文语音角色名，忽略全局 edge-tts voice"""
        return "中文女"

    async def synthesize(self, text: str, path: str, voice: str):
        loop = asyncio.get_event_loop()
        def _gen():
            model = self._load_model()
            output = model.inference_sft(text, voice)  # 已由 resolve_voice 转换
            import torchaudio
            torchaudio.save(path.replace(".mp3", ".wav"),
                            output["tts_speech"], 22050)
            from pydub import AudioSegment as PydubSegment
            wav_path = path.replace(".mp3", ".wav")
            PydubSegment.from_wav(wav_path).export(path, format="mp3")
            os.remove(wav_path)
        await loop.run_in_executor(None, _gen)


class SiliconFlowTTSEngine(TTSEngine):
    """SiliconFlow: 硅基流动云端 CosyVoice2（免费额度，OpenAI 兼容 API，中文音质最佳）
    注册即送额度: https://cloud.siliconflow.cn
    配置示例:
      "siliconflow": {
          "api_key": "sk-xxx",
          "model": "FunAudioLLM/CosyVoice2-0.5B",
          "voice": "FunAudioLLM/CosyVoice2-0.5B:alex"
      }
    """
    name = "siliconflow"
    is_local = False

    def __init__(self, api_key: str = None, model: str = None, voice_id: str = None):
        self.api_key = api_key or os.environ.get("SILICONFLOW_API_KEY", "")
        self.model = model or "FunAudioLLM/CosyVoice2-0.5B"
        self.voice_id = voice_id or "FunAudioLLM/CosyVoice2-0.5B:alex"

    def resolve_voice(self, global_voice: str) -> str:
        """SiliconFlow 使用自己的 voice_id，忽略全局 edge-tts voice"""
        return self.voice_id

    async def synthesize(self, text: str, path: str, voice: str):
        import httpx
        url = "https://api.siliconflow.cn/v1/audio/speech"
        payload = {
            "model": self.model,
            "input": text,
            "voice": voice,  # 已经由 resolve_voice 转换
            "response_format": "mp3",
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code in (401, 403):
                raise TTSFatalError(
                    f"SiliconFlow 认证/余额错误 ({resp.status_code}): {resp.text[:200]}")
            if resp.status_code != 200:
                raise RuntimeError(
                    f"SiliconFlow TTS 失败 ({resp.status_code}): {resp.text[:200]}")
            with open(path, "wb") as f:
                f.write(resp.content)


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

    async def synthesize(self, text: str, path: str, voice: str):
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
    "cosyvoice": CosyVoiceEngine,
    "siliconflow": SiliconFlowTTSEngine,
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
    if engine_name in ("piper", "cosyvoice"):
        return engine_cls(model_path=engine_config.get("model_path"))
    elif engine_name == "sherpa-onnx":
        return engine_cls(model_config=engine_config)
    elif engine_name == "siliconflow":
        return engine_cls(
            api_key=engine_config.get("api_key"),
            model=engine_config.get("model"),
            voice_id=engine_config.get("voice"),
        )
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
    failure_json = tts_dir.parent / "tts_failure.json"

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
    all_items = []
    skipped_placeholder = 0
    for idx, seg in enumerate(segments):
        text_zh = seg.get("text_zh", seg.get("text", ""))
        if len(text_zh.strip()) >= 2:
            # 防御：跳过纯标点/占位符文本（如 "---"、"..."），TTS 引擎无法合成
            if not re.search(r'[\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]', text_zh):
                skipped_placeholder += 1
                continue
            all_items.append({"idx": idx, "text_zh": text_zh})
    if skipped_placeholder:
        print(f"     ⚠️  跳过 {skipped_placeholder} 个无可发音内容的片段"
              f"（纯标点/占位符如 '---'）")

    if not all_items:
        print(f"     无需生成 TTS 片段")
        return

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
                    duration=min(target_ms, 500), frame_rate=16000)
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

        # 标点和停顿约占 10% 额外时间
        estimated_ms *= 1.1
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
      英文/数字: ~100ms/字符
    """
    import jieba
    import unicodedata

    words = jieba.lcut(text_zh)
    total_ms = 0.0
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
            total_ms += other_count * 100  # 英文/数字

    return total_ms


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


def _align_tts_to_timeline(segments: List[dict], output_dir: Path) -> Path:
    """阶段 C: atempo 调速 + 叠加拼接 → chinese_dub.wav

    语速策略:
      1. 收集每段 TTS 时长 / 目标时长 = raw_ratio
      2. 计算全局中位数基线，各段按 40% 向基线混合
      3. 指数平滑（α=0.3）消除相邻段跳变
      4. 钳制到 [SPEED_MIN, SPEED_MAX] 区间，保证听感自然
      5. 超出区间的片段：过短用静音居中填充，过长截断
    断点恢复: 调速后的 seg_XXXX_adj.wav 会被缓存，重跑时自动跳过
    """
    from pydub import AudioSegment as PydubSegment

    tts_dir = output_dir / "tts_segments"
    audio_path = output_dir / "audio.wav"
    original_audio = PydubSegment.from_wav(str(audio_path))
    total_ms = len(original_audio)

    # ── 语速目标区间 ──
    SPEED_MIN = 0.95   # 低于此值用静音填充而非极端降速
    SPEED_MAX = 1.25   # 高于此值截断而非极端加速（听起来太快）

    print(f"     时间线对齐中 (目标语速区间: {SPEED_MIN:.2f}x ~ {SPEED_MAX:.2f}x)...")
    final_audio = PydubSegment.silent(duration=total_ms, frame_rate=16000)
    stats = {"adjusted": 0, "skipped": 0, "padded": 0, "clamped_fast": 0, "clamped_slow": 0}

    # First pass: collect all raw speed ratios
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

    # Compute global baseline (median of valid ratios)
    valid_ratios = [r for r in raw_ratios if r is not None and r > 0]
    if valid_ratios:
        sorted_ratios = sorted(valid_ratios)
        median_ratio = sorted_ratios[len(sorted_ratios) // 2]
    else:
        median_ratio = 1.0

    # Blend toward median and apply smoothing
    BLEND_WEIGHT = 0.4  # 40% toward global median
    SMOOTH_ALPHA = 0.3  # exponential smoothing factor
    blended_ratios = []
    for r in raw_ratios:
        if r is None:
            blended_ratios.append(None)
        else:
            blended_ratios.append(r * (1 - BLEND_WEIGHT) + median_ratio * BLEND_WEIGHT)

    # Exponential smoothing pass
    smoothed_ratios = list(blended_ratios)
    prev_valid = None
    for i, r in enumerate(smoothed_ratios):
        if r is not None:
            if prev_valid is not None:
                smoothed_ratios[i] = SMOOTH_ALPHA * prev_valid + (1 - SMOOTH_ALPHA) * r
            prev_valid = smoothed_ratios[i]

    # 钳制到目标区间 [SPEED_MIN, SPEED_MAX]
    clamped_ratios = []
    for r in smoothed_ratios:
        if r is None:
            clamped_ratios.append(None)
        else:
            clamped_ratios.append(max(SPEED_MIN, min(SPEED_MAX, r)))

    # 统计钳制情况
    for i, (sm, cl) in enumerate(zip(smoothed_ratios, clamped_ratios)):
        if sm is not None and cl is not None:
            if sm > SPEED_MAX:
                stats["clamped_fast"] += 1
            elif sm < SPEED_MIN:
                stats["clamped_slow"] += 1

    # 计算钳制后的实际平均语速
    clamped_valid = [r for r in clamped_ratios if r is not None]
    avg_speed = sum(clamped_valid) / max(1, len(clamped_valid))
    print(f"     全局语速基线: {median_ratio:.2f}x → 钳制后平均: {avg_speed:.2f}x"
          f" (混合={BLEND_WEIGHT}, 平滑={SMOOTH_ALPHA})")
    if stats["clamped_fast"] or stats["clamped_slow"]:
        print(f"     钳制: {stats['clamped_fast']} 段过快被限速,"
              f" {stats['clamped_slow']} 段过慢被提速")

    # ── 保存调速报告（断点恢复时可查看） ──
    speed_report = {
        "median_ratio": round(median_ratio, 4),
        "avg_clamped": round(avg_speed, 4),
        "speed_range": [SPEED_MIN, SPEED_MAX],
        "total_segments": len(segments),
        "clamped_fast": stats["clamped_fast"],
        "clamped_slow": stats["clamped_slow"],
    }
    with open(output_dir / "speed_report.json", "w", encoding="utf-8") as f:
        json.dump(speed_report, f, ensure_ascii=False, indent=2)

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

        # 对于 TTS 远短于目标时长的片段：用钳制后的 ratio 调速 + 静音居中填充
        if raw_ratio < SPEED_MIN and len(tts_audio) > 0:
            # 用 speed_ratio（已钳制到 SPEED_MIN）做轻微降速
            if speed_ratio < 0.98:
                adjusted = tts_dir / f"seg_{idx:04d}_adj.wav"
                if not adjusted.exists():
                    filt = _build_atempo_filter(speed_ratio)
                    try:
                        subprocess.run([
                            "ffmpeg", "-i", str(tts_path), "-filter:a", filt,
                            "-ar", "16000", "-ac", "1", str(adjusted), "-y"
                        ], capture_output=True, check=True, timeout=30)
                    except Exception:
                        adjusted = None
                if adjusted and adjusted.exists():
                    tts_audio = PydubSegment.from_wav(str(adjusted))
            # 居中放置，前后填充静音
            gap = target_dur - len(tts_audio)
            if gap > 0:
                pad_front = gap // 2
                padded = PydubSegment.silent(duration=target_dur, frame_rate=16000)
                padded = padded.overlay(tts_audio, position=pad_front)
                tts_audio = padded
            stats["padded"] += 1
        elif 0.5 < speed_ratio and speed_ratio != 1.0:
            adjusted = tts_dir / f"seg_{idx:04d}_adj.wav"
            if not adjusted.exists():
                filt = _build_atempo_filter(speed_ratio)
                try:
                    subprocess.run([
                        "ffmpeg", "-i", str(tts_path), "-filter:a", filt,
                        "-ar", "16000", "-ac", "1", str(adjusted), "-y"
                    ], capture_output=True, check=True, timeout=30)
                except Exception:
                    adjusted = None
            if adjusted and adjusted.exists():
                tts_audio = PydubSegment.from_wav(str(adjusted))
                stats["adjusted"] += 1

        if len(tts_audio) > target_dur:
            tts_audio = tts_audio[:target_dur]

        if target_start < total_ms:
            final_audio = final_audio.overlay(tts_audio, position=target_start)

    dub_path = output_dir / "chinese_dub.wav"
    final_audio.export(str(dub_path), format="wav")
    print(f"  ✅ 配音完成 (调速:{stats['adjusted']}, 填充:{stats['padded']},"
          f" 限速:{stats['clamped_fast']}, 提速:{stats['clamped_slow']},"
          f" 跳过:{stats['skipped']}, 总:{len(segments)})")
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
    return _align_tts_to_timeline(segments, output_dir)


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

    iter_dir = output_dir / "iterations"
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

        # 2) 无超速 → 收敛（过短交给时间对齐阶段处理）
        if not overfast:
            if underslow:
                print(f"\n  ✅ 翻译优化完成! ({len(underslow)} 个过短片段将由时间线对齐阶段静音填充)")
            else:
                print(f"\n  ✅ 所有片段语速均在合理范围内，优化完成!")
            break

        new_segments = segments

        # 3) LLM 精简过长翻译
        print(f"     调用 LLM 精简 {len(overfast)} 个超速片段...")
        new_segments = _refine_with_llm(new_segments, overfast, llm_config)

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
        "4) 严禁重复上下文内容——上下文摘要仅供避免重复参考，不要从中取内容\n"
        "5) 适合配音朗读，语句自然\n"
        "6) 输出格式：每段先 [编号]，然后分行输出 [轻]/[中]/[短] 三个版本"
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

    return refined


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
    # 去除行首的 markdown/列表标记 + [轻]/[中]/[短] 标签
    text = re.sub(r"^[-*]*\s*\*{0,2}\[([轻中短])\]\*{0,2}\s*", "", text.strip())
    # 如果整行都是系统指令回显（如"以下为每段翻译的三个精简版本..."），返回空
    if re.search(r"[轻中短].*[/／].*[轻中短]", text):
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


def _select_best_candidate(
    candidates: List[str], target_ms: int, original_zh: str,
    idx: int, segments: List[dict],
) -> str:
    """从多个精简候选中选最接近目标时长的，同时排除不合格候选。

    选择策略：
      1. 排除与相邻段重复的候选
      2. 排除比原文更长的候选
      3. 排除与原文语义忠实度过低的候选（防止跨段内容污染）
      4. 用 jieba 分词估算每个候选的朗读时长
      5. 选时长最接近 target_ms 且不超出的
      6. 都超出则选最短的
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
        # 排除比原文更长的
        if len(cand) >= len(original_zh):
            continue
        # 排除与邻段重复的
        if _is_duplicate_of_neighbors(cand, idx, segments):
            continue
        # 排除与原文语义忠实度过低的候选（防止 LLM 跨段内容混淆）
        if not _check_refine_fidelity(original_zh, cand):
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

        # 优先选不超出的；都超出选最接近的
        not_over = [s for s in scored if not s[3]]
        if not_over:
            # 不超出目标的里面，选最接近的
            best = min(not_over, key=lambda s: s[2])
        else:
            # 都超出了，选最短的
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
        "但当前翻译朗读时间远短于原始时间窗口，播放时会被降速，听起来不自然。\n"
        "要求：\n"
        "1) 适当补充细节或使用更完整的表达，使翻译朗读时长更接近英文原文\n"
        "2) 参考上下文保持语义连贯，不要凭空捏造无关内容\n"
        "3) 适合配音朗读，语句自然流畅\n"
        "4) 只输出扩展后的翻译，保持 [编号] 格式，一行一句"
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
            lines.append(
                f"[{j+1}]\n"
                f"  英文: {item['text_en']}\n"
                f"  当前翻译: {item['text_zh']}\n"
                f"  需扩展约 {expansion}% (当前仅占时间窗口的 {ratio:.0%})\n"
                f"  上文: {prev_zh}\n"
                f"  下文: {next_zh}"
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
                    # 只有确实变长了才采纳
                    if len(clean_zh) > len(item["text_zh"]):
                        # 检查是否与相邻段重复
                        if _is_duplicate_of_neighbors(clean_zh, item["idx"], expanded):
                            print(f"       ⚠️  #{item['idx']} 扩展结果与相邻段重复，跳过")
                            continue
                        expanded[item["idx"]]["text_zh"] = clean_zh
        except Exception as e:
            print(f"     ⚠️  LLM 扩展批次 {i//batch_size+1} 失败: {e}")

    return expanded


def clean_iterations(output_dir: Path):
    """清理迭代中间数据，恢复到初始翻译状态"""
    iter_dir = output_dir / "iterations"
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
            f"[3:a]aresample=44100[dub];"
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
            f"[0:a]volume={volume}[bg];[1:a]aresample=44100[dub];"
            f"[bg][dub]amix=inputs=2:duration=first:dropout_transition=2[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(final_path), "-y"
        ], capture_output=True, check=True)

    print(f"  ✅ 最终视频: {final_path}")
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
                    config.get("audio_separation", {}))
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
                    config.get("whisper_beam_size", 5))
                raw_segments = deduplicate_segments(raw_segments)
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
            failure_json_path = output_dir / "tts_failure.json"
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

        # 字幕 + 时间线对齐
        _logger.step_begin("字幕+对齐")
        step_n = _next_step()
        if segments:
            _log(f"[{step_n}/{total_steps}] 生成字幕 + 时间线对齐")
            if "subtitle" not in skip:
                generate_srt_files(segments, output_dir)
            dub_path = _align_tts_to_timeline(segments, output_dir)
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
    main()
