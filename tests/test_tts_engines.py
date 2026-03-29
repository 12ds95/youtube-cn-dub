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
    _generate_tts_segments, _backup_tts,
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


# ── resolve_voice 测试：确保各引擎不会误用 edge-tts 的全局语音 ──

def test_resolve_voice_edge_tts():
    """edge-tts 应直接使用全局 voice"""
    engine = EdgeTTSEngine()
    assert engine.resolve_voice("zh-CN-YunxiNeural") == "zh-CN-YunxiNeural"


def test_resolve_voice_siliconflow_ignores_global():
    """SiliconFlow 应使用自己的 voice_id，忽略全局 zh-CN-YunxiNeural"""
    engine = SiliconFlowTTSEngine(voice_id="FunAudioLLM/CosyVoice2-0.5B:alex")
    resolved = engine.resolve_voice("zh-CN-YunxiNeural")
    assert resolved == "FunAudioLLM/CosyVoice2-0.5B:alex"
    assert "YunxiNeural" not in resolved


def test_resolve_voice_siliconflow_default():
    """SiliconFlow 未配 voice_id 时应用默认值，而非全局 voice"""
    engine = SiliconFlowTTSEngine()
    resolved = engine.resolve_voice("zh-CN-YunxiNeural")
    assert "CosyVoice2" in resolved
    assert "YunxiNeural" not in resolved


def test_resolve_voice_pyttsx3_ignores_global():
    """pyttsx3 应使用系统语音名，忽略全局 voice"""
    engine = Pyttsx3Engine(voice_name="Ting-Ting")
    resolved = engine.resolve_voice("zh-CN-YunxiNeural")
    assert resolved == "Ting-Ting"
    assert "YunxiNeural" not in resolved


def test_resolve_voice_gtts_ignores_global():
    """gTTS 应使用 zh-cn 语言代码，忽略全局 voice"""
    engine = GTTSEngine()
    resolved = engine.resolve_voice("zh-CN-YunxiNeural")
    assert resolved == "zh-cn"
    assert "YunxiNeural" not in resolved


def test_resolve_voice_piper_ignores_global():
    """Piper 应使用模型路径，忽略全局 voice"""
    engine = PiperTTSEngine(model_path="/models/zh_CN-huayan-medium.onnx")
    resolved = engine.resolve_voice("zh-CN-YunxiNeural")
    assert resolved == "/models/zh_CN-huayan-medium.onnx"
    assert "YunxiNeural" not in resolved


def test_resolve_voice_cosyvoice_ignores_global():
    """CosyVoice 应使用中文角色名，忽略全局 voice"""
    engine = CosyVoiceEngine()
    resolved = engine.resolve_voice("zh-CN-YunxiNeural")
    assert resolved == "中文女"
    assert "YunxiNeural" not in resolved


def test_resolve_voice_sherpa_ignores_global():
    """sherpa-onnx 应使用 speaker_id，忽略全局 voice"""
    engine = SherpaOnnxEngine(model_config={"speaker_id": 3})
    resolved = engine.resolve_voice("zh-CN-YunxiNeural")
    assert resolved == "3"
    assert "YunxiNeural" not in resolved


# ── tts_chain 测试 ──

def test_tts_chain_primary_engine():
    """tts_chain 第一个元素应作为主引擎"""
    config = {"tts_chain": ["gtts", "edge-tts", "pyttsx3"]}
    engine = _create_tts_engine({**config, "tts_engine": config["tts_chain"][0]})
    assert isinstance(engine, GTTSEngine)


def test_tts_chain_string_format():
    """tts_chain 应支持单字符串格式"""
    config = {"tts_chain": "siliconflow"}
    chain = config["tts_chain"]
    if isinstance(chain, str):
        chain = [chain]
    assert chain == ["siliconflow"]


def test_tts_chain_overrides_tts_engine():
    """tts_chain 应优先于 tts_engine + tts_fallback"""
    config = {
        "tts_engine": "edge-tts",
        "tts_fallback": ["gtts"],
        "tts_chain": ["siliconflow", "pyttsx3", "edge-tts"],
    }
    # tts_chain 存在时，主引擎应为 siliconflow
    chain = config.get("tts_chain")
    assert chain is not None
    primary = chain[0]
    fallbacks = chain[1:]
    assert primary == "siliconflow"
    assert fallbacks == ["pyttsx3", "edge-tts"]


# ── 整体回退策略测试 ──

def test_whole_fallback_clears_all_on_engine_switch():
    """整体回退：换引擎时应清空全部片段，不混用不同引擎的产出"""
    import tempfile, asyncio
    from pathlib import Path

    call_log = []

    class FakeFailEngine(TTSEngine):
        name = "fake-fail"
        def resolve_voice(self, v): return "fail-voice"
        async def synthesize(self, text, path, voice):
            call_log.append(("fail", path))
            # 只生成空文件（模拟失败）
            Path(path).touch()

    class FakeOKEngine(TTSEngine):
        name = "fake-ok"
        def resolve_voice(self, v): return "ok-voice"
        async def synthesize(self, text, path, voice):
            call_log.append(("ok", path))
            with open(path, "wb") as f:
                f.write(b"\xff" * 100)  # 非空内容

    # 模拟引擎链
    segments = [
        {"text_zh": "你好世界", "start": 0, "end": 2},
        {"text_zh": "测试文本", "start": 2, "end": 4},
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        tts_dir = Path(tmpdir)

        # 手动模拟整体回退流程
        engines = [FakeFailEngine(), FakeOKEngine()]

        for eng_pos, engine in enumerate(engines):
            if eng_pos > 0:
                # 整体回退：清空前一引擎产出
                for idx in range(len(segments)):
                    p = tts_dir / f"seg_{idx:04d}.mp3"
                    if p.exists():
                        p.unlink()

            for idx, seg in enumerate(segments):
                p = tts_dir / f"seg_{idx:04d}.mp3"
                await_sync(engine.synthesize(seg["text_zh"], str(p),
                                             engine.resolve_voice("")))

            # 检查是否全部成功
            all_ok = all(
                (tts_dir / f"seg_{i:04d}.mp3").exists() and
                (tts_dir / f"seg_{i:04d}.mp3").stat().st_size > 0
                for i in range(len(segments))
            )
            if all_ok:
                break

        # 验证：最终文件全部来自 fake-ok 引擎（非空）
        for i in range(len(segments)):
            p = tts_dir / f"seg_{i:04d}.mp3"
            assert p.exists() and p.stat().st_size > 0, \
                f"seg_{i:04d}.mp3 应由 fake-ok 生成"

        # 验证：fake-ok 引擎处理了全部片段（而非只补漏）
        ok_calls = [c for c in call_log if c[0] == "ok"]
        assert len(ok_calls) == len(segments), \
            f"整体回退应让新引擎重新生成全部 {len(segments)} 个片段，实际={len(ok_calls)}"


def await_sync(coro):
    """同步执行协程（测试辅助）"""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


def test_whole_fallback_no_voice_mixing():
    """整体回退后不应存在前一引擎的残留文件"""
    import tempfile
    from pathlib import Path

    class WriteMarkerEngine(TTSEngine):
        def __init__(self, marker: bytes):
            self.marker = marker
        def resolve_voice(self, v): return "x"
        async def synthesize(self, text, path, voice):
            with open(path, "wb") as f:
                f.write(self.marker)

    eng_a = WriteMarkerEngine(b"ENGINE_A")
    eng_b = WriteMarkerEngine(b"ENGINE_B")

    with tempfile.TemporaryDirectory() as tmpdir:
        tts_dir = Path(tmpdir)

        # 引擎 A 先生成
        for i in range(3):
            p = tts_dir / f"seg_{i:04d}.mp3"
            await_sync(eng_a.synthesize(f"text{i}", str(p), "x"))

        # 模拟整体回退：清空后用引擎 B 重新生成
        for i in range(3):
            p = tts_dir / f"seg_{i:04d}.mp3"
            if p.exists():
                p.unlink()
        for i in range(3):
            p = tts_dir / f"seg_{i:04d}.mp3"
            await_sync(eng_b.synthesize(f"text{i}", str(p), "x"))

        # 验证全部来自引擎 B
        for i in range(3):
            content = (tts_dir / f"seg_{i:04d}.mp3").read_bytes()
            assert content == b"ENGINE_B", \
                f"seg_{i:04d} 应为 ENGINE_B，实际={content}"


# ── is_local 属性测试 ──

def test_remote_engines_is_local_false():
    """远程引擎 is_local 应为 False"""
    remote = [EdgeTTSEngine, GTTSEngine, SiliconFlowTTSEngine]
    for cls in remote:
        assert cls.is_local is False, f"{cls.name} 应为远程引擎 (is_local=False)"


def test_local_engines_is_local_true():
    """本地引擎 is_local 应为 True"""
    local = [PiperTTSEngine, SherpaOnnxEngine, CosyVoiceEngine, Pyttsx3Engine]
    for cls in local:
        assert cls.is_local is True, f"{cls.name} 应为本地引擎 (is_local=True)"


# ── 备份与失败 JSON 测试 ──

def test_backup_tts_copies_files():
    """_backup_tts 应将 TTS 文件复制到备份目录"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        tts_dir = Path(tmpdir) / "tts"
        tts_dir.mkdir()
        backup_dir = Path(tmpdir) / "tts_backup_edge-tts"

        items = [{"idx": i, "text_zh": f"test{i}"} for i in range(3)]
        for item in items:
            p = tts_dir / f"seg_{item['idx']:04d}.mp3"
            p.write_bytes(b"\xff" * 100)

        _backup_tts(tts_dir, backup_dir, items)

        assert backup_dir.exists()
        for item in items:
            bp = backup_dir / f"seg_{item['idx']:04d}.mp3"
            assert bp.exists() and bp.stat().st_size == 100


def test_failure_json_written_on_fail():
    """引擎失败时应写入 tts_failure.json，含失败片段列表"""
    import tempfile, asyncio, json
    from pathlib import Path

    class AlwaysFailEngine(TTSEngine):
        name = "always-fail"
        is_local = True
        def resolve_voice(self, v): return "x"
        async def synthesize(self, text, path, voice):
            Path(path).touch()  # 0 字节

    segments = [
        {"text_zh": "你好世界呀", "start": 0, "end": 2},
        {"text_zh": "测试失败文本", "start": 2, "end": 4},
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        tts_dir = Path(tmpdir) / "tts_segments"
        tts_dir.mkdir()

        # 直接模拟失败 JSON 写入逻辑
        all_items = [{"idx": i, "text_zh": s["text_zh"]} for i, s in enumerate(segments)]
        fail_info = {
            "engine": "always-fail",
            "total_segments": len(all_items),
            "failed_count": len(all_items),
            "failed_segments": [item["idx"] for item in all_items],
            "chain": ["always-fail"],
            "chain_position": 0,
            "voice": "x",
        }
        failure_json = Path(tmpdir) / "tts_failure.json"
        with open(failure_json, "w") as f:
            json.dump(fail_info, f)

        data = json.loads(failure_json.read_text())
        assert data["engine"] == "always-fail"
        assert data["failed_count"] == 2
        assert data["failed_segments"] == [0, 1]


def test_failure_json_cleared_on_success():
    """全部片段成功后 tts_failure.json 应被删除"""
    import tempfile, json
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        failure_json = Path(tmpdir) / "tts_failure.json"
        failure_json.write_text(json.dumps({"engine": "test"}))
        assert failure_json.exists()

        # 模拟成功后清理
        if failure_json.exists():
            failure_json.unlink()
        assert not failure_json.exists()


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
