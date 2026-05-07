#!/usr/bin/env python3
"""扫描 output/<topic>/<NN_title>/<title>.mp4 生成 m3u8 播放列表给 IINA。

按 3blue1brown_videos.json 的 topic 顺序 + 目录前缀 NN 排序。
"""
import json
import re
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


SUB_VARIANTS = [
    # (源字幕, 软链后缀): IINA/mpv 默认按视频同名 + 语言后缀加载字幕
    # 默认显示双语 (subtitle_bilingual.srt → <title>.srt)
    ("subtitle_bilingual.srt", ".srt"),
    # 切换轨道用 (在 IINA 字幕菜单中可选)
    ("subtitle_zh.srt", ".zh.srt"),
    ("subtitle_en.srt", ".en.srt"),
]


def ensure_subtitle_symlinks(vdir: Path, mp4_stem: str) -> tuple[int, int, int]:
    """在视频目录创建软链 <title>.srt → subtitle_bilingual.srt 等。

    跳过：源字幕不存在 / 目标已是正确软链 / 目标是真实文件（不覆盖）。
    返回 (新建数, 已存在数, 源缺失数)。
    """
    n_created = 0
    n_existed = 0
    n_missing_src = 0
    for src_name, suffix in SUB_VARIANTS:
        src = vdir / src_name
        if not src.exists():
            n_missing_src += 1
            continue
        link = vdir / f"{mp4_stem}{suffix}"
        if link.is_symlink():
            if link.resolve() == src.resolve():
                n_existed += 1
                continue
            link.unlink()
        elif link.exists():
            n_existed += 1  # 真实文件，不动
            continue
        link.symlink_to(src_name)
        n_created += 1
    return n_created, n_existed, n_missing_src


def main():
    topics = topic_order()
    entries = []  # [(topic, nn, title, path)]
    total_created = 0
    total_existed = 0
    total_missing_src = 0
    videos_no_subtitles = []  # 完全没有任何源字幕的视频

    for topic in topics:
        topic_dir = OUTPUT / topic
        if not topic_dir.is_dir():
            continue
        for vdir in sorted(topic_dir.iterdir(), key=lambda d: nn_key(d.name)):
            if not vdir.is_dir():
                continue
            # 找 <title>.mp4 (排除 original.mp4)
            mp4s = [f for f in vdir.glob("*.mp4") if f.name != "original.mp4"]
            if not mp4s:
                continue
            mp4 = mp4s[0]  # 取第一个；正常每目录只有一个非 original 的 mp4
            # 创建字幕软链, IINA 自动挂载
            n_c, n_e, n_m = ensure_subtitle_symlinks(vdir, mp4.stem)
            total_created += n_c
            total_existed += n_e
            total_missing_src += n_m
            if n_c == 0 and n_e == 0:  # 该视频一条字幕源都没有
                videos_no_subtitles.append(str(vdir.relative_to(OUTPUT)))
            # 显示标题: 去掉 NN_ 前缀, 下划线变空格
            title = re.sub(r"^\d+_", "", vdir.name).replace("_", " ")
            entries.append((topic, vdir.name, title, mp4))

    # 写 m3u8
    lines = ["#EXTM3U"]
    for topic, _nn, title, mp4 in entries:
        # IINA 显示格式: "Topic — Title"
        display = f"{topic} — {title}"
        lines.append(f"#EXTINF:-1,{display}")
        lines.append(str(mp4))

    PLAYLIST.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✅ 生成 {PLAYLIST}")
    print(f"   {len(entries)} 个视频, 按 topic 顺序: {', '.join(topics)}")
    print(f"   🔗 字幕软链: 新建 {total_created} 条, 已存在 {total_existed} 条, 源缺失 {total_missing_src} 条")
    if videos_no_subtitles:
        print(f"   ⚠️  {len(videos_no_subtitles)} 个视频完全没字幕源, IINA 不会自动挂载:")
        for v in videos_no_subtitles[:5]:
            print(f"        {v}")
        if len(videos_no_subtitles) > 5:
            print(f"        ... +{len(videos_no_subtitles)-5} 个")


if __name__ == "__main__":
    main()
