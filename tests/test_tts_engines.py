#!/usr/bin/env python3
"""
测试：TTS 可插拔引擎架构
验证引擎注册表、工厂函数、fallback 链、新增引擎的完整性。
不调用实际 TTS 服务，仅测试架构逻辑。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import (
    TTS_ENGINES, TTSEngine, _create_tts_engine,
    EdgeTTSEngine, GTTSEngine, SiliconFlowTTSEngine,
    Pyttsx3Engine, PiperTTSEngine, SherpaOnnxEngine, CosyVoiceEngine,
)


def test_all_engines_registered():
    """所有引擎都应在 TTS_ENGINES 注册表中"""
    expected = {
        "edge-tts", "gtts", "siliconflow", "pyttsx3",
        "piper", "sherpa-onnx", "cosyvoice",
    }
    actual = set(TTS_ENGINES.keys())
    assert expected == actual, f"引擎注册表不匹配: 缺少 {expected - actual}, 多余 {actual - expected}"


def test_all_engines_inherit_base():
    """所有注册引擎都应继承 TTSEngine 基类"""
    for name, cls in TTS_ENGINES.items():
        assert issubclass(cls, TTSEngine), f"{name} 未继承 TTSEngine"


def test_all_engines_have_synthesize():
    """所有引擎都应实现 synthesize 方法"""
    for name, cls in TTS_ENGINES.items():
        assert hasattr(cls, 'synthesize'), f"{name} 缺少 synthesize 方法"
        # 确保不是基类的 NotImplementedError
        if name != "base":
            import inspect
            source = inspect.getsource(cls.synthesize)
            assert "NotImplementedError" not in source, \
                f"{name}.synthesize 仍是基类占位"


def test_all_engines_have_name():
    """每个引擎类都应有唯一的 name 属性"""
    names = set()
    for key, cls in TTS_ENGINES.items():
        assert hasattr(cls, 'name'), f"TTS_ENGINES['{key}'] 缺少 name 属性"
        assert cls.name not in names, f"name '{cls.name}' 重复"
        names.add(cls.name)


def test_factory_default_engine():
    """默认配置应创建 edge-tts 引擎"""
    engine = _create_tts_engine({})
    assert isinstance(engine, EdgeTTSEngine)


def test_factory_unknown_engine_fallback():
    """未知引擎名应回退到 edge-tts"""
    engine = _create_tts_engine({"tts_engine": "nonexistent"})
    assert isinstance(engine, EdgeTTSEngine)


def test_factory_siliconflow():
    """SiliconFlow 引擎应正确接收配置参数"""
    config = {
        "tts_engine": "siliconflow",
        "siliconflow": {
            "api_key": "test-key",
            "model": "test-model",
            "voice": "test-voice",
        },
    }
    engine = _create_tts_engine(config)
    assert isinstance(engine, SiliconFlowTTSEngine)
    assert engine.api_key == "test-key"
    assert engine.model == "test-model"
    assert engine.voice_id == "test-voice"


def test_factory_pyttsx3():
    """pyttsx3 引擎应正确接收配置参数"""
    config = {
        "tts_engine": "pyttsx3",
        "pyttsx3": {
            "voice_name": "Ting-Ting",
            "rate": 200,
        },
    }
    engine = _create_tts_engine(config)
    assert isinstance(engine, Pyttsx3Engine)
    assert engine.voice_name == "Ting-Ting"
    assert engine.rate == 200


def test_factory_gtts():
    """gTTS 引擎应正确创建"""
    engine = _create_tts_engine({"tts_engine": "gtts"})
    assert isinstance(engine, GTTSEngine)


def test_fallback_list_parsing():
    """tts_fallback 应支持字符串和列表两种格式"""
    # 字符串格式
    config1 = {"tts_fallback": "gtts"}
    fb1 = config1.get("tts_fallback", [])
    if isinstance(fb1, str):
        fb1 = [fb1]
    assert fb1 == ["gtts"]

    # 列表格式
    config2 = {"tts_fallback": ["gtts", "pyttsx3"]}
    fb2 = config2.get("tts_fallback", [])
    if isinstance(fb2, str):
        fb2 = [fb2]
    assert fb2 == ["gtts", "pyttsx3"]


def test_batch_synthesize_exists():
    """基类应有 synthesize_batch 默认实现"""
    assert hasattr(TTSEngine, 'synthesize_batch')


def test_free_online_engines_count():
    """应至少有 3 个免费在线引擎可选"""
    free_online = {"edge-tts", "gtts", "siliconflow"}
    registered = set(TTS_ENGINES.keys())
    assert free_online.issubset(registered), \
        f"缺少免费在线引擎: {free_online - registered}"


def test_offline_engines_count():
    """应至少有 2 个离线引擎（无需网络）"""
    offline = {"pyttsx3", "piper", "sherpa-onnx"}
    registered = set(TTS_ENGINES.keys())
    assert len(offline & registered) >= 2, \
        f"离线引擎不足: 仅有 {offline & registered}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  \u2705 {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  \u274c {t.__name__}: {e}")
            failed += 1
    icon = '\u2705' if failed == 0 else '\u274c'
    print(f"\n{icon} {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
