#!/usr/bin/env bash
# 端到端管线测试脚本 — 用最小视频反复验证完整流程
# 跳过: download, extract, separate (保留已有的 original.mp4, audio.wav, 分离音频)
# 运行: transcribe → translate → refine → tts → subtitle → merge
#
# 用法:
#   bash test_pipeline.sh            # 完整测试 (transcribe → merge)
#   bash test_pipeline.sh --fast     # 快速测试 (跳过 transcribe，复用 segments_cache)

set -euo pipefail
cd "$(dirname "$0")"

VIDEO_ID="zjMuIxRvygQ"
VIDEO_DIR="output/$VIDEO_ID"

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

if [ ! -d "$VIDEO_DIR" ]; then
    echo -e "${RED}❌ 测试视频目录不存在: $VIDEO_DIR${NC}"
    exit 1
fi

# 检查必要文件
for f in original.mp4 audio.wav info.json; do
    if [ ! -f "$VIDEO_DIR/$f" ]; then
        echo -e "${RED}❌ 缺少必要文件: $VIDEO_DIR/$f${NC}"
        exit 1
    fi
done

FAST_MODE=false
if [ "${1:-}" = "--fast" ]; then
    FAST_MODE=true
fi

echo -e "${YELLOW}🧪 管线端到端测试${NC}"
echo "   视频: $VIDEO_ID (Quaternions and 3d rotation, 359s)"
echo "   目录: $VIDEO_DIR"
echo ""

# --- 清理上次生成的文件 ---
echo -e "${YELLOW}🧹 清理旧的生成文件...${NC}"

# 保留: original.mp4, audio.wav, audio_vocals.wav, audio_accompaniment.wav, info.json
# 清理: 翻译、TTS、字幕、合成等所有中间产物
rm -f  "$VIDEO_DIR/chinese_dub.wav"
rm -f  "$VIDEO_DIR/final.mp4"
rm -f  "$VIDEO_DIR"/subtitle_*.srt
rm -f  "$VIDEO_DIR/speed_report.json"
rm -rf "$VIDEO_DIR/tts_segments"
rm -rf "$VIDEO_DIR/iterations"
rm -f  "$VIDEO_DIR"/pipeline_*.log

if [ "$FAST_MODE" = true ]; then
    echo "   --fast 模式: 保留 segments_cache.json (跳过 transcribe + translate)"
else
    rm -f "$VIDEO_DIR/segments_cache.json"
    rm -f "$VIDEO_DIR/transcribe_cache.json"
    echo "   完整模式: 清理所有缓存 (从 transcribe 开始)"
fi

echo "   ✅ 清理完成"
echo ""

# --- 构建临时配置 ---
SKIP_STEPS='["download", "extract", "separate"]'
if [ "$FAST_MODE" = true ]; then
    SKIP_STEPS='["download", "extract", "separate", "transcribe", "translate"]'
fi

TMPCONFIG=$(mktemp /tmp/test_pipeline_XXXXXX.json)
trap "rm -f '$TMPCONFIG'" EXIT

cat > "$TMPCONFIG" <<JSONEOF
{
  "resume_from": "$VIDEO_DIR",
  "voice": "zh-CN-YunxiNeural",
  "translator": "llm",
  "llm": {
    "api_url": "https://coding.dashscope.aliyuncs.com/v1/chat/completions",
    "api_key": "sk-sp-e0987ca0f8c04f969a5218dbdc6f1401",
    "model": "qwen3-coder-next",
    "batch_size": 15,
    "temperature": 0.3
  },
  "tts_chain": ["edge-tts", "piper", "gtts", "pyttsx3"],
  "piper": {
    "model_path": "models/piper/zh_CN-huayan-medium.onnx"
  },
  "skip_steps": $SKIP_STEPS,
  "refine": {
    "enabled": true,
    "max_iterations": 3,
    "speed_threshold": 1.25
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
echo "   refine: max_iterations=3"
echo ""

START_TIME=$(date +%s)

# 运行管线
bash run.sh --config "$TMPCONFIG"

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
MINUTES=$((ELAPSED / 60))
SECONDS=$((ELAPSED % 60))

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# --- 验证输出 ---
PASS=true

check_file() {
    local path="$1"
    local desc="$2"
    if [ -f "$path" ] && [ "$(stat -f%z "$path" 2>/dev/null)" -gt 0 ]; then
        local size
        size=$(stat -f%z "$path" | awk '{
            if ($1 >= 1048576) printf "%.1f MB", $1/1048576;
            else if ($1 >= 1024) printf "%.1f KB", $1/1024;
            else printf "%d B", $1;
        }')
        echo -e "  ${GREEN}✅${NC} $desc ($size)"
    else
        echo -e "  ${RED}❌${NC} $desc — 文件缺失或为空"
        PASS=false
    fi
}

echo -e "${YELLOW}📋 输出验证:${NC}"
check_file "$VIDEO_DIR/final.mp4"              "final.mp4 (最终视频)"
check_file "$VIDEO_DIR/chinese_dub.wav"        "chinese_dub.wav (配音音轨)"
check_file "$VIDEO_DIR/segments_cache.json"    "segments_cache.json (翻译缓存)"
check_file "$VIDEO_DIR/subtitle_bilingual.srt" "subtitle_bilingual.srt (双语字幕)"
check_file "$VIDEO_DIR/subtitle_zh.srt"        "subtitle_zh.srt (中文字幕)"
check_file "$VIDEO_DIR/subtitle_en.srt"        "subtitle_en.srt (英文字幕)"
check_file "$VIDEO_DIR/speed_report.json"      "speed_report.json (语速报告)"

# 检查 TTS 片段目录
TTS_COUNT=$(ls "$VIDEO_DIR/tts_segments"/*.mp3 2>/dev/null | wc -l | tr -d ' ')
if [ "$TTS_COUNT" -gt 0 ]; then
    echo -e "  ${GREEN}✅${NC} tts_segments/ ($TTS_COUNT 个片段)"
else
    echo -e "  ${RED}❌${NC} tts_segments/ — 无 TTS 片段"
    PASS=false
fi

echo ""
echo -e "  ⏱️  耗时: ${MINUTES}分${SECONDS}秒"
echo ""

if [ "$PASS" = true ]; then
    echo -e "${GREEN}🎉 管线测试通过!${NC}"
else
    echo -e "${RED}💥 管线测试失败 — 请检查上方缺失项${NC}"
    exit 1
fi
