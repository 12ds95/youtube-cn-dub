# yt-dlp 下载失败：n challenge solving failed

## 现象

运行 `pipeline.py` 下载 YouTube 视频时，yt-dlp 报错：

```
WARNING: [youtube] a1wgUW2j0Rg: n challenge solving failed: Some formats may be missing.
Ensure you have a supported JavaScript runtime and challenge solver script distribution installed.
WARNING: Only images are available for download. use --list-formats to see them
ERROR: Requested format is not available
```

只能获取 storyboard 图片，没有视频/音频流。

## 排查过程

1. **检查 JS 运行时**：`node --version` → v24.14.0，已安装。
2. **检查 yt-dlp 版本**：2026.3.17（最新）。
3. **检查 EJS 依赖**：`pip list | grep ejs` → 无结果，**缺少 `yt-dlp-ejs` 包**。
4. **查阅 yt-dlp EJS wiki**：PyPI 用户需要 `pip install -U "yt-dlp[default]"` 来安装 EJS challenge solver 脚本。
5. **安装**：`pip install -U "yt-dlp[default]"` → 安装了 `yt-dlp-ejs-0.8.0`。
6. **验证**：安装后 `--js-runtimes node -F` 能正常列出所有视频格式。

## 根因

YouTube 的反爬虫机制要求客户端解一个 JavaScript "n challenge"。yt-dlp 支持用 node/deno/bun 等运行时执行这个 challenge，但需要 `yt-dlp-ejs` 包提供 challenge solver 脚本。

`pipeline.py` 的 `ydl_opts` 中已有 `"js_runtimes": {"node": {}}`，但缺少 EJS 脚本包，所以虽然找到了 node 却无法执行 challenge。

## 修复

```bash
venv/bin/pip install -U "yt-dlp[default]"
```

这会安装 `yt-dlp-ejs`、`websockets`、`brotli`、`mutagen`、`pycryptodomex` 等完整依赖。

## 后续

- `test.sh` 的环境检查应加入 `yt-dlp-ejs` 包检测
- `requirements.txt` 或安装文档应使用 `yt-dlp[default]` 而非裸 `yt-dlp`
