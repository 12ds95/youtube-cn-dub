#!/usr/bin/env bash
# 集成测试脚本: 对 10 个视频执行全功能管线测试
# 全功能开启: two_pass, nlp_segmentation, post_tts_calibration, gap_borrowing, video_slowdown
# 用途: 收集 TTS 时长校准数据 + 回归测试
# 覆盖领域: 微积分、计算机科学、微分方程、神经网络、分析、概率

set -euo pipefail
cd "$(dirname "$0")"

# ctrl+c / 异常退出时清理临时配置文件
trap 'rm -f /tmp/test_*_*.json' EXIT

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# 从 config.json 读取 LLM 配置（避免硬编码 API Key）
if [ ! -f "config.json" ]; then
    echo -e "${RED}❌ config.json 不存在，请先从 config.example.json 复制并配置${NC}"
    exit 1
fi
LLM_API_URL=$(python3 -c "import json; c=json.load(open('config.json')); print(c.get('llm',{}).get('api_url',''))")
LLM_API_KEY=$(python3 -c "import json; c=json.load(open('config.json')); print(c.get('llm',{}).get('api_key',''))")
LLM_MODEL=$(python3 -c "import json; c=json.load(open('config.json')); print(c.get('llm',{}).get('model',''))")
if [ -z "$LLM_API_KEY" ]; then
    echo -e "${RED}❌ config.json 中 llm.api_key 为空${NC}"
    exit 1
fi

VIDEOS=(
    "d4EgbgTm0Bg"
    "kCc8FmEb1nY"
    "zjMuIxRvygQ"
)

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

    # 判断 skip_steps
    SKIP_STEPS='["download", "extract", "separate"]'
    if [ -f "$VIDEO_DIR/transcribe_cache.json" ]; then
        SKIP_STEPS='["download", "extract", "separate", "transcribe"]'
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
    "two_pass": true,
    "isometric": 3,
    "isometric_cps_threshold": 5.5
  },
  "nlp_segmentation": true,
  "tts_chain": ["edge-tts", "piper", "gtts", "pyttsx3"],
  "piper": {
    "model_path": "models/piper/zh_CN-huayan-medium.onnx"
  },
  "skip_steps": $SKIP_STEPS,
  "refine": {
    "enabled": true,
    "max_iterations": 20,
    "speed_threshold": 1.25,
    "post_tts_calibration": true,
    "calibration_threshold": 1.30
  },
  "alignment": {
    "gap_borrowing": true,
    "max_borrow_ms": 300,
    "video_slowdown": true,
    "max_slowdown_factor": 0.85
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
    echo "   功能: two_pass, nlp_segmentation, isometric, post_tts_calibration, gap_borrowing, video_slowdown"
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

    # 验证输出
    echo ""
    echo -e "${YELLOW}📋 输出验证 ($VIDEO_ID):${NC}"
    local PASS=true
    for f in final.mp4 chinese_dub.wav segments_cache.json subtitle_bilingual.srt subtitle_zh.srt subtitle_en.srt audit/speed_report.json; do
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
echo -e "${YELLOW}🧪 10 视频集成测试 (全功能开启)${NC}"
echo "   视频: ${VIDEOS[*]}"
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
