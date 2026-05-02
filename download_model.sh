#!/bin/bash
# ============================================================
# 模型下载脚本
# 用法:
#   bash download_model.sh whisper [tiny|base|small|medium|large-v3-turbo]
#   bash download_model.sh piper
#   bash download_model.sh sherpa
#   bash download_model.sh all        # 下载全部
#
# 默认使用国内镜像 (hf-mirror.com)；海外环境可加 --no-mirror
# ============================================================

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
USE_MIRROR=true

# 解析 --no-mirror
for arg in "$@"; do
    if [ "$arg" = "--no-mirror" ]; then
        USE_MIRROR=false
    fi
done
# 清理 flag 参数，保留位置参数
ARGS=()
for arg in "$@"; do
    [[ "$arg" != "--no-mirror" ]] && ARGS+=("$arg")
done
set -- "${ARGS[@]}"

COMPONENT="${1:-whisper}"

_curl_download() {
    local url="$1" dest="$2"
    curl -L --connect-timeout 30 --max-time 1800 --retry 5 --retry-delay 10 -C - \
        "$url" -o "$dest"
}

# ── Whisper 模型 ──────────────────────────────────────────────
download_whisper() {
    local size="${1:-medium}"
    local model_dir="$SCRIPT_DIR/models/faster-whisper-$size"
    mkdir -p "$model_dir"

    # 非 Systran 官方 repo 的模型映射（使用社区 int8 量化 / CTranslate2 转换版本）
    # medium: rhasspy int8 量化版（749MB vs Systran float16 1.5GB），精度几乎无损
    # large-v3-turbo: deepdml CTranslate2 转换版（Systran 未发布 turbo）
    if [ "$size" = "medium" ]; then
        local repo="rhasspy/faster-whisper-medium-int8"
    elif [ "$size" = "large-v3-turbo" ]; then
        local repo="deepdml/faster-whisper-large-v3-turbo-ct2"
    else
        local repo="Systran/faster-whisper-$size"
    fi

    if $USE_MIRROR; then
        local base="https://hf-mirror.com/$repo/resolve/main"
    else
        local base="https://huggingface.co/$repo/resolve/main"
    fi

    echo "📥 下载 faster-whisper-$size 模型 ($repo)..."
    echo "   目标目录: $model_dir"

    # model.bin + vocabulary.txt 从原 repo 下载
    for f in vocabulary.txt model.bin; do
        if [ -f "$model_dir/$f" ]; then
            echo "  ⏭  $f 已存在，跳过"
            continue
        fi
        echo "  📦 下载 $f ..."
        _curl_download "$base/$f" "$model_dir/$f"
        echo "  ✅ $f 下载完成 ($(du -h "$model_dir/$f" | cut -f1))"
    done

    # config.json + tokenizer.json: 从 Systran 官方 repo 下载
    # int8 量化版 config.json 缺少 alignment_heads 字段（word_timestamps 必需）
    # int8 量化版无 tokenizer.json（缺失会导致运行时连 HuggingFace 下载，国内超时）
    if $USE_MIRROR; then
        local systran_base="https://hf-mirror.com/Systran/faster-whisper-$size/resolve/main"
    else
        local systran_base="https://huggingface.co/Systran/faster-whisper-$size/resolve/main"
    fi
    for f in config.json tokenizer.json; do
        if [ -f "$model_dir/$f" ]; then
            echo "  ⏭  $f 已存在，跳过"
            continue
        fi
        echo "  📦 下载 $f (从 Systran 官方 repo)..."
        _curl_download "$systran_base/$f" "$model_dir/$f"
        echo "  ✅ $f 下载完成 ($(du -h "$model_dir/$f" | cut -f1))"
    done

    echo "🎉 Whisper 模型下载完成: $model_dir"
    ls -lh "$model_dir/"
}

# ── Piper TTS 中文模型 ───────────────────────────────────────
# 官方: https://huggingface.co/rhasspy/piper-voices/tree/main/zh/zh_CN
# 模型: zh_CN-huayan-medium（~70MB，普通话女声，质量最佳）
# 可选: zh_CN-huayan-medium / zh_CN-chaowen-medium / zh_CN-xiao_ya-medium
download_piper() {
    local voice="${1:-huayan}"
    local quality="${2:-medium}"
    local model_name="zh_CN-${voice}-${quality}"
    local model_dir="$SCRIPT_DIR/models/piper"
    mkdir -p "$model_dir"

    if $USE_MIRROR; then
        local base="https://hf-mirror.com/rhasspy/piper-voices/resolve/main/zh/zh_CN/${voice}/${quality}"
    else
        local base="https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/${voice}/${quality}"
    fi

    echo "📥 下载 Piper TTS 模型: $model_name ..."
    echo "   官方仓库: https://huggingface.co/rhasspy/piper-voices"
    echo "   目标目录: $model_dir"

    # 模型文件 (.onnx) + 配置文件 (.onnx.json)
    for f in "${model_name}.onnx" "${model_name}.onnx.json"; do
        if [ -f "$model_dir/$f" ]; then
            echo "  ⏭  $f 已存在，跳过"
            continue
        fi
        echo "  📦 下载 $f ..."
        _curl_download "$base/$f" "$model_dir/$f"
        echo "  ✅ $f 下载完成 ($(du -h "$model_dir/$f" | cut -f1))"
    done

    echo "🎉 Piper 模型下载完成: $model_dir/$model_name.onnx"
    echo ""
    echo "   配置 config.json 使用方法:"
    echo '   "tts_chain": ["piper", "edge-tts", "pyttsx3"],'
    echo '   "piper": { "model_path": "models/piper/'"$model_name"'.onnx" }'
    echo ""
    echo "   可选中文语音: huayan（女声）/ chaowen / xiao_ya"
    echo "   下载其他语音: bash download_model.sh piper chaowen"
}

# ── sherpa-onnx MeloTTS 中文模型 ─────────────────────────────
# 官方: https://github.com/k2-fsa/sherpa-onnx/releases (tts-models)
# 模型: vits-melo-tts-zh_en（~110MB，中英混合，质量优秀）
download_sherpa() {
    local model_name="vits-melo-tts-zh_en"
    local model_dir="$SCRIPT_DIR/models/sherpa-onnx"
    local tar_file="$model_dir/${model_name}.tar.bz2"
    mkdir -p "$model_dir"

    # sherpa-onnx 模型托管在 GitHub Releases
    local official_url="https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/${model_name}.tar.bz2"
    # 国内 GitHub 加速镜像
    local mirror_url="https://ghfast.top/https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/${model_name}.tar.bz2"

    echo "📥 下载 sherpa-onnx MeloTTS 中文模型: $model_name ..."
    echo "   官方仓库: https://github.com/k2-fsa/sherpa-onnx"
    echo "   目标目录: $model_dir"

    # 如果已解压则跳过
    if [ -f "$model_dir/$model_name/model.onnx" ]; then
        echo "  ⏭  模型已存在，跳过"
        echo "🎉 sherpa-onnx 模型位置: $model_dir/$model_name/"
        return 0
    fi

    echo "  📦 下载 ${model_name}.tar.bz2 ..."
    if $USE_MIRROR; then
        echo "  🌐 尝试国内镜像..."
        _curl_download "$mirror_url" "$tar_file" 2>/dev/null || {
            echo "  ⚠️  镜像下载失败，尝试官方地址..."
            _curl_download "$official_url" "$tar_file"
        }
    else
        _curl_download "$official_url" "$tar_file"
    fi

    echo "  📦 解压..."
    tar -xjf "$tar_file" -C "$model_dir"
    rm -f "$tar_file"

    echo "🎉 sherpa-onnx 模型下载完成: $model_dir/$model_name/"
    echo ""
    echo "   配置 config.json 使用方法:"
    echo '   "tts_chain": ["sherpa-onnx", "edge-tts", "pyttsx3"],'
    echo '   "sherpa_onnx": {'
    echo '     "model": "models/sherpa-onnx/'"$model_name"'/model.onnx",'
    echo '     "lexicon": "models/sherpa-onnx/'"$model_name"'/lexicon.txt",'
    echo '     "tokens": "models/sherpa-onnx/'"$model_name"'/tokens.txt"'
    echo '   }'
    ls -lh "$model_dir/$model_name/"
}


# ── NLLB 翻译模型 ────────────────────────────────────────────
download_nllb() {
    local model_dir="$SCRIPT_DIR/models/nllb-200-distilled-600M-ct2-int8"
    mkdir -p "$model_dir"

    if $USE_MIRROR; then
        local base="https://hf-mirror.com/JustFrederik/nllb-200-distilled-600M-ct2-int8/resolve/main"
    else
        local base="https://huggingface.co/JustFrederik/nllb-200-distilled-600M-ct2-int8/resolve/main"
    fi

    echo "📥 下载 NLLB-200 翻译模型 (int8, ~618MB)..."
    echo "   目标目录: $model_dir"

    local files="model.bin sentencepiece.bpe.model config.json shared_vocabulary.txt tokenizer.json"
    for f in $files; do
        if [ -f "$model_dir/$f" ]; then
            echo "  ⏭  $f 已存在，跳过"
            continue
        fi
        echo "  📦 下载 $f..."
        _curl_download "$base/$f" "$model_dir/$f"
        echo "  ✅ $f ($(du -h "$model_dir/$f" | cut -f1))"
    done

    echo "🎉 NLLB 翻译模型下载完成: $model_dir"
    ls -lh "$model_dir/"
}


# ── VITS 中文男声 TTS 模型 ───────────────────────────────────
download_vits_male() {
    local model_dir="$SCRIPT_DIR/models/vits-zh-hf-fanchen-wnj"
    mkdir -p "$model_dir/dict/pos_dict"

    if $USE_MIRROR; then
        local base="https://hf-mirror.com/csukuangfj/vits-zh-hf-fanchen-wnj/resolve/main"
    else
        local base="https://huggingface.co/csukuangfj/vits-zh-hf-fanchen-wnj/resolve/main"
    fi

    echo "📥 下载 VITS 中文男声模型 (fanchen-wnj, 16kHz, ~115MB)..."
    echo "   目标目录: $model_dir"

    # 主文件
    for f in vits-zh-hf-fanchen-wnj.onnx lexicon.txt tokens.txt date.fst number.fst phone.fst new_heteronym.fst; do
        if [ -f "$model_dir/$f" ]; then
            echo "  ⏭  $f 已存在，跳过"
            continue
        fi
        echo "  📦 下载 $f..."
        _curl_download "$base/$f" "$model_dir/$f"
        echo "  ✅ $f ($(du -h "$model_dir/$f" | cut -f1))"
    done

    # dict 目录
    for f in dict/hmm_model.utf8 dict/idf.utf8 dict/jieba.dict.utf8 dict/stop_words.utf8 dict/user.dict.utf8 dict/pos_dict/char_state_tab.utf8 dict/pos_dict/prob_emit.utf8 dict/pos_dict/prob_start.utf8 dict/pos_dict/prob_trans.utf8; do
        if [ -f "$model_dir/$f" ]; then
            echo "  ⏭  $f 已存在，跳过"
            continue
        fi
        echo "  📦 下载 $f..."
        _curl_download "$base/$f" "$model_dir/$f"
    done

    echo "🎉 VITS 男声模型下载完成: $model_dir"
    ls -lh "$model_dir/"
}


# ── 入口 ─────────────────────────────────────────────────────
case "$COMPONENT" in
    whisper)
        download_whisper "${2:-medium}"
        ;;
    piper)
        download_piper "${2:-huayan}" "${3:-medium}"
        ;;
    sherpa|sherpa-onnx)
        download_sherpa
        ;;
    nllb)
        download_nllb
        ;;
    vits-male)
        download_vits_male
        ;;
    all)
        download_whisper "${2:-medium}"
        echo ""
        download_piper
        echo ""
        download_sherpa
        echo ""
        download_nllb
        echo ""
        download_vits_male
        ;;
    tiny|base|small|medium|large-v3-turbo)
        # 兼容旧用法: bash download_model.sh small
        download_whisper "$COMPONENT"
        ;;
    *)
        echo "用法:"
        echo "  bash download_model.sh whisper [tiny|base|small|medium|large-v3-turbo]"
        echo "                                  # Whisper 语音识别模型（默认 medium）"
        echo "  bash download_model.sh piper [huayan|chaowen|xiao_ya]   # Piper TTS 中文模型（CPU）"
        echo "  bash download_model.sh sherpa                           # sherpa-onnx MeloTTS 中文模型（CPU）"
        echo "  bash download_model.sh nllb                             # NLLB-200 本地翻译模型（CPU）"
        echo "  bash download_model.sh vits-male                        # VITS 中文男声 TTS（CPU, 16kHz）"
        echo "  bash download_model.sh all                              # 下载全部模型"
        echo ""
        echo "选项:"
        echo "  --no-mirror    使用官方源（海外环境推荐）"
        exit 1
        ;;
esac
