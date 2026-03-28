#!/bin/bash
# ============================================================
# 一键启动脚本
# 用法: bash run.sh <YouTube_URL> [其他参数]
# 示例: bash run.sh "https://www.youtube.com/watch?v=XXXX"
#       bash run.sh --config config.json
#       bash run.sh --resume-from output/VIDEO_ID
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python3"

# 检查 venv 是否存在
if [ ! -f "$VENV_PYTHON" ]; then
    echo "❌ 虚拟环境未创建。请先运行: bash setup.sh"
    exit 1
fi

# 确保 Homebrew ffmpeg 优先于 Anaconda 的旧版
if [ -d "/opt/homebrew/opt/ffmpeg/bin" ]; then
    export PATH="/opt/homebrew/opt/ffmpeg/bin:$PATH"
elif [ -d "/usr/local/opt/ffmpeg/bin" ]; then
    export PATH="/usr/local/opt/ffmpeg/bin:$PATH"
fi

# 确保 Node.js 在 PATH (nvm 环境)
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh" 2>/dev/null
for d in "$HOME"/.nvm/versions/node/*/bin; do
    if [ -d "$d" ]; then
        export PATH="$d:$PATH"
        break
    fi
done

# 直接用 venv 的 python 执行，避免 source activate 在子 shell 中失效
exec "$VENV_PYTHON" "$SCRIPT_DIR/pipeline.py" "$@"
