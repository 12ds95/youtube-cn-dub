#!/usr/bin/env python3
"""
TTS 引擎冒烟测试 — 真实合成验证。
每个引擎调用一次 synthesize()，检查产出文件存在且 > 0 字节。

运行：python3 tests/test_tts_smoke.py
  - 在线引擎(edge-tts, gtts)需要网络
  - 本地引擎(piper, sherpa-onnx)需要已下载模型
  - pyttsx3 需要系统中文语音包
  - siliconflow 需要有效 API Key（无 key 则跳过）
  - cosyvoice 需要 GPU + 本地部署（默认跳过）
"""
import sys
import os
import asyncio
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_TEXT = "这是一段中文语音合成的冒烟测试。"


def _run(coro):
    """同步执行协程"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# 网络瞬时故障关键词 — 命中则 skip 而非 fail
_NETWORK_ERR_KEYWORDS = (
    "timeout", "timed out", "connection reset", "connection refused",
    "connection error", "connectionerror", "remote disconnected",
    "network is unreachable", "name or service not known",
    "no route to host", "ssl", "eof occurred",
)


def _run_network(coro, engine_label: str):
    """执行远程引擎协程，网络瞬时故障自动 skip"""
    try:
        return _run(coro)
    except Exception as e:
        msg = str(e).lower()
        if any(kw in msg for kw in _NETWORK_ERR_KEYWORDS):
            raise unittest_skip(f"{engine_label} 网络不可用: {e}")
        raise


# ── edge-tts ──

def test_edge_tts_real_synthesis():
    """edge-tts 真实合成：生成 mp3 文件且 > 0 字节"""
    from pipeline import EdgeTTSEngine
    engine = EdgeTTSEngine()
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        path = f.name
    try:
        _run_network(engine.synthesize(TEST_TEXT, path, "zh-CN-YunxiNeural"), "edge-tts")
        size = os.path.getsize(path)
        assert size > 100, f"edge-tts 产出文件太小: {size} bytes"
    finally:
        if os.path.exists(path):
            os.unlink(path)


# ── gtts ──

def test_gtts_real_synthesis():
    """gTTS 真实合成：生成 mp3 文件且 > 0 字节"""
    from pipeline import GTTSEngine
    engine = GTTSEngine()
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        path = f.name
    try:
        voice = engine.resolve_voice("zh-CN-YunxiNeural")
        _run_network(engine.synthesize(TEST_TEXT, path, voice), "gtts")
        size = os.path.getsize(path)
        assert size > 100, f"gtts 产出文件太小: {size} bytes"
    finally:
        if os.path.exists(path):
            os.unlink(path)


# ── pyttsx3 ──

def test_pyttsx3_real_synthesis():
    """pyttsx3 真实合成：生成 mp3 文件且 > 0 字节"""
    from pipeline import Pyttsx3Engine
    engine = Pyttsx3Engine()
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        path = f.name
    try:
        voice = engine.resolve_voice("zh-CN-YunxiNeural")
        _run(engine.synthesize(TEST_TEXT, path, voice))
        size = os.path.getsize(path)
        assert size > 100, f"pyttsx3 产出文件太小: {size} bytes"
    finally:
        if os.path.exists(path):
            os.unlink(path)


# ── piper ──

def test_piper_real_synthesis():
    """Piper 真实合成（需已下载模型）：生成 mp3 文件且 > 0 字节"""
    model_path = os.path.join(PROJECT_ROOT, "models/piper/zh_CN-huayan-medium.onnx")
    if not os.path.exists(model_path):
        raise unittest_skip(f"Piper 模型不存在: {model_path}，跳过")
    from pipeline import PiperTTSEngine
    engine = PiperTTSEngine(model_path=model_path)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        path = f.name
    try:
        voice = engine.resolve_voice("zh-CN-YunxiNeural")
        _run(engine.synthesize(TEST_TEXT, path, voice))
        size = os.path.getsize(path)
        assert size > 100, f"piper 产出文件太小: {size} bytes"
    finally:
        if os.path.exists(path):
            os.unlink(path)


# ── sherpa-onnx ──

def test_sherpa_onnx_real_synthesis():
    """sherpa-onnx 真实合成（需已下载模型）：生成 mp3 文件且 > 0 字节"""
    model_dir = os.path.join(PROJECT_ROOT, "models/sherpa-onnx/vits-melo-tts-zh_en")
    model_file = os.path.join(model_dir, "model.onnx")
    if not os.path.exists(model_file):
        raise unittest_skip(f"sherpa-onnx 模型不存在: {model_file}，跳过")
    from pipeline import SherpaOnnxEngine
    model_config = {
        "model": model_file,
        "lexicon": os.path.join(model_dir, "lexicon.txt"),
        "tokens": os.path.join(model_dir, "tokens.txt"),
        "dict_dir": os.path.join(model_dir, "dict"),
        "speaker_id": 0,
    }
    engine = SherpaOnnxEngine(model_config=model_config)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        path = f.name
    try:
        voice = engine.resolve_voice("zh-CN-YunxiNeural")
        _run(engine.synthesize(TEST_TEXT, path, voice))
        size = os.path.getsize(path)
        assert size > 100, f"sherpa-onnx 产出文件太小: {size} bytes"
    finally:
        if os.path.exists(path):
            os.unlink(path)


# ── siliconflow ──

def test_siliconflow_real_synthesis():
    """SiliconFlow 真实合成（需有效 API Key + 余额）：生成 mp3 文件且 > 0 字节"""
    import json
    config_path = os.path.join(PROJECT_ROOT, "config.json")
    api_key = ""
    if os.path.exists(config_path):
        with open(config_path) as f:
            cfg = json.load(f)
        api_key = cfg.get("siliconflow", {}).get("api_key", "")
    if not api_key or len(api_key) < 10:
        raise unittest_skip("SiliconFlow API Key 未配置，跳过")
    from pipeline import SiliconFlowTTSEngine
    engine = SiliconFlowTTSEngine(api_key=api_key)
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        path = f.name
    try:
        voice = engine.resolve_voice("zh-CN-YunxiNeural")
        try:
            _run_network(engine.synthesize(TEST_TEXT, path, voice), "siliconflow")
        except unittest_skip:
            raise  # 网络问题已被 _run_network 处理为 skip
        except Exception as e:
            err_msg = str(e).lower()
            # 余额不足 / 认证失败 / 权限不够 → 当作跳过而非失败
            if any(kw in err_msg for kw in ("401", "403", "balance", "insufficient",
                                             "quota", "unauthorized", "forbidden")):
                raise unittest_skip(f"SiliconFlow API 不可用（余额/权限）: {e}")
            raise
        size = os.path.getsize(path)
        assert size > 100, f"siliconflow 产出文件太小: {size} bytes"
    finally:
        if os.path.exists(path):
            os.unlink(path)


# ── pipeline 集成：_create_tts_engine + synthesize ──

def test_create_engine_and_synthesize_edge_tts():
    """pipeline _create_tts_engine 创建 edge-tts 引擎后能正常合成"""
    from pipeline import _create_tts_engine
    config = {"tts_engine": "edge-tts"}
    engine = _create_tts_engine(config)
    voice = engine.resolve_voice("zh-CN-YunxiNeural")
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        path = f.name
    try:
        _run_network(engine.synthesize("工厂函数集成测试", path, voice), "factory:edge-tts")
        size = os.path.getsize(path)
        assert size > 100, f"工厂函数 edge-tts 产出文件太小: {size} bytes"
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_create_engine_and_synthesize_gtts():
    """pipeline _create_tts_engine 创建 gtts 引擎后能正常合成"""
    from pipeline import _create_tts_engine
    config = {"tts_engine": "gtts"}
    engine = _create_tts_engine(config)
    voice = engine.resolve_voice("zh-CN-YunxiNeural")
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        path = f.name
    try:
        _run_network(engine.synthesize("工厂函数集成测试", path, voice), "factory:gtts")
        size = os.path.getsize(path)
        assert size > 100, f"工厂函数 gtts 产出文件太小: {size} bytes"
    finally:
        if os.path.exists(path):
            os.unlink(path)


# ── 辅助 ──

class unittest_skip(Exception):
    """标记跳过（非失败）"""
    pass


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = skipped = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except unittest_skip as e:
            print(f"  ⏭  {t.__name__}: {e}")
            skipped += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1

    parts = [f"✅ {passed} passed"]
    if failed:
        parts.append(f"❌ {failed} failed")
    if skipped:
        parts.append(f"⏭ {skipped} skipped")
    icon = '✅' if failed == 0 else '❌'
    print(f"\n{icon} {', '.join(parts)}")
    sys.exit(1 if failed else 0)
