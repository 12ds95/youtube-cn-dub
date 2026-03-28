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
        "style": "",  # 翻译风格: "" (默认), "口语化", "正式", "学术" 等
    },

    # 性能选项
    "tts_concurrency": 5,        # TTS 并发数
    "whisper_beam_size": 5,      # Whisper beam search 大小
    "skip_steps": [],            # 跳过的步骤: ["download","transcribe","translate","subtitle","tts","merge"]

    # 迭代优化（翻译过长时自动精简）
    # 小循环（自动）：测量→精简→重TTS→再测量→仍超速则继续精简，直到收敛
    # 大循环（人工）：人工审听后决定是否再跑一轮，用 --resume-iteration 断点续跑
    "refine": {
        "enabled": False,          # 是否启用
        "max_iterations": 5,       # 小循环最大迭代轮次
        "speed_threshold": 1.25,   # 加速倍率阈值（>1.25x 即触发精简，1.5x 已很明显）
        "resume_iteration": None,  # 从第 N 轮迭代恢复（大循环断点续跑）
    },
    "clean_iterations": False,     # 清理迭代中间数据
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


# ─── Step 3.5: 去重 ────────────────────────────────────────────────
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

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(endpoint, json=payload, headers=headers)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"].strip()

            # 解析返回的编号格式
            translations = _parse_numbered_translations(content, len(batch))
            # 对齐保证：验证解析结果数量是否匹配
            if len([t for t in translations if t.strip()]) < len(batch) * 0.7:
                print(f"     ⚠️  LLM 批次对齐失败 ({len(translations)} vs {len(batch)})，降级逐条翻译")
                translations = _translate_llm_single(batch, endpoint, headers, model, system_prompt, temperature)
        except Exception as e:
            print(f"     ⚠️  LLM 批次 {batch_idx+1} 失败: {e}")
            # 降级：逐条翻译
            translations = _translate_llm_single(batch, endpoint, headers, model, system_prompt, temperature)

        batch_results = []
        for seg, zh in zip(batch, translations):
            # 校验：翻译过短（<2字符且原文>10字符）视为解析失败，保留原文
            if zh and len(zh.strip()) >= 2:
                text_zh = zh.strip()
            elif len(seg["text"]) > 10:
                print(f"     ⚠️  翻译异常（\"{zh}\"），保留原文: \"{seg['text'][:30]}\"")
                text_zh = seg["text"]
            else:
                text_zh = zh or seg["text"]
            # 最终安全网：确保 text_zh 没有 [N] 前缀泄漏
            if re.match(r"^\[\d+\]", text_zh):
                text_zh = _strip_numbered_prefix(text_zh)
            batch_results.append({
                "start": seg["start"], "end": seg["end"],
                "text_en": seg["text"], "text_zh": text_zh,
            })
            result.append(batch_results[-1])

        # 保存最后两句作为下一批的上下文
        if batch_results:
            prev_context = [r["text_zh"] for r in batch_results[-2:]]

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
                zh = _strip_think_block(zh)  # 去除可能的 <think> 块
                results.append(zh)
        except Exception:
            results.append(seg["text"])
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


# ─── Step 6: 中文配音 (拆分为三阶段) ──────────────────────────────
async def _tts_one(text: str, path: str, voice: str, semaphore):
    """生成单个 TTS 片段（带并发控制）"""
    import edge_tts
    async with semaphore:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(path)


async def _generate_tts_segments(
    segments: List[dict], tts_dir: Path,
    voice: str = "zh-CN-YunxiNeural", concurrency: int = 5,
):
    """阶段 A: 并发生成 TTS .mp3 文件"""
    tts_dir.mkdir(exist_ok=True)
    semaphore = asyncio.Semaphore(concurrency)
    tasks = []
    for idx, seg in enumerate(segments):
        p = tts_dir / f"seg_{idx:04d}.mp3"
        if not p.exists() or p.stat().st_size == 0:
            text_zh = seg.get("text_zh", seg.get("text", ""))
            if len(text_zh.strip()) < 2:
                continue  # 跳过空/垃圾文本，避免生成空 mp3
            if p.exists():
                p.unlink()  # 删除 0 字节文件
            tasks.append(_tts_one(text_zh, str(p), voice, semaphore))

    if tasks:
        print(f"     生成 {len(tasks)} 个新 TTS 片段...")
        batch_n = max(1, concurrency * 2)
        for i in range(0, len(tasks), batch_n):
            await asyncio.gather(*tasks[i:i+batch_n], return_exceptions=True)
            done = min(i + batch_n, len(tasks))
            print(f"     TTS 进度: {done}/{len(tasks)}")
    else:
        print(f"     TTS 片段已缓存，跳过生成")

    # Retry pass: detect and retry 0-byte TTS files
    retry_tasks = []
    for idx, seg in enumerate(segments):
        p = tts_dir / f"seg_{idx:04d}.mp3"
        if p.exists() and p.stat().st_size == 0:
            text_zh = seg.get("text_zh", seg.get("text", ""))
            if len(text_zh.strip()) >= 2:
                p.unlink()  # Remove 0-byte file
                retry_tasks.append(_tts_one(text_zh, str(p), voice, semaphore))

    if retry_tasks:
        print(f"     重试 {len(retry_tasks)} 个 0 字节 TTS 片段...")
        await asyncio.gather(*retry_tasks, return_exceptions=True)

        # If still 0-byte after retry, generate silence placeholder
        for idx, seg in enumerate(segments):
            p = tts_dir / f"seg_{idx:04d}.mp3"
            if p.exists() and p.stat().st_size == 0:
                # Create a minimal silence file so downstream doesn't break
                target_ms = int((seg.get("end", 0) - seg.get("start", 0)) * 1000)
                if target_ms > 0:
                    from pydub import AudioSegment as PydubSegment
                    silence = PydubSegment.silent(duration=min(target_ms, 500), frame_rate=16000)
                    silence.export(str(p), format="mp3")
                    print(f"     ⚠️  seg_{idx:04d}.mp3 重试失败，已填充静音")


def _measure_speed_ratios(
    segments: List[dict], tts_dir: Path, threshold: float = 1.5,
) -> List[dict]:
    """阶段 B: 测量每个片段 TTS 时长 vs 原始时间窗口的比率"""
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
    """阶段 C: atempo 调速 + 叠加拼接 → chinese_dub.wav"""
    from pydub import AudioSegment as PydubSegment

    tts_dir = output_dir / "tts_segments"
    audio_path = output_dir / "audio.wav"
    original_audio = PydubSegment.from_wav(str(audio_path))
    total_ms = len(original_audio)

    print(f"     时间线对齐中...")
    final_audio = PydubSegment.silent(duration=total_ms, frame_rate=16000)
    stats = {"adjusted": 0, "skipped": 0, "padded": 0}

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

    print(f"     全局语速基线: {median_ratio:.2f}x (混合权重={BLEND_WEIGHT}, 平滑系数={SMOOTH_ALPHA})")

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

        speed_ratio = smoothed_ratios[idx] if smoothed_ratios[idx] is not None else (len(tts_audio) / target_dur)

        # 对于过短片段 (ratio < 0.7)：不做极端降速，改用静音填充居中放置
        if speed_ratio < 0.7 and len(tts_audio) > 0:
            # 轻微降速到 0.85x 左右使节奏更自然，剩余用静音填充
            mild_ratio = max(speed_ratio, 0.85)
            if mild_ratio < 0.98:
                adjusted = tts_dir / f"seg_{idx:04d}_adj.wav"
                if not adjusted.exists():
                    filt = _build_atempo_filter(mild_ratio)
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
    print(f"  ✅ 配音完成 (调速:{stats['adjusted']}, 静音填充:{stats['padded']}, 跳过:{stats['skipped']}, 总:{len(segments)})")
    return dub_path


async def generate_chinese_dub(
    segments: List[dict], output_dir: Path,
    voice: str = "zh-CN-YunxiNeural", concurrency: int = 5,
) -> Path:
    """生成中文配音 (A→C 全流程，向后兼容)"""
    print(f"  🗣  生成配音 (voice={voice}, 并发={concurrency})...")
    tts_dir = output_dir / "tts_segments"
    await _generate_tts_segments(segments, tts_dir, voice, concurrency)
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
    迭代优化主循环:
      测量语速比 → 筛选超速片段 → LLM 精简翻译 → 重新 TTS → 重复
    返回优化后的 segments (同时更新 segments_cache.json)
    """
    import copy

    refine_cfg = config.get("refine", {})
    max_iter = refine_cfg.get("max_iterations", 3)
    threshold = refine_cfg.get("speed_threshold", 1.5)
    resume_iter = refine_cfg.get("resume_iteration")
    voice = config["voice"]
    concurrency = config.get("tts_concurrency", 5)
    llm_config = config.get("llm", {})

    if not llm_config.get("api_key"):
        print("  ⚠️  迭代优化需要 LLM 翻译引擎 (llm.api_key 为空)，跳过")
        return segments

    tts_dir = output_dir / "tts_segments"
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
        print(f"  🔄 迭代优化 第 {it+1}/{max_iter} 轮 (阈值: >{threshold}x)")
        print(f"  {'─'*50}")

        # 1) 测量语速
        speed_data = _measure_speed_ratios(segments, tts_dir, threshold)
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
            no_tts = [d for d in skipped if d.get("skip_reason") == "no_tts"]
            print(f"     跳过片段: {len(skipped)} (无TTS: {len(no_tts)}, 零时长: {len(skipped)-len(no_tts)})")
        if overfast:
            top = sorted(overfast, key=lambda x: x["speed_ratio"], reverse=True)[:5]
            for d in top:
                zh_pre = d["text_zh"][:25] + ("..." if len(d["text_zh"]) > 25 else "")
                print(f"       #{d['idx']:3d}  {d['speed_ratio']:.2f}x  \"{zh_pre}\"")

        # 2) 无超速且无过短 → 收敛
        if not overfast and not underslow:
            print(f"\n  ✅ 所有片段语速均在合理范围内，优化完成!")
            break

        new_segments = segments

        # 3a) LLM 精简过长翻译
        if overfast:
            print(f"     调用 LLM 精简 {len(overfast)} 个超速片段...")
            new_segments = _refine_with_llm(new_segments, overfast, llm_config)

        # 3b) 过短片段不再调用 LLM 扩展（LLM 容易生成偏离原文的内容）
        #     改为交给 _align_tts_to_timeline 的静音填充 + 轻微降速处理
        if underslow:
            print(f"     ⏭  {len(underslow)} 个过短片段将由时间线对齐阶段处理（静音填充+降速）")

        # 4) 统计变更
        changes = []
        changed_indices = []
        all_adjusted = overfast + underslow
        for item in all_adjusted:
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

        # 6) 删除被修改片段的 TTS 缓存，重新生成
        for idx in changed_indices:
            for suffix in [".mp3", "_adj.wav"]:
                p = tts_dir / f"seg_{idx:04d}{suffix}"
                if p.exists():
                    p.unlink()
        print(f"     增量 TTS: 仅重新生成 {len(changed_indices)}/{len(segments)} 个变更片段")
        await _generate_tts_segments(segments, tts_dir, voice, concurrency)

    # 写回主缓存
    cache_file = output_dir / "segments_cache.json"
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)

    # 输出大循环引导（人工审听）
    final_speed = _measure_speed_ratios(segments, tts_dir, threshold)
    final_overfast = [d for d in final_speed if d["status"] == "overfast"]
    final_underslow = [d for d in final_speed if d["status"] == "underslow"]
    final_active = [d for d in final_speed if d["status"] != "skipped"]
    final_max = max((d["speed_ratio"] for d in final_active), default=0)
    final_min = min((d["speed_ratio"] for d in final_active if d["speed_ratio"] > 0), default=0)
    final_avg = sum(d["speed_ratio"] for d in final_active) / max(1, len(final_active))

    print(f"\n  {'━'*50}")
    print(f"  📊 小循环完成 — 语速分析汇总:")
    print(f"     总片段: {len(final_active)}, 仍超速: {len(final_overfast)}, 仍过短: {len(final_underslow)}")
    print(f"     最大: {final_max:.2f}x, 最小: {final_min:.2f}x, 平均: {final_avg:.2f}x")
    if final_overfast or final_underslow:
        print(f"  💡 建议：播放 final.mp4 实际审听，若仍有语速问题可再次运行:")
        print(f"     python pipeline.py --resume-from {output_dir} --refine 5")
        print(f"     或手动编辑 segments_cache.json 中 text_zh 字段后重跑")
    else:
        print(f"  ✅ 所有片段语速均在合理范围内，语速自然")
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


def _refine_with_llm(
    segments: List[dict], overfast_items: List[dict], llm_config: dict,
) -> List[dict]:
    """使用 LLM 精简过长的翻译（带上下文感知）"""
    import httpx
    import copy

    refined = copy.deepcopy(segments)

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
        "但当前翻译朗读时间超出原始时间窗口，需要精简。\n"
        "要求：\n"
        "1) 保持核心语义不变，用更简洁的中文表达\n"
        "2) 必须忠实翻译当前英文原文，不得偏离原文含义\n"
        "3) 严禁与上文或下文重复——精简后的译文不能和相邻段落说同样的话\n"
        "4) 适合配音朗读，语句自然\n"
        "5) 只输出精简后的翻译，保持 [编号] 格式，一行一句"
    )

    for i in range(0, len(overfast_items), batch_size):
        batch = overfast_items[i:i + batch_size]
        lines = []
        for j, item in enumerate(batch):
            idx = item["idx"]
            ratio = item["speed_ratio"]
            reduction = int((1 - 1.0 / ratio) * 100)
            prev_zh = segments[idx - 1]["text_zh"] if idx > 0 else "(开头)"
            next_zh = (segments[idx + 1]["text_zh"]
                       if idx < len(segments) - 1 else "(结尾)")
            lines.append(
                f"[{j+1}]\n"
                f"  英文: {item['text_en']}\n"
                f"  当前翻译: {item['text_zh']}\n"
                f"  需缩短约 {reduction}% (当前需 {ratio:.1f}x 加速)\n"
                f"  上文: {prev_zh}\n"
                f"  下文: {next_zh}"
            )

        user_msg = (
            f"请精简以下 {len(batch)} 段翻译。"
            f"每段用 [编号] 格式输出精简后的译文，一行一句：\n\n"
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
                    # 安全网：去除可能的 [N] 前缀
                    clean_zh = _strip_numbered_prefix(new_zh) if re.match(r"^\[\d+\]", new_zh) else new_zh
                    # 只有确实变短了才采纳
                    if len(clean_zh) < len(item["text_zh"]):
                        # 检查是否与相邻段重复
                        if _is_duplicate_of_neighbors(clean_zh, item["idx"], refined):
                            print(f"       ⚠️  #{item['idx']} 精简结果与相邻段重复，跳过")
                            continue
                        refined[item["idx"]]["text_zh"] = clean_zh
        except Exception as e:
            print(f"     ⚠️  LLM 精简批次 {i//batch_size+1} 失败: {e}")

    return refined


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
    refine_enabled = config.get("refine", {}).get("enabled", False)
    total_steps = 8 if refine_enabled else 7

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
    if refine_enabled:
        rcfg = config["refine"]
        print(f"   迭代优化: {rcfg['max_iterations']} 轮 (阈值 >{rcfg['speed_threshold']}x)")
    print(f"{'='*60}\n")

    # ── 清理迭代数据 ──
    if config.get("clean_iterations"):
        clean_iterations(output_dir)
        print()

    cache_file = output_dir / "segments_cache.json"

    # Step 1: 下载
    video_path = output_dir / "original.mp4"
    title = "unknown"
    if "download" not in skip:
        print(f"[1/{total_steps}] 下载视频")
        if config["resume_from"] and video_path.exists():
            print(f"  ⏭  使用已有视频")
            info_file = output_dir / "info.json"
            if info_file.exists():
                title = json.load(open(info_file))["title"]
        else:
            video_path, title = download_video(url, output_dir, config["browser"])
        config["video_title"] = title
        print()

    # Step 2: 提取音频
    if "extract" not in skip:
        print(f"[2/{total_steps}] 提取音频")
        audio_path = extract_audio(video_path, output_dir)
        print()

    # Step 3+4: 转录 + 翻译
    if cache_file.exists() and "transcribe" not in skip and "translate" not in skip:
        print(f"[3/{total_steps}] 语音识别")
        print(f"[4/{total_steps}] 翻译")
        print("  ⏭  使用缓存 (segments_cache.json)")
        with open(cache_file, "r", encoding="utf-8") as f:
            segments = json.load(f)
        print(f"     {len(segments)} 个片段已加载")
        print()
    else:
        if "transcribe" not in skip:
            print(f"[3/{total_steps}] 语音识别 (Whisper)")
            raw_segments = transcribe_audio(
                output_dir / "audio.wav", config["whisper_model"],
                config.get("whisper_beam_size", 5))
            raw_segments = deduplicate_segments(raw_segments)
            print()
        else:
            print(f"[3/{total_steps}] 语音识别 - 跳过")
            raw_segments = []
            print()

        if "translate" not in skip and raw_segments:
            print(f"[4/{total_steps}] 翻译")
            segments = translate_segments(raw_segments, config)
            segments = deduplicate_segments(segments)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(segments, f, ensure_ascii=False, indent=2)
            print()
        else:
            print(f"[4/{total_steps}] 翻译 - 跳过")
            segments = []
            print()

    # ──────────── 分支: 标准 vs 迭代优化 ────────────
    if refine_enabled and segments:
        # ── 迭代优化流程 (8步) ──

        # Step 5: 生成 TTS
        if "tts" not in skip:
            print(f"[5/{total_steps}] 生成 TTS 片段")
            print(f"  🗣  voice={config['voice']}, 并发={config.get('tts_concurrency', 5)}")
            tts_dir = output_dir / "tts_segments"
            await _generate_tts_segments(
                segments, tts_dir, config["voice"],
                config.get("tts_concurrency", 5))
            print()

        # Step 6: 迭代优化
        if "refine" not in skip:
            print(f"[6/{total_steps}] 迭代优化翻译")
            segments = await run_refinement_loop(segments, output_dir, config)
            print()

        # Step 7: 字幕 + 时间线对齐
        if segments:
            print(f"[7/{total_steps}] 生成字幕 + 时间线对齐")
            if "subtitle" not in skip:
                generate_srt_files(segments, output_dir)
            dub_path = _align_tts_to_timeline(segments, output_dir)
            print()

        # Step 8: 合成
        dub_path = output_dir / "chinese_dub.wav"
        if "merge" not in skip and dub_path.exists():
            print(f"[8/{total_steps}] 合成最终视频")
            final_path = merge_final_video(
                video_path, dub_path, output_dir, config["volume"])
            print()
    else:
        # ── 标准流程 (7步) ──

        # Step 5: 字幕
        if "subtitle" not in skip and segments:
            print(f"[5/{total_steps}] 生成字幕")
            srt_en, srt_zh, srt_bi = generate_srt_files(segments, output_dir)
            print()

        # Step 6: 配音
        if "tts" not in skip and segments:
            print(f"[6/{total_steps}] 生成中文配音")
            dub_path = await generate_chinese_dub(
                segments, output_dir, config["voice"],
                config.get("tts_concurrency", 5))
            print()

        # Step 7: 合成
        dub_path = output_dir / "chinese_dub.wav"
        if "merge" not in skip and dub_path.exists():
            print(f"[7/{total_steps}] 合成最终视频")
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
