#!/usr/bin/env python3
"""把每个视频目录的 subtitle_bilingual.srt mux 进 <title>.mp4 软字幕轨.

为什么:
  m3u8 + 字幕软链路线在 IINA 下不可靠 (mpv 不支持 #EXTVLCOPT, 且
  IINA 在 playlist 模式下对外挂字幕扫描行为不一致). mux 后字幕成为
  mp4 容器的内嵌轨, 任何播放器都识别, 不依赖文件名匹配.

策略:
  - 仅 remux (-c copy -c:s mov_text), 不重新编码视频/音频, 每个视频几秒
  - atomic 替换: 写入 <title>.tmp.mp4, ffmpeg 成功后 mv 覆盖原 <title>.mp4
  - 已经 mux 过的视频跳过 (检测内嵌字幕轨)
  - 保留 subtitle_*.srt 源文件 (将来想改字幕仍可重 mux)
  - 顺便清理已无用的 <title>.{srt,zh.srt,en.srt} 软链
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
JSON_FILE = Path("/Users/caixin/Desktop/youtube-cn-dub-batch/3blue1brown_videos.json")


def topic_order():
    if JSON_FILE.exists():
        try:
            data = json.loads(JSON_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return list(data.keys())
        except Exception:
            pass
    return sorted([d.name for d in OUTPUT.iterdir() if d.is_dir()])


def nn_key(dirname: str):
    m = re.match(r"^(\d+)_", dirname)
    return (0, int(m.group(1))) if m else (1, dirname)


def has_subtitle_track(mp4: Path) -> bool:
    """ffprobe 检测 mp4 是否已含字幕轨."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "s",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(mp4)],
            text=True, stderr=subprocess.DEVNULL,
        )
        return bool(out.strip())
    except Exception:
        return False


def mux_one(mp4: Path, srt: Path) -> tuple[bool, str]:
    """remux 字幕进 mp4. 成功返回 (True, ''), 失败返回 (False, err)."""
    tmp = mp4.with_suffix(".tmp.mp4")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(mp4), "-i", str(srt),
             "-c", "copy", "-c:s", "mov_text",
             "-metadata:s:s:0", "language=zho",
             "-metadata:s:s:0", "title=双语",
             "-disposition:s:0", "default+forced",
             str(tmp)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            return False, result.stderr[-500:]
        # atomic 替换
        tmp.replace(mp4)
        return True, ""
    except subprocess.TimeoutExpired:
        if tmp.exists():
            tmp.unlink()
        return False, "ffmpeg 超时 (>300s)"
    except Exception as e:
        if tmp.exists():
            tmp.unlink()
        return False, str(e)


def cleanup_old_symlinks(vdir: Path, mp4_stem: str) -> int:
    """清理之前 build_iina_playlist.py 生成的字幕软链 (mux 后不需要)."""
    n = 0
    for suffix in (".srt", ".zh.srt", ".en.srt"):
        link = vdir / f"{mp4_stem}{suffix}"
        if link.is_symlink():
            link.unlink()
            n += 1
    return n


def main():
    topics = topic_order()
    todo = []  # [(vdir, mp4, srt)]
    skipped_done = 0
    skipped_nosub = 0

    for topic in topics:
        topic_dir = OUTPUT / topic
        if not topic_dir.is_dir():
            continue
        for vdir in sorted(topic_dir.iterdir(), key=lambda d: nn_key(d.name)):
            if not vdir.is_dir():
                continue
            mp4s = [f for f in vdir.glob("*.mp4")
                    if f.name not in ("original.mp4", "final.mp4")
                    and not f.name.endswith(".tmp.mp4")]
            if not mp4s:
                continue
            mp4 = mp4s[0]
            srt = vdir / "subtitle_bilingual.srt"
            if not srt.exists():
                skipped_nosub += 1
                continue
            if has_subtitle_track(mp4):
                skipped_done += 1
                # 已 mux 过, 顺便清理软链
                cleanup_old_symlinks(vdir, mp4.stem)
                continue
            todo.append((vdir, mp4, srt))

    print(f"扫描结果: {len(todo)} 个待 mux, {skipped_done} 个已 mux 跳过, {skipped_nosub} 个无字幕源跳过\n")
    if not todo:
        print("✅ 全部已 mux, 无需处理")
        return

    n_ok = 0
    n_fail = 0
    n_links_cleaned = 0
    for i, (vdir, mp4, srt) in enumerate(todo, 1):
        rel = vdir.relative_to(OUTPUT)
        print(f"[{i:>3}/{len(todo)}] {rel}")
        ok, err = mux_one(mp4, srt)
        if ok:
            n_ok += 1
            n_links_cleaned += cleanup_old_symlinks(vdir, mp4.stem)
            print(f"        ✅ ok ({mp4.stat().st_size // 1024 // 1024} MB)")
        else:
            n_fail += 1
            print(f"        ❌ {err[:200]}")

    print(f"\n汇总: 成功 {n_ok}, 失败 {n_fail}, 顺手清理软链 {n_links_cleaned} 条")
    if n_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
