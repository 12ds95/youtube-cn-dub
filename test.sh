#!/bin/bash
# ============================================================
# 测试入口脚本
# 用法: bash test.sh           # 运行全部测试（环境检查 + 单元测试）
#       bash test.sh smoke      # 仅环境冒烟测试
#       bash test.sh unit       # 仅单元测试
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python3"
MODE="${1:-all}"
PASSED=0
FAILED=0
WARNINGS=0

pass_test() { echo "  ✅ $1"; PASSED=$((PASSED + 1)); }
fail_test() { echo "  ❌ $1"; FAILED=$((FAILED + 1)); }
warn_test() { echo "  ⚠️  $1"; WARNINGS=$((WARNINGS + 1)); }

# ─── 环境冒烟测试 ────────────────────────────────────────────────
run_smoke() {
    echo "============================================================"
    echo "  环境冒烟测试"
    echo "============================================================"
    echo ""

    # 1. 虚拟环境
    echo "[1/5] 虚拟环境"
    if [ -f "$VENV_PYTHON" ]; then
        PY_VER=$("$VENV_PYTHON" --version 2>&1)
        pass_test "venv Python: $PY_VER"
    else
        fail_test "venv 不存在，请先运行: bash setup.sh"
        echo ""
        echo "测试终止。"
        exit 1
    fi

    # 2. Python 依赖
    echo ""
    echo "[2/5] Python 依赖"
    for mod in faster_whisper edge_tts deep_translator pydub yt_dlp yt_dlp_ejs httpx gtts pyttsx3 piper sherpa_onnx; do
        if "$VENV_PYTHON" -c "import $mod" 2>/dev/null; then
            ver=$("$VENV_PYTHON" -c "import $mod; print(getattr($mod, '__version__', 'ok'))" 2>/dev/null || echo "ok")
            pass_test "$mod ($ver)"
        else
            fail_test "$mod 未安装"
        fi
    done

    # 3. 系统工具
    echo ""
    echo "[3/5] 系统工具"
    if command -v ffmpeg >/dev/null 2>&1; then
        FF_VER=$(ffmpeg -version 2>&1 | head -1)
        if echo "$FF_VER" | grep -qE "version [123]\." ; then
            warn_test "ffmpeg 版本过旧 ($FF_VER)，需 ≥ 4.x"
        else
            pass_test "ffmpeg: $FF_VER"
        fi
    else
        fail_test "ffmpeg 未安装"
    fi

    if command -v ffprobe >/dev/null 2>&1; then
        pass_test "ffprobe"
    else
        fail_test "ffprobe 未安装"
    fi

    if command -v node >/dev/null 2>&1; then
        pass_test "Node.js: $(node --version)"
    else
        warn_test "Node.js 未安装（可选）"
    fi

    # 4. edge-tts 可用性
    echo ""
    echo "[4/5] 运行时检查"
    "$VENV_PYTHON" -c "
import asyncio, edge_tts, tempfile, os

async def test():
    f = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
    f.close()
    try:
        comm = edge_tts.Communicate('测试', 'zh-CN-YunxiNeural')
        await comm.save(f.name)
        size = os.path.getsize(f.name)
        assert size > 0, f'TTS output is empty ({size} bytes)'
        print('ok')
    finally:
        os.unlink(f.name)

asyncio.run(test())
" 2>&1
    if [ $? -eq 0 ]; then
        pass_test "edge-tts 语音合成"
    else
        fail_test "edge-tts 语音合成失败（需要网络连接）"
    fi

    if [ -d "$SCRIPT_DIR/models/faster-whisper-small" ] || [ -d "$SCRIPT_DIR/models/faster-whisper-tiny" ]; then
        MODELS=$(ls -d "$SCRIPT_DIR/models/faster-whisper-"* 2>/dev/null | xargs -I{} basename {})
        pass_test "本地 Whisper 模型: $MODELS"
    else
        warn_test "无本地 Whisper 模型，首次运行将自动下载（或运行 bash download_model.sh small）"
    fi

    # Piper 模型
    if [ -f "$SCRIPT_DIR/models/piper/zh_CN-huayan-medium.onnx" ]; then
        pass_test "Piper 中文模型: zh_CN-huayan-medium.onnx"
    else
        warn_test "Piper 模型未下载（可选，运行 bash download_model.sh piper）"
    fi

    # sherpa-onnx 模型
    if [ -f "$SCRIPT_DIR/models/sherpa-onnx/vits-melo-tts-zh_en/model.onnx" ]; then
        pass_test "sherpa-onnx 中文模型: vits-melo-tts-zh_en"
    else
        warn_test "sherpa-onnx 模型未下载（可选，运行 bash download_model.sh sherpa）"
    fi

    # 5. 配置文件
    echo ""
    echo "[5/5] 配置文件"
    if [ -f "$SCRIPT_DIR/config.example.json" ]; then
        pass_test "config.example.json 模板存在"
    else
        fail_test "config.example.json 缺失"
    fi

    if [ -f "$SCRIPT_DIR/config.json" ]; then
        HAS_KEY=$("$VENV_PYTHON" -c "
import json
with open('$SCRIPT_DIR/config.json') as f:
    cfg = json.load(f)
key = cfg.get('llm', {}).get('api_key', '')
print('yes' if key and len(key) > 10 else 'no')
" 2>/dev/null || echo "no")
        if [ "$HAS_KEY" = "yes" ]; then
            pass_test "config.json 已配置（含 LLM API Key）"
        else
            pass_test "config.json 存在（LLM API Key 未配置，将使用 Google 翻译）"
        fi
    else
        warn_test "config.json 不存在（可选，使用命令行参数或复制 config.example.json）"
    fi
}

# ─── 单元测试 ────────────────────────────────────────────────────
run_unit() {
    echo "============================================================"
    echo "  单元测试 (tests/)"
    echo "============================================================"
    echo ""

    TEST_FAILED=0
    for test_file in "$SCRIPT_DIR"/tests/test_*.py; do
        test_name=$(basename "$test_file" .py)
        echo "--- $test_name ---"
        if "$VENV_PYTHON" "$test_file" 2>&1; then
            pass_test "$test_name"
        else
            fail_test "$test_name"
            TEST_FAILED=$((TEST_FAILED + 1))
        fi
        echo ""
    done

    if [ $TEST_FAILED -gt 0 ]; then
        echo "  ⚠️  $TEST_FAILED 个测试文件失败"
    fi
}

# ─── 主入口 ──────────────────────────────────────────────────────
echo ""

case "$MODE" in
    smoke)
        run_smoke
        ;;
    unit)
        run_unit
        ;;
    all|*)
        run_smoke
        echo ""
        run_unit
        ;;
esac

# ─── 汇总 ─────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo "  测试结果: ✅ $PASSED 通过  ❌ $FAILED 失败  ⚠️  $WARNINGS 警告"
if [ $FAILED -gt 0 ]; then
    echo "  请修复失败项后重新测试"
    exit 1
else
    echo "  全部通过！"
fi
echo "============================================================"
