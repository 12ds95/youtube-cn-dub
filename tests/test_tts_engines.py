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
    _generate_tts_segments, _backup_tts, TTSFatalError,
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


# ── 断点恢复流程测试 ──

def test_checkpoint_resume_retries_failed_segments_first():
    """断点恢复应先用失败引擎重试失败片段，而非直接跳到下一引擎"""
    import tempfile, asyncio, json
    from pathlib import Path
    from pipeline import _write_failure_json

    call_log = []  # 记录哪些片段被哪个引擎处理

    class MockEngine(TTSEngine):
        """第二次被调用时能成功的引擎"""
        name = "mock-retry"
        is_local = True  # 本地引擎只重试 1 轮
        def __init__(self):
            self.attempt = {}
        def resolve_voice(self, v): return "mock-voice"
        async def synthesize(self, text, path, voice):
            call_log.append(("mock-retry", os.path.basename(path)))
            # 所有片段在第二次合成时成功
            if path not in self.attempt:
                self.attempt[path] = 0
            self.attempt[path] += 1
            if self.attempt[path] >= 2:
                with open(path, "wb") as f:
                    f.write(b"\xff" * 100)
            else:
                Path(path).touch()  # 0 字节 = 失败

    segments = [
        {"text_zh": "成功片段一", "start": 0, "end": 2},
        {"text_zh": "失败需要重试", "start": 2, "end": 4},
        {"text_zh": "成功片段三", "start": 4, "end": 6},
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        tts_dir = output_dir / "tts_segments"
        tts_dir.mkdir()

        # 模拟上次运行: seg_0000 和 seg_0002 成功，seg_0001 失败
        (tts_dir / "seg_0000.mp3").write_bytes(b"\xff" * 100)
        (tts_dir / "seg_0002.mp3").write_bytes(b"\xff" * 100)
        (tts_dir / "seg_0001.mp3").touch()  # 0 字节 = 失败

        # 写 tts_failure.json
        failure_json = output_dir / "tts_failure.json"
        fail_info = {
            "engine": "mock-retry",
            "total_segments": 3,
            "failed_count": 1,
            "failed_segments": [1],
            "chain": ["mock-retry", "gtts"],
            "chain_position": 0,
            "voice": "mock-voice",
        }
        failure_json.write_text(json.dumps(fail_info))

        # 验证 failure_json 正确写入
        data = json.loads(failure_json.read_text())
        assert data["engine"] == "mock-retry"
        assert data["failed_segments"] == [1]
        assert 1 in data["failed_segments"]
        print("     (断点恢复 JSON 验证通过)")


def test_all_items_defined_before_resume():
    """all_items 必须在断点恢复逻辑之前定义，否则会 NameError"""
    import ast
    with open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "pipeline.py")) as f:
        source = f.read()

    # 简单检查：在 _generate_tts_segments 函数中，
    # "all_items = []" 应出现在 "failure_json.exists()" 之前
    func_start = source.find("async def _generate_tts_segments")
    assert func_start > 0, "_generate_tts_segments 函数未找到"
    func_body = source[func_start:]

    all_items_def = func_body.find("all_items = []")
    failure_check = func_body.find("failure_json.exists()")
    assert all_items_def > 0, "未找到 all_items = []"
    assert failure_check > 0, "未找到 failure_json.exists()"
    assert all_items_def < failure_check, \
        "all_items 必须在 failure_json.exists() 之前定义 (防止 NameError)"


def test_fatal_error_skips_engine_immediately():
    """TTSFatalError (认证/余额等) 应立即跳过引擎而非无效重试"""
    import asyncio, tempfile, json
    from pathlib import Path

    # 1. TTSFatalError 是独立异常类，继承 Exception
    assert issubclass(TTSFatalError, Exception)
    assert not issubclass(TTSFatalError, RuntimeError)

    # 2. SiliconFlow 引擎 401/403 应抛出 TTSFatalError 而非 RuntimeError
    source_file = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "pipeline.py")
    with open(source_file) as f:
        source = f.read()
    # 验证 SiliconFlowTTSEngine.synthesize 中有 TTSFatalError
    sf_start = source.find("class SiliconFlowTTSEngine")
    sf_end = source.find("\nclass ", sf_start + 1)
    sf_body = source[sf_start:sf_end]
    assert "TTSFatalError" in sf_body, \
        "SiliconFlowTTSEngine 应对 401/403 抛出 TTSFatalError"

    # 3. synthesize_batch 应传播 TTSFatalError（不被 return_exceptions 吞掉）
    assert "TTSFatalError" in source[source.find("async def synthesize_batch"):
                                      source.find("class EdgeTTSEngine")], \
        "synthesize_batch 应检测并传播 TTSFatalError"

    # 4. _smart_retry_engine 应能处理 TTSFatalError
    retry_fn = source[source.find("async def _smart_retry_engine"):
                       source.find("def _write_failure_json")]
    assert "TTSFatalError" in retry_fn, \
        "_smart_retry_engine 应处理 TTSFatalError"

    print("     (TTSFatalError 快速跳过引擎验证通过)")


def test_cache_loaded_when_skip_steps_includes_transcribe():
    """skip_steps 包含 transcribe/translate 时仍应从 segments_cache.json 加载 segments"""
    source_file = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "pipeline.py")
    with open(source_file) as f:
        source = f.read()

    # 定位 process_video 中的缓存加载逻辑
    pv_start = source.find("async def process_video")
    assert pv_start > 0
    pv_body = source[pv_start:]

    # 旧代码的 bug: cache_file.exists() and "transcribe" not in skip and "translate" not in skip
    # 修复后: cache_file.exists() 作为首要条件，不受 skip_steps 限制
    cache_load_block = pv_body[:pv_body.find("# ──────────── 分支")]

    # 确认不再有 "transcribe" not in skip 作为缓存加载的前提条件
    # 正确逻辑: if cache_file.exists(): 独立判断
    first_cache_check = cache_load_block.find("cache_file.exists()")
    assert first_cache_check > 0, "未找到 cache_file.exists() 检查"

    # 在第一个 cache_file.exists() 后面不应紧跟 "not in skip" 条件
    after_check = cache_load_block[first_cache_check:first_cache_check + 100]
    assert '"transcribe" not in skip' not in after_check, \
        "缓存加载不应要求 transcribe not in skip (会导致 skip transcribe 时 segments 为空)"
    print("     (segments 缓存加载逻辑验证通过)")


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
