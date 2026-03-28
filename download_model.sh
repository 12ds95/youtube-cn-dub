#!/bin/bash
# ============================================================
# 模型下载脚本 - 通过 hf-mirror.com 国内镜像下载 Whisper 模型
# 用法: bash download_model.sh [tiny|base|small|medium]
# ============================================================

MODEL_SIZE="${1:-small}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODEL_DIR="$SCRIPT_DIR/models/faster-whisper-$MODEL_SIZE"

echo "📥 下载 faster-whisper-$MODEL_SIZE 模型..."
echo "   目标目录: $MODEL_DIR"

mkdir -p "$MODEL_DIR"

BASE="https://hf-mirror.com/Systran/faster-whisper-$MODEL_SIZE/resolve/main"

FILES="config.json vocabulary.txt tokenizer.json preprocessor_config.json model.bin"

for f in $FILES; do
    if [ -f "$MODEL_DIR/$f" ]; then
        echo "  ⏭  $f 已存在，跳过"
        continue
    fi
    echo "  📦 下载 $f ..."
    curl -L --connect-timeout 30 --max-time 1800 --retry 5 --retry-delay 10 -C - \
        "$BASE/$f" -o "$MODEL_DIR/$f"
    if [ $? -ne 0 ]; then
        echo "  ❌ 下载 $f 失败！请重试"
        exit 1
    fi
    echo "  ✅ $f 下载完成 ($(du -h "$MODEL_DIR/$f" | cut -f1))"
done

echo ""
echo "🎉 模型下载完成！"
echo "   位置: $MODEL_DIR"
ls -lh "$MODEL_DIR/"
