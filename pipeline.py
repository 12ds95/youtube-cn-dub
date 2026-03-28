#!/usr/bin/env python3
"""
YouTube 英文视频 → 中文配音 + 中英双语字幕 端到端 Pipeline (v2)
================================================================
工具链: yt-dlp + faster-whisper + deep-translator/LLM + edge-tts + ffmpeg

用法:
    # 基础用法
    python pipeline.py "https://www.youtube.com/watch?v=XXXX"

    # 使用 JSON 配置文件
    python pipeline.py --config config.json

    # 从已有输出目录断点续跑（调试翻译/配音）
    python pipeline.py --resume-from output/my_video

    # 完成后重命名输出目录
    python pipeline.py "URL" --rename "线性代数精讲"

输出:
    output/<video_id_or_name>/
        ├── original.mp4          # 原始视频
        ├── audio.wav             # 原始音频
        ├── segments_cache.json   # 转录+翻译缓存（可手动编辑）
        ├── subtitle_en.srt       # 英文字幕
        ├── subtitle_zh.srt       # 中文字幕
        ├── subtitle_bilingual.srt # 中英双语字幕
        ├── chinese_dub.wav       # 中文配音音轨
        └── final.mp4             # 最终输出（中文配音 + 外挂字幕）
"""

import argparse
import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

# ─── 默认配置 ──────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "url": None,
    "output": "output",
    "voice": "zh-CN-YunxiNeural",
    "whisper_model": "small",
    "volume": 0.15,
    "browser": "chrome",
    "rename": None,
    "resume_from": None,

    # 翻译引擎: "google" 或 "llm"
    "translator": "google",
    # LLM 翻译配置（当 translator="llm" 时生效）
    "llm": {
        "api_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "system_prompt": (
            "你是专业的英中翻译引擎。将以下英文文本翻译为简体中文。"
            "要求：1)翻译准确流畅，符合中文表达习惯；"
            "2)保持技术术语的专业性；"
            "3)翻译要适合做视频配音朗读，语句通顺自然；"
            "4)只输出翻译结果，不要解释。"
        ),
        "batch_size": 15,
        "temperature": 0.3,
    },

    # 性能选项
    "tts_concurrency": 5,        # TTS 并发数
    "whisper_beam_size": 5,      # Whisper beam search 大小
    "skip_steps": [],            # 跳过的步骤: ["download","transcribe","translate","subtitle","tts","merge"]
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
    """检查必要工具"""
    missing = []
    for mod in ["faster_whisper", "edge_tts", "deep_translator", "pydub"]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod.replace("_", "-"))
    for cmd in ["ffmpeg", "ffprobe"]:
        if not shutil.which(cmd):
            missing.append(cmd)
    if not shutil.which("yt-dlp"):
        try:
            __import__("yt_dlp")
        except ImportError:
            missing.append("yt-dlp")

    if config["translator"] == "llm":
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
def download_video(url: str, output_dir: Path, browser: str = "chrome") -> Tuple[Path, str]:
    """使用 yt-dlp 下载视频"""
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

    ydl_opts = {
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
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
            with open(output_dir / "info.json", "w", encoding="utf-8") as f:
                json.dump({"title": title, "id": info.get("id", ""),
                           "url": url, "duration": info.get("duration", 0)},
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


# ─── Step 4: 翻译 ──────────────────────────────────────────────────

def translate_segments(segments: List[dict], config: dict) -> List[dict]:
    """根据配置选择翻译引擎"""
    engine = config.get("translator", "google")
    if engine == "llm":
        return _translate_llm(segments, config["llm"])
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
            result.append({
                "start": seg["start"], "end": seg["end"],
                "text_en": seg["text"], "text_zh": zh or seg["text"],
            })
        print(f"     进度: {min(i+batch_size, len(segments))}/{len(segments)}")
        if i + batch_size < len(segments):
            time.sleep(1)
    print(f"  ✅ 翻译完成")
    return result


def _translate_llm(segments: List[dict], llm_config: dict) -> List[dict]:
    """LLM 大模型翻译引擎 (OpenAI 兼容 API)"""
    import httpx

    api_url = llm_config["api_url"].rstrip("/")
    api_key = llm_config["api_key"]
    model = llm_config["model"]
    system_prompt = llm_config.get("system_prompt", "将英文翻译为中文，只输出翻译结果。")
    batch_size = llm_config.get("batch_size", 15)
    temperature = llm_config.get("temperature", 0.3)

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

    result = []
    for i in range(0, len(segments), batch_size):
        batch = segments[i:i + batch_size]
        # 构造批量翻译请求：每行一句，用编号标记
        lines = []
        for j, seg in enumerate(batch):
            lines.append(f"[{j+1}] {seg['text']}")
        user_msg = "\n".join(lines)

        batch_prompt = (
            f"{system_prompt}\n\n"
            f"请翻译以下 {len(batch)} 句话，每句保持 [编号] 格式，"
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

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"].strip()

            # 解析返回的编号格式
            translations = _parse_numbered_translations(content, len(batch))
        except Exception as e:
            print(f"     ⚠️  LLM 批次 {i//batch_size+1} 失败: {e}")
            # 降级：逐条翻译
            translations = _translate_llm_single(batch, endpoint, headers, model, system_prompt, temperature)

        for seg, zh in zip(batch, translations):
            result.append({
                "start": seg["start"], "end": seg["end"],
                "text_en": seg["text"], "text_zh": zh or seg["text"],
            })
        print(f"     进度: {min(i+batch_size, len(segments))}/{len(segments)}")

    print(f"  ✅ LLM 翻译完成")
    return result


def _translate_llm_single(batch, endpoint, headers, model, system_prompt, temperature):
    """逐条 LLM 翻译（降级方案）"""
    import httpx
    results = []
    for seg in batch:
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
                results.append(zh)
        except Exception:
            results.append(seg["text"])
    return results


def _parse_numbered_translations(content: str, expected_count: int) -> List[str]:
    """解析 LLM 返回的编号格式翻译"""
    lines = content.strip().split("\n")
    translations = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 匹配 [1] 翻译内容 或 1. 翻译内容
        match = re.match(r"^\[?\d+\]?\s*\.?\s*(.+)$", line)
        if match:
            translations.append(match.group(1).strip())
        elif translations:
            # 可能是上一行的续行
            translations[-1] += line

    # 如果解析数量不对，按行分割
    if len(translations) != expected_count:
        translations = [l.strip() for l in content.strip().split("\n") if l.strip()]

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


# ─── Step 6: 中文配音 ─────────────────────────────────────────────
async def _tts_one(text: str, path: str, voice: str, semaphore):
    """生成单个 TTS 片段（带并发控制）"""
    import edge_tts
    async with semaphore:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(path)


async def generate_chinese_dub(
    segments: List[dict], output_dir: Path,
    voice: str = "zh-CN-YunxiNeural", concurrency: int = 5,
) -> Path:
    """并发生成 TTS + 时间线对齐"""
    from pydub import AudioSegment

    print(f"  🗣  生成配音 (voice={voice}, 并发={concurrency})...")

    tts_dir = output_dir / "tts_segments"
    tts_dir.mkdir(exist_ok=True)

    audio_path = output_dir / "audio.wav"
    original_audio = AudioSegment.from_wav(str(audio_path))
    total_ms = len(original_audio)

    # ── 并发生成 TTS ──
    semaphore = asyncio.Semaphore(concurrency)
    tasks = []
    tts_paths = []
    for idx, seg in enumerate(segments):
        p = tts_dir / f"seg_{idx:04d}.mp3"
        tts_paths.append(p)
        if not p.exists():
            tasks.append(_tts_one(seg["text_zh"], str(p), voice, semaphore))

    if tasks:
        print(f"     生成 {len(tasks)} 个新 TTS 片段...")
        # 分批执行并报告进度
        batch_n = max(1, concurrency * 2)
        for i in range(0, len(tasks), batch_n):
            await asyncio.gather(*tasks[i:i+batch_n], return_exceptions=True)
            done = min(i + batch_n, len(tasks))
            print(f"     TTS 进度: {done}/{len(tasks)}")
    else:
        print(f"     TTS 片段已缓存，跳过生成")

    # ── 时间线对齐 + 拼接 ──
    print(f"     时间线对齐中...")
    final_audio = AudioSegment.silent(duration=total_ms, frame_rate=16000)
    stats = {"adjusted": 0, "skipped": 0}

    for idx, (seg, tts_path) in enumerate(zip(segments, tts_paths)):
        if not tts_path.exists():
            stats["skipped"] += 1
            continue

        tts_audio = AudioSegment.from_mp3(str(tts_path))
        target_start = int(seg["start"] * 1000)
        target_dur = int(seg["end"] * 1000) - target_start

        if target_dur <= 0:
            stats["skipped"] += 1
            continue

        speed_ratio = len(tts_audio) / target_dur
        if 0.5 < speed_ratio and speed_ratio != 1.0:
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
                tts_audio = AudioSegment.from_wav(str(adjusted))
                stats["adjusted"] += 1

        if len(tts_audio) > target_dur:
            tts_audio = tts_audio[:target_dur]

        if target_start < total_ms:
            final_audio = final_audio.overlay(tts_audio, position=target_start)

    dub_path = output_dir / "chinese_dub.wav"
    final_audio.export(str(dub_path), format="wav")
    print(f"  ✅ 配音完成 (调速:{stats['adjusted']}, 跳过:{stats['skipped']}, 总:{len(segments)})")
    return dub_path


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


# ─── Step 7: 合成视频 ─────────────────────────────────────────────
def merge_final_video(video_path: Path, dub_path: Path,
                      output_dir: Path, volume: float = 0.15) -> Path:
    print(f"  🎬 合成最终视频...")
    final_path = output_dir / "final.mp4"
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
    t_start = time.time()
    skip = set(config.get("skip_steps", []))

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

    print(f"   输出目录: {output_dir}")
    print(f"   翻译引擎: {config['translator']}"
          + (f" ({config['llm']['model']})" if config['translator'] == 'llm' else ""))
    print(f"   配音语音: {config['voice']}")
    print(f"   Whisper:  {config['whisper_model']}")
    print(f"{'='*60}\n")

    cache_file = output_dir / "segments_cache.json"

    # Step 1: 下载
    video_path = output_dir / "original.mp4"
    title = "unknown"
    if "download" not in skip:
        print("[1/7] 下载视频")
        if config["resume_from"] and video_path.exists():
            print(f"  ⏭  使用已有视频")
            info_file = output_dir / "info.json"
            if info_file.exists():
                title = json.load(open(info_file))["title"]
        else:
            video_path, title = download_video(url, output_dir, config["browser"])
        print()

    # Step 2: 提取音频
    if "extract" not in skip:
        print("[2/7] 提取音频")
        audio_path = extract_audio(video_path, output_dir)
        print()

    # Step 3+4: 转录 + 翻译
    if cache_file.exists() and "transcribe" not in skip and "translate" not in skip:
        print("[3/7] 语音识别")
        print("[4/7] 翻译")
        print("  ⏭  使用缓存 (segments_cache.json)")
        with open(cache_file, "r", encoding="utf-8") as f:
            segments = json.load(f)
        # 检查：如果用户手动编辑了 cache 或切换了翻译引擎，可以删掉 cache 重跑
        print(f"     {len(segments)} 个片段已加载")
        print()
    else:
        if "transcribe" not in skip:
            print("[3/7] 语音识别 (Whisper)")
            raw_segments = transcribe_audio(
                output_dir / "audio.wav", config["whisper_model"],
                config.get("whisper_beam_size", 5))
            print()
        else:
            print("[3/7] 语音识别 - 跳过")
            raw_segments = []
            print()

        if "translate" not in skip and raw_segments:
            print("[4/7] 翻译")
            segments = translate_segments(raw_segments, config)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(segments, f, ensure_ascii=False, indent=2)
            print()
        else:
            print("[4/7] 翻译 - 跳过")
            segments = []
            print()

    # Step 5: 字幕
    if "subtitle" not in skip and segments:
        print("[5/7] 生成字幕")
        srt_en, srt_zh, srt_bi = generate_srt_files(segments, output_dir)
        print()

    # Step 6: 配音
    if "tts" not in skip and segments:
        print("[6/7] 生成中文配音")
        dub_path = await generate_chinese_dub(
            segments, output_dir, config["voice"],
            config.get("tts_concurrency", 5))
        print()

    # Step 7: 合成
    dub_path = output_dir / "chinese_dub.wav"
    if "merge" not in skip and dub_path.exists():
        print("[7/7] 合成最终视频")
        final_path = merge_final_video(
            video_path, dub_path, output_dir, config["volume"])
        print()

    # ── 重命名输出目录 ──
    final_dir = output_dir
    if config.get("rename"):
        new_name = config["rename"]
        new_dir = output_dir.parent / new_name
        if new_dir.exists():
            print(f"  ⚠️  目标目录已存在，跳过重命名: {new_dir}")
        else:
            output_dir.rename(new_dir)
            final_dir = new_dir
            print(f"  📁 已重命名: {output_dir.name} → {new_name}")

    elapsed = time.time() - t_start
    print(f"\n{'='*60}")
    print(f"🎉 处理完成! (耗时 {elapsed:.0f}s)")
    print(f"   输出目录: {final_dir}")
    print(f"   最终视频: {final_dir}/final.mp4")
    print(f"   双语字幕: {final_dir}/subtitle_bilingual.srt")
    print(f"{'='*60}")


def _url_hash(url: str) -> str:
    import hashlib
    return hashlib.md5(url.encode()).hexdigest()[:11]


# ─── CLI ───────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="YouTube 英文视频 → 中文配音 + 双语字幕 (v2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python pipeline.py "https://www.youtube.com/watch?v=XXXX"
  python pipeline.py --config config.json
  python pipeline.py "URL" --translator llm --llm-api-key sk-xxx
  python pipeline.py --resume-from output/zjMuIxRvygQ --translator llm
  python pipeline.py "URL" --rename "线性代数精讲"

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

    args = parser.parse_args()
    config = load_config(args)

    check_dependencies(config)
    asyncio.run(process_video(config))


if __name__ == "__main__":
    main()
