#!/bin/bash
# ============================================================
# 环境部署脚本
# 用法: bash setup.sh
# 功能: 创建 venv、安装 Python 依赖、检查系统工具
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
PYTHON=""

echo "============================================================"
echo "  YouTube 中文配音工具 - 环境部署"
echo "============================================================"
echo ""

# ─── 1. 查找合适的 Python ─────────────────────────────────────
echo "[1/4] 查找 Python..."

# 优先级: python3.11 > python3.10 > python3.9 > python3 (排除 Anaconda)
for candidate in python3.11 python3.10 python3.9 python3; do
    path=$(command -v "$candidate" 2>/dev/null || true)
    if [ -n "$path" ]; then
        # 跳过 Anaconda 的 python（可能版本过旧或有兼容问题）
        if "$path" -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>/dev/null; then
            PYTHON="$path"
            break
        fi
    fi
done

# Homebrew 常见路径兜底
if [ -z "$PYTHON" ]; then
    for p in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        if [ -x "$p" ] && "$p" -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>/dev/null; then
            PYTHON="$p"
            break
        fi
    done
fi

if [ -z "$PYTHON" ]; then
    echo "  ❌ 未找到 Python 3.9+。请安装: brew install python@3.11"
    exit 1
fi

PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
echo "  ✅ 使用 $PYTHON (Python $PY_VERSION)"

# ─── 2. 创建虚拟环境 ──────────────────────────────────────────
echo ""
echo "[2/4] 配置虚拟环境..."

if [ -d "$VENV_DIR" ] && [ -f "$VENV_DIR/bin/python3" ]; then
    echo "  ⏭  venv 已存在，跳过创建"
else
    echo "  📦 创建 venv..."
    "$PYTHON" -m venv "$VENV_DIR"
    echo "  ✅ venv 创建完成"
fi

VENV_PIP="$VENV_DIR/bin/pip"
VENV_PYTHON="$VENV_DIR/bin/python3"

# ─── 3. 安装 Python 依赖 ──────────────────────────────────────
echo ""
echo "[3/4] 安装 Python 依赖..."

# 国内镜像源（加速下载，可通过 PIP_INDEX_URL 环境变量覆盖）
PIP_MIRROR="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
PIP_INSTALL="$VENV_PIP install -i $PIP_MIRROR --trusted-host $(echo $PIP_MIRROR | sed 's|https\?://\([^/]*\).*|\1|')"

$PIP_INSTALL --upgrade pip -q

PACKAGES=(
    "faster-whisper"
    "edge-tts"
    "deep-translator"
    "pydub"
    "yt-dlp"
    "httpx"
    "demucs"
    "spacy"
    "jieba"
    "pypinyin"
    "sentencepiece"
)

for pkg in "${PACKAGES[@]}"; do
    if "$VENV_PYTHON" -c "import importlib; importlib.import_module('${pkg//-/_}')" 2>/dev/null; then
        echo "  ⏭  $pkg 已安装"
    else
        echo "  📦 安装 $pkg..."
        $PIP_INSTALL "$pkg" -q
        echo "  ✅ $pkg"
    fi
done

# spaCy 英文模型（NLP 断句需要）
if "$VENV_PYTHON" -c "import spacy; spacy.load('en_core_web_sm')" 2>/dev/null; then
    echo "  ⏭  spacy en_core_web_sm 已安装"
else
    echo "  📦 安装 spacy en_core_web_sm 模型..."
    # 优先用 GitHub 加速代理（国内直连 GitHub 慢）
    $PIP_INSTALL https://ghfast.top/https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl -q 2>/dev/null || \
        "$VENV_PYTHON" -m spacy download en_core_web_sm -q 2>/dev/null || \
        echo "  ⚠️  en_core_web_sm 自动安装失败，请手动: $VENV_PYTHON -m spacy download en_core_web_sm"
    echo "  ✅ en_core_web_sm"
fi

# yt-dlp-ejs (YouTube 反爬需要)
if "$VENV_PIP" show yt-dlp-ejs >/dev/null 2>&1; then
    echo "  ⏭  yt-dlp-ejs 已安装"
else
    echo "  📦 安装 yt-dlp-ejs..."
    $PIP_INSTALL yt-dlp-ejs -q
    echo "  ✅ yt-dlp-ejs"
fi

# 质量评分工具（可选，score_videos.py 使用）
echo ""
echo "  📦 安装质量评分工具（可选）..."
$PIP_INSTALL praat-parselmouth -q 2>/dev/null && echo "  ✅ praat-parselmouth" || echo "  ⏭  praat-parselmouth (可选，跳过)"

# ─── 4. 检查系统工具 ──────────────────────────────────────────
echo ""
echo "[4/4] 检查系统工具..."

MISSING=()

# ffmpeg
if command -v ffmpeg >/dev/null 2>&1; then
    FF_VERSION=$(ffmpeg -version 2>&1 | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
    echo "  ✅ ffmpeg ($FF_VERSION)"
else
    MISSING+=("ffmpeg (brew install ffmpeg)")
fi

# ffprobe
if command -v ffprobe >/dev/null 2>&1; then
    echo "  ✅ ffprobe"
else
    MISSING+=("ffprobe (随 ffmpeg 一起安装)")
fi

# Node.js
NODE_FOUND=false
if command -v node >/dev/null 2>&1; then
    NODE_FOUND=true
else
    # 检查 nvm
    export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
    [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh" 2>/dev/null
    if command -v node >/dev/null 2>&1; then
        NODE_FOUND=true
    fi
fi

if $NODE_FOUND; then
    echo "  ✅ Node.js ($(node --version 2>/dev/null || echo 'unknown'))"
else
    echo "  ⚠️  Node.js 未找到（可选，部分 YouTube 下载场景需要）"
    echo "     安装: brew install node"
fi

# ─── 结果汇总 ─────────────────────────────────────────────────
echo ""
echo "============================================================"
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "  ⚠️  缺少以下系统工具，请手动安装:"
    for m in "${MISSING[@]}"; do
        echo "     - $m"
    done
    echo ""
    echo "  安装后即可使用: bash run.sh \"YouTube_URL\""
else
    echo "  ✅ 环境部署完成！"
    echo ""
    echo "  快速开始:"
    echo "    bash run.sh \"https://www.youtube.com/watch?v=XXXX\""
    echo ""
    echo "  使用 LLM 翻译（推荐）:"
    echo "    cp config.example.json config.json"
    echo "    # 编辑 config.json 填入 API Key"
    echo "    bash run.sh --config config.json"
fi
echo "============================================================"
