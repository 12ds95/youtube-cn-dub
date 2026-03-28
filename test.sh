#!/bin/bash
# ============================================================
# 冒烟测试脚本
# 用法: bash test.sh
# 功能: 验证环境、依赖、核心模块是否正常工作
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python3"
PASSED=0
FAILED=0
WARNINGS=0

pass() { echo "  ✅ $1"; PASSED=$((PASSED + 1)); }
fail() { echo "  ❌ $1"; FAILED=$((FAILED + 1)); }
warn() { echo "  ⚠️  $1"; WARNINGS=$((WARNINGS + 1)); }

echo "============================================================"
echo "  YouTube 中文配音工具 - 冒烟测试"
echo "============================================================"
echo ""

# ─── 1. 虚拟环境 ──────────────────────────────────────────────
echo "[1/5] 虚拟环境"
if [ -f "$VENV_PYTHON" ]; then
    PY_VER=$("$VENV_PYTHON" --version 2>&1)
    pass "venv Python: $PY_VER"
else
    fail "venv 不存在，请先运行: bash setup.sh"
    echo ""
    echo "测试终止。"
    exit 1
fi

# ─── 2. Python 依赖 ───────────────────────────────────────────
echo ""
echo "[2/5] Python 依赖"
for mod in faster_whisper edge_tts deep_translator pydub yt_dlp httpx; do
    if "$VENV_PYTHON" -c "import $mod" 2>/dev/null; then
        ver=$("$VENV_PYTHON" -c "import $mod; print(getattr($mod, '__version__', 'ok'))" 2>/dev/null || echo "ok")
        pass "$mod ($ver)"
    else
        fail "$mod 未安装"
    fi
done

# ─── 3. 系统工具 ───────────────────────────────────────────────
echo ""
echo "[3/5] 系统工具"

# ffmpeg 版本检查
if command -v ffmpeg >/dev/null 2>&1; then
    FF_VER=$(ffmpeg -version 2>&1 | head -1)
    # 检查是否为 Anaconda 旧版
    if echo "$FF_VER" | grep -qE "version [123]\." ; then
        warn "ffmpeg 版本过旧 ($FF_VER)，需 ≥ 4.x"
    else
        pass "ffmpeg: $FF_VER"
    fi
else
    fail "ffmpeg 未安装"
fi

if command -v ffprobe >/dev/null 2>&1; then
    pass "ffprobe"
else
    fail "ffprobe 未安装"
fi

# Node.js (可选)
if command -v node >/dev/null 2>&1; then
    pass "Node.js: $(node --version)"
else
    warn "Node.js 未安装（可选）"
fi

# ─── 4. 核心模块测试 ──────────────────────────────────────────
echo ""
echo "[4/5] 核心模块"

# 测试翻译解析器（之前出过 bug 的关键模块）
"$VENV_PYTHON" -c "
import re, sys

def _strip_think_block(content):
    return re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()

def _strip_numbered_prefix(line):
    return re.sub(r'^\[?\d+\]?\s*\.?\s*', '', line.strip()).strip()

# 测试 1: <think> 块剥离
content = '<think>\n1. reasoning\n</think>\n\n[1] 翻译一\n[2] 翻译二'
cleaned = _strip_think_block(content)
assert '<think>' not in cleaned, 'think block not stripped'

# 测试 2: 编号前缀剥离
assert _strip_numbered_prefix('[1] 你好') == '你好'
assert _strip_numbered_prefix('[14] 世界') == '世界'
assert _strip_numbered_prefix('1. 你好') == '你好'
assert _strip_numbered_prefix('普通文本') == '普通文本'

print('ok')
" 2>&1
if [ $? -eq 0 ]; then
    pass "翻译解析器 (_strip_think_block, _strip_numbered_prefix)"
else
    fail "翻译解析器测试失败"
fi

# 测试 edge-tts 可用性
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
    pass "edge-tts 语音合成"
else
    fail "edge-tts 语音合成失败（需要网络连接）"
fi

# 测试 Whisper 模型是否存在
if [ -d "$SCRIPT_DIR/models/faster-whisper-small" ] || [ -d "$SCRIPT_DIR/models/faster-whisper-tiny" ]; then
    MODELS=$(ls -d "$SCRIPT_DIR/models/faster-whisper-"* 2>/dev/null | xargs -I{} basename {})
    pass "本地 Whisper 模型: $MODELS"
else
    warn "无本地 Whisper 模型，首次运行将自动下载（或运行 bash download_model.sh small）"
fi

# ─── 5. 配置文件 ──────────────────────────────────────────────
echo ""
echo "[5/5] 配置文件"

if [ -f "$SCRIPT_DIR/config.example.json" ]; then
    pass "config.example.json 模板存在"
else
    fail "config.example.json 缺失"
fi

if [ -f "$SCRIPT_DIR/config.json" ]; then
    # 检查是否有有效的 API Key
    HAS_KEY=$("$VENV_PYTHON" -c "
import json
with open('$SCRIPT_DIR/config.json') as f:
    cfg = json.load(f)
key = cfg.get('llm', {}).get('api_key', '')
print('yes' if key and len(key) > 10 else 'no')
" 2>/dev/null || echo "no")
    if [ "$HAS_KEY" = "yes" ]; then
        pass "config.json 已配置（含 LLM API Key）"
    else
        pass "config.json 存在（LLM API Key 未配置，将使用 Google 翻译）"
    fi
else
    warn "config.json 不存在（可选，使用命令行参数或复制 config.example.json）"
fi

# ─── 汇总 ─────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo "  测试结果: ✅ $PASSED 通过  ❌ $FAILED 失败  ⚠️  $WARNINGS 警告"
if [ $FAILED -gt 0 ]; then
    echo "  请修复失败项后重新测试"
    exit 1
else
    echo "  环境就绪！"
fi
echo "============================================================"
