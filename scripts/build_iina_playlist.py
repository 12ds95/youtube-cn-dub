#!/usr/bin/env python3
"""扫描 output/<topic>/<NN_title>/<title>.mp4 生成 m3u8 播放列表给 IINA。

按 3blue1brown_videos.json 的 topic 顺序 + 目录前缀 NN 排序。

字幕集成: 通过 scripts/mux_subtitles_into_mp4.py 把 subtitle_bilingual.srt
直接 mux 进 <title>.mp4 软字幕轨 (一次处理永久生效, 任何播放器都识别).
本脚本只生成 playlist, 字幕由 mp4 容器自带, 不再创建外部字幕软链.
"""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
JSON_FILE = Path("/Users/caixin/Desktop/youtube-cn-dub-batch/3blue1brown_videos.json")
PLAYLIST = OUTPUT / "playlist.m3u8"


def topic_order():
    """从 JSON 取 topic 顺序; 缺失则按字母序兜底。"""
    if JSON_FILE.exists():
        try:
            data = json.loads(JSON_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return list(data.keys())
        except Exception:
            pass
    return sorted([d.name for d in OUTPUT.iterdir() if d.is_dir()])


def nn_key(dirname: str):
    """提取目录名前缀 NN, 用于段内排序; 无前缀则按字母序末尾。"""
    m = re.match(r"^(\d+)_", dirname)
    return (0, int(m.group(1))) if m else (1, dirname)


def has_internal_subtitle(mp4: Path) -> bool:
    """ffprobe 检测 mp4 是否已 mux 过字幕轨."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "s",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(mp4)],
            text=True, stderr=subprocess.DEVNULL,
        )
        return bool(out.strip())
    except Exception:
        return False


def main():
    topics = topic_order()
    entries = []  # [(topic, nn, title, path)]
    no_subtitle = []  # mp4 还没 mux 字幕

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
            if not has_internal_subtitle(mp4):
                no_subtitle.append(str(vdir.relative_to(OUTPUT)))
            title = re.sub(r"^\d+_", "", vdir.name).replace("_", " ")
            entries.append((topic, vdir.name, title, mp4))

    lines = ["#EXTM3U"]
    for topic, _nn, title, mp4 in entries:
        display = f"{topic} — {title}"
        lines.append(f"#EXTINF:-1,{display}")
        lines.append(str(mp4))

    PLAYLIST.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✅ 生成 {PLAYLIST}")
    print(f"   {len(entries)} 个视频, 按 topic 顺序: {', '.join(topics)}")
    if no_subtitle:
        print(f"   ⚠️  {len(no_subtitle)} 个视频未 mux 字幕, 跑 mux_subtitles_into_mp4.py 补:")
        for v in no_subtitle[:5]:
            print(f"        {v}")
        if len(no_subtitle) > 5:
            print(f"        ... +{len(no_subtitle)-5} 个")
    else:
        print(f"   ✅ 全部 mp4 已内嵌字幕轨")


if __name__ == "__main__":
    main()
