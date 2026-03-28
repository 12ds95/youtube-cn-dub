#!/bin/bash
# ============================================================
# 一键启动脚本
# 用法: bash run.sh <YouTube_URL> [其他参数]
# 示例: bash run.sh "https://www.youtube.com/watch?v=XXXX"
#       bash run.sh "https://www.youtube.com/watch?v=XXXX" --voice zh-CN-XiaoxiaoNeural
#       bash run.sh "https://www.youtube.com/watch?v=XXXX" --whisper-model small
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 激活虚拟环境
source "$SCRIPT_DIR/venv/bin/activate"

# 确保 Node.js 在 PATH (nvm 环境)
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh" 2>/dev/null
# 或直接加入 nvm node 路径
for d in "$HOME"/.nvm/versions/node/*/bin; do
    if [ -d "$d" ]; then
        export PATH="$d:$PATH"
        break
    fi
done

# 运行 pipeline
python "$SCRIPT_DIR/pipeline.py" "$@"
