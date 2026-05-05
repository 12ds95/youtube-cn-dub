#!/usr/bin/env bash
# 端到端管线测试脚本 — 用最小视频反复验证完整流程
# 跳过: download, extract, separate (保留已有的 original.mp4, audio.wav, 分离音频)
#
# 用法:
#   bash test_pipeline.sh              # 快速测试: 跳过 transcribe+translate，复用缓存 (默认)
#   bash test_pipeline.sh --integrated # 集成测试: 全功能开启，跳过 transcribe（用缓存）
#   bash test_pipeline.sh --baseline   # 回归测试: 全功能关闭，验证不引入回归
#   bash test_pipeline.sh --fast       # 快速测试 (跳过 transcribe+translate，复用缓存)
#   bash test_pipeline.sh --refine     # 仅测翻译+迭代优化 (跳过 TTS/字幕/合成)
#   bash test_pipeline.sh --retranslate # 删除翻译缓存从翻译开始，跳过 TTS 后步骤

set -euo pipefail
cd "$(dirname "$0")"

VIDEO_ID="zjMuIxRvygQ"
VIDEO_DIR="output/$VIDEO_ID"

# 颜色
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

MODE="fast"
case "${1:-}" in
    --full)        MODE="full" ;;
    --integrated)  MODE="integrated" ;;
    --baseline)    MODE="baseline" ;;
    --fast)        MODE="fast" ;;
    --refine)      MODE="refine" ;;
    --retranslate) MODE="retranslate" ;;
esac

echo -e "${YELLOW}🧪 管线端到端测试${NC}"
echo "   视频: $VIDEO_ID (Quaternions and 3d rotation, 359s)"
echo "   目录: $VIDEO_DIR"
echo "   模式: $MODE"
echo ""

# --- 清理上次生成的文件 ---
echo -e "${YELLOW}🧹 清理旧的生成文件...${NC}"

# 所有模式都清理的文件
rm -f  "$VIDEO_DIR/chinese_dub.wav"
rm -f  "$VIDEO_DIR/final.mp4"
rm -f  "$VIDEO_DIR"/subtitle_*.srt
rm -rf "$VIDEO_DIR/tts_segments"
rm -rf "$VIDEO_DIR/audit"

case "$MODE" in
    full|baseline)
        rm -f "$VIDEO_DIR/segments_cache.json"
        rm -f "$VIDEO_DIR/transcribe_cache.json"
        echo "   $MODE 模式: 清理所有缓存 (从 transcribe 开始)"
        ;;
    integrated)
        rm -f "$VIDEO_DIR/segments_cache.json"
        echo "   --integrated 模式: 保留 transcribe_cache，清理翻译缓存 (从翻译开始，全功能)"
        ;;
    fast)
        echo "   --fast 模式: 保留 segments_cache.json (跳过 transcribe + translate)"
        ;;
    refine)
        echo "   --refine 模式: 保留 segments_cache.json，仅运行迭代优化 (跳过 TTS 后步骤)"
        ;;
    retranslate)
        rm -f "$VIDEO_DIR/segments_cache.json"
        echo "   --retranslate 模式: 删除翻译缓存，从翻译开始 (跳过 TTS 后步骤)"
        ;;
esac

echo "   ✅ 清理完成"
echo ""

# --- 构建临时配置 ---
SKIP_STEPS='["download", "extract", "separate"]'

case "$MODE" in
    full|baseline)
        SKIP_STEPS='["download", "extract", "separate"]'
        ;;
    integrated)
        SKIP_STEPS='["download", "extract", "separate", "transcribe"]'
        ;;
    fast)
        SKIP_STEPS='["download", "extract", "separate", "transcribe", "translate"]'
        ;;
    refine)
        SKIP_STEPS='["download", "extract", "separate", "transcribe", "translate", "tts", "subtitle", "merge"]'
        ;;
    retranslate)
        SKIP_STEPS='["download", "extract", "separate", "transcribe", "tts", "subtitle", "merge"]'
        ;;
esac

TMPCONFIG=$(mktemp /tmp/test_pipeline_XXXXXX.json)
trap "rm -f '$TMPCONFIG'" EXIT

# 新功能开关: baseline 模式全部关闭，其他模式全部开启
if [ "$MODE" = "baseline" ]; then
    TWO_PASS=false
    NLP_SEG=false
    POST_CAL=false
    GAP_BORROW=false
    VID_SLOW=false
    ATEMPO_DISABLED=false
    FEEDBACK_LOOP=false
    FEATURE_DESC="全部关闭（回归基线）"
else
    TWO_PASS=true
    NLP_SEG=true
    POST_CAL=true
    GAP_BORROW=true
    VID_SLOW=true
    ATEMPO_DISABLED=true
    FEEDBACK_LOOP=true
    FEATURE_DESC="two_pass, nlp_segmentation, post_tts_calibration, gap_borrowing, video_slowdown, atempo_disabled, feedback_loop, isometric=3"
fi

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
    "two_pass": $TWO_PASS,
    "isometric": 3
  },
  "nlp_segmentation": $NLP_SEG,
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
    "enabled": false,
    "max_iterations": 3,
    "speed_threshold": 1.5,
    "post_tts_calibration": $POST_CAL,
    "calibration_threshold": 1.30
  },
  "alignment": {
    "atempo_disabled": $ATEMPO_DISABLED,
    "feedback_loop": $FEEDBACK_LOOP,
    "gap_borrowing": $GAP_BORROW,
    "max_borrow_ms": 300,
    "video_slowdown": $VID_SLOW,
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
echo "   功能: $FEATURE_DESC"
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

if [ "$MODE" = "refine" ] || [ "$MODE" = "retranslate" ]; then
    # 仅验证翻译相关文件
    check_file "$VIDEO_DIR/segments_cache.json" "segments_cache.json (翻译缓存)"

    # 分析翻译质量
    echo ""
    echo -e "${YELLOW}📊 翻译质量分析:${NC}"
    VENV_PYTHON="venv/bin/python3"
    if [ ! -f "$VENV_PYTHON" ]; then
        VENV_PYTHON="python3"
    fi

    "$VENV_PYTHON" -c "
import json, sys

with open('$VIDEO_DIR/segments_cache.json') as f:
    segs = json.load(f)

total = len(segs)
issues = []

for i, seg in enumerate(segs):
    en = seg.get('text_en', '')
    zh = seg.get('text_zh', '')
    en_len = len(en)
    zh_chars = sum(1 for c in zh if '\u4e00' <= c <= '\u9fff')

    # 检查中文过长（超过英文字符数）
    if zh_chars > en_len * 0.8:
        issues.append(f'  ⚠️  #{i} 中文过长 ({zh_chars}字/{en_len}英字): {zh[:40]}...')

    # 检查重复：当前段中文与其他段高度相似
    for j in range(max(0, i-3), i):
        other_zh = segs[j].get('text_zh', '')
        if len(zh) > 10 and len(other_zh) > 10:
            # 简单子串检测
            if zh in other_zh or other_zh in zh:
                issues.append(f'  ❌  #{i} 与 #{j} 翻译重复: {zh[:40]}...')

print(f'  总片段: {total}')
# 统计迭代变化（如果有 iter_0）
try:
    with open('$VIDEO_DIR/audit/iterations/iter_0_segments.json') as f:
        orig = json.load(f)
    changed = sum(1 for a, b in zip(orig, segs) if a.get('text_zh') != b.get('text_zh'))
    print(f'  迭代变更: {changed}/{total} 段')
except:
    pass

if issues:
    print(f'  发现 {len(issues)} 个问题:')
    for iss in issues[:10]:
        print(iss)
    if len(issues) > 10:
        print(f'  ... 共 {len(issues)} 个问题')
else:
    print(f'  ✅ 未发现明显翻译问题')
" || true

else
    # 完整验证
    check_file "$VIDEO_DIR/final.mp4"              "final.mp4 (最终视频)"
    check_file "$VIDEO_DIR/chinese_dub.wav"        "chinese_dub.wav (配音音轨)"
    check_file "$VIDEO_DIR/segments_cache.json"    "segments_cache.json (翻译缓存)"
    check_file "$VIDEO_DIR/subtitle_bilingual.srt" "subtitle_bilingual.srt (双语字幕)"
    check_file "$VIDEO_DIR/subtitle_zh.srt"        "subtitle_zh.srt (中文字幕)"
    check_file "$VIDEO_DIR/subtitle_en.srt"        "subtitle_en.srt (英文字幕)"
    check_file "$VIDEO_DIR/audit/speed_report.json"  "audit/speed_report.json (语速报告)"

    # 检查 TTS 片段目录
    TTS_COUNT=$(ls "$VIDEO_DIR/tts_segments"/*.mp3 2>/dev/null | wc -l | tr -d ' ')
    if [ "$TTS_COUNT" -gt 0 ]; then
        echo -e "  ${GREEN}✅${NC} tts_segments/ ($TTS_COUNT 个片段)"
    else
        echo -e "  ${RED}❌${NC} tts_segments/ — 无 TTS 片段"
        PASS=false
    fi

    # 自动质量评分
    echo ""
    echo -e "${YELLOW}📊 自动质量评分:${NC}"
    VENV_PYTHON="venv/bin/python3"
    if [ ! -f "$VENV_PYTHON" ]; then
        VENV_PYTHON="python3"
    fi
    "$VENV_PYTHON" score_videos.py "$VIDEO_DIR" --compare || true
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
