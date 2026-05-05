#!/usr/bin/env bash
# 集成测试脚本: 对 output/ 下所有视频执行全功能管线测试
# 全功能开启: two_pass, nlp_segmentation, post_tts_calibration,
#   gap_borrowing, video_slowdown, atempo_disabled, feedback_loop
# 用途: 收集 TTS 时长校准数据 + 回归测试

set -euo pipefail
cd "$(dirname "$0")"

# ── 从 config.json 动态读取 LLM 凭据（禁止硬编码 API Key）──
if [ ! -f config.json ]; then
    echo "❌ config.json 不存在，请先复制 config.example.json 并填入 API Key"
    exit 1
fi
LLM_API_KEY=$(./venv/bin/python3 -c "import json; c=json.load(open('config.json')); print(c.get('llm',{}).get('api_key',''))")
LLM_API_URL=$(./venv/bin/python3 -c "import json; c=json.load(open('config.json')); print(c.get('llm',{}).get('api_url',''))")
LLM_MODEL=$(./venv/bin/python3 -c "import json; c=json.load(open('config.json')); print(c.get('llm',{}).get('model',''))")
if [ -z "$LLM_API_KEY" ]; then
    echo "❌ config.json 中 llm.api_key 为空"
    exit 1
fi

# ctrl+c / 异常退出时清理临时配置文件
trap 'rm -f /tmp/test_*_*.json' EXIT

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# 动态发现 output/ 下所有视频目录
# 判断逻辑: 含管线产物文件(transcribe_cache.json/original.mp4/audio_vocals.wav)的为视频目录
#           否则为分类目录，遍历其子目录
VIDEOS=()
for entry in output/*/; do
    entry="${entry%/}"          # 去尾部斜杠
    entry="${entry#output/}"    # 去 output/ 前缀
    if [ -f "output/$entry/transcribe_cache.json" ] || \
       [ -f "output/$entry/original.mp4" ] || \
       [ -f "output/$entry/audio_vocals.wav" ]; then
        # 独立视频目录(含管线产物)
        VIDEOS+=("$entry")
    else
        # 分类目录: 遍历其下每个子目录
        for sub in output/"$entry"/*/; do
            [ -d "$sub" ] || continue
            sub="${sub%/}"
            sub="${sub#output/}"
            VIDEOS+=("$sub")
        done
    fi
done

run_video() {
    local VIDEO_ID="$1"
    local VIDEO_DIR="output/$VIDEO_ID"

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo -e "${YELLOW}🧪 测试视频: $VIDEO_ID${NC}"
    echo "   目录: $VIDEO_DIR"

    if [ ! -d "$VIDEO_DIR" ]; then
        echo -e "${RED}❌ 目录不存在: $VIDEO_DIR${NC}"
        return 1
    fi

    # 判断 skip_steps (临时跳过合成视频阶段)
    SKIP_STEPS='["download", "extract", "separate", "subtitle", "merge"]'
    if [ -f "$VIDEO_DIR/transcribe_cache.json" ]; then
        SKIP_STEPS='["download", "extract", "separate", "transcribe", "subtitle", "merge"]'
        echo "   模式: 跳过 transcribe (有缓存)"
    else
        echo "   模式: 需要 transcribe (无缓存)"
    fi

    # 清理旧的生成文件 (保留 transcribe_cache, original.mp4, audio*.wav)
    rm -f  "$VIDEO_DIR/segments_cache.json"
    rm -f  "$VIDEO_DIR/chinese_dub.wav"
    rm -f  "$VIDEO_DIR/final.mp4"
    rm -f  "$VIDEO_DIR"/subtitle_*.srt
    rm -rf "$VIDEO_DIR/tts_segments"
    rm -rf "$VIDEO_DIR/audit"
    echo "   清理完成"

    # 清理上次残留的临时配置文件 (ctrl+c 后 mktemp 模板冲突)
    local SAFE_ID="${VIDEO_ID//\//_}"
    rm -f /tmp/test_${SAFE_ID}_*.json

    # 构建临时配置
    local TMPCONFIG
    TMPCONFIG=$(mktemp /tmp/test_${SAFE_ID}_XXXXXX.json)

    cat > "$TMPCONFIG" <<JSONEOF
{
  "resume_from": "$VIDEO_DIR",
  "voice": "zh-CN-YunxiNeural",
  "translator": "llm",
  "llm": {
    "api_url": "$LLM_API_URL",
    "api_key": "$LLM_API_KEY",
    "model": "$LLM_MODEL",
    "batch_size": 8,
    "temperature": 0.3,
    "two_pass": true
  },
  "nlp_segmentation": true,
  "tts_chain": ["edge-tts", "sherpa-onnx", "piper", "gtts", "pyttsx3"],
  "piper": {
    "model_path": "models/piper/zh_CN-huayan-medium.onnx"
  },
  "sherpa_onnx": {
    "model": "models/vits-zh-hf-fanchen-wnj/vits-zh-hf-fanchen-wnj.onnx",
    "lexicon": "models/vits-zh-hf-fanchen-wnj/lexicon.txt",
    "tokens": "models/vits-zh-hf-fanchen-wnj/tokens.txt",
    "dict_dir": "models/vits-zh-hf-fanchen-wnj/dict",
    "speaker_id": 0
  },
  "skip_steps": $SKIP_STEPS,
  "refine": {
    "enabled": true,
    "max_iterations": 5,
    "speed_threshold": 1.5,
    "post_tts_calibration": true,
    "calibration_threshold": 1.30
  },
  "alignment": {
    "gap_borrowing": true,
    "max_borrow_ms": 300,
    "video_slowdown": true,
    "max_slowdown_factor": 0.85,
    "atempo_disabled": true,
    "feedback_loop": true,
    "feedback_tolerance": 0.15
  },
  "audio_separation": {
    "enabled": true,
    "model": "htdemucs",
    "vocal_volume": 0.15,
    "bgm_volume": 1.0,
    "device": "auto"
  }
}
JSONEOF

    echo -e "${YELLOW}🚀 开始运行管线...${NC}"
    echo "   skip_steps: $SKIP_STEPS"
    echo "   功能: two_pass, nlp_segmentation, post_tts_calibration, gap_borrowing, video_slowdown, atempo_disabled, feedback_loop"
    echo ""

    local START_TIME
    START_TIME=$(date +%s)

    bash run.sh --config "$TMPCONFIG"

    local END_TIME ELAPSED MINUTES SECONDS
    END_TIME=$(date +%s)
    ELAPSED=$((END_TIME - START_TIME))
    MINUTES=$((ELAPSED / 60))
    SECONDS=$((ELAPSED % 60))

    rm -f "$TMPCONFIG"

    # 验证输出 (跳过 final.mp4 和字幕)
    echo ""
    echo -e "${YELLOW}📋 输出验证 ($VIDEO_ID):${NC}"
    local PASS=true
    for f in chinese_dub.wav segments_cache.json audit/speed_report.json; do
        if [ -f "$VIDEO_DIR/$f" ] && [ "$(stat -f%z "$VIDEO_DIR/$f" 2>/dev/null)" -gt 0 ]; then
            local size
            size=$(stat -f%z "$VIDEO_DIR/$f" | awk '{
                if ($1 >= 1048576) printf "%.1f MB", $1/1048576;
                else if ($1 >= 1024) printf "%.1f KB", $1/1024;
                else printf "%d B", $1;
            }')
            echo -e "  ${GREEN}✅${NC} $f ($size)"
        else
            echo -e "  ${RED}❌${NC} $f — 缺失或为空"
            PASS=false
        fi
    done

    local TTS_COUNT
    TTS_COUNT=$(ls "$VIDEO_DIR/tts_segments"/*.mp3 2>/dev/null | wc -l | tr -d ' ')
    if [ "$TTS_COUNT" -gt 0 ]; then
        echo -e "  ${GREEN}✅${NC} tts_segments/ ($TTS_COUNT 个片段)"
    else
        echo -e "  ${RED}❌${NC} tts_segments/ — 无 TTS 片段"
        PASS=false
    fi

    # 质量评分门禁
    echo ""
    echo -e "${YELLOW}📊 质量评分 ($VIDEO_ID):${NC}"
    ./venv/bin/python3 score_videos.py "$VIDEO_DIR" --gate
    if [ $? -ne 0 ]; then
        PASS=false
    fi

    echo ""
    echo -e "  ⏱️  耗时: ${MINUTES}分${SECONDS}秒"

    if [ "$PASS" = true ]; then
        echo -e "  ${GREEN}🎉 $VIDEO_ID 测试通过!${NC}"
        return 0
    else
        echo -e "  ${RED}💥 $VIDEO_ID 测试失败${NC}"
        return 1
    fi
}

# 主流程
echo -e "${YELLOW}🧪 ${#VIDEOS[@]} 视频集成测试 (全功能开启)${NC}"
echo "   视频数量: ${#VIDEOS[@]}"
echo ""

TOTAL_START=$(date +%s)
FAILED=0

for vid in "${VIDEOS[@]}"; do
    if ! run_video "$vid"; then
        FAILED=$((FAILED + 1))
    fi
done

TOTAL_END=$(date +%s)
TOTAL_ELAPSED=$((TOTAL_END - TOTAL_START))
TOTAL_MIN=$((TOTAL_ELAPSED / 60))
TOTAL_SEC=$((TOTAL_ELAPSED % 60))

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "  总耗时: ${TOTAL_MIN}分${TOTAL_SEC}秒"
if [ "$FAILED" -eq 0 ]; then
    echo -e "  ${GREEN}🎉 全部通过!${NC}"
else
    echo -e "  ${RED}💥 $FAILED 个视频失败${NC}"
    exit 1
fi
