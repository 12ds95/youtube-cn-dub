# LightGBM 与系统 libomp 冲突 — macOS SIGSEGV 调研

**日期**: 2026-05-08
**触发**: Wordle 视频（`v68zYyaEmEA`）`--retranslate-only` 跑 pipeline.py 时 100% 段错误（exit -11），其它视频 < 1% 偶发崩溃。
**关联代码**: `pipeline.py`, `duration_estimator.py:_load_lgbm/_estimate_v6`, `batch_process.py:_run_with_download_watch`

---

## 1. 现象

崩溃位置精确：`build_unit_translation_lines` 第一次调用 `compute_target_char_range` → `_global_ms_per_char_jieba` → `estimate_duration` → `_load_lgbm()`，触发 `Fatal Python error: Segmentation fault`。faulthandler 在打印 traceback 时再次崩溃（"Fatal Python error: Fatal Python error:"），表明 Python 解释器状态已被 native 库破坏。

```
[5/8] 翻译
  📎 Sentence Unit 合并: 841 → 178 段
  ...
  ⚠️  中等锚点密度 (0.54/段)，缩小批次 8→6
Fatal Python error: Fatal Python error: Fatal Python error:
exit -11
```

## 2. 根因

**多个 OpenMP 运行时（libomp）在同一进程内并存**，首次进入 OpenMP 临界区时线程池初始化竞态：

| 来源 | 携带 libomp | 加载时机 |
|------|-------------|---------|
| LightGBM (pip wheel) | bundled `libomp.dylib` | 首次 `lgb.Booster(...)` |
| numpy (OpenBLAS) | 系统 `libomp` | numpy import |
| jieba 自身 | 无（纯 Python） | — |
| ctranslate2 (faster-whisper) | 自带 | 仅全跑模式 |
| PyTorch (demucs) | 自带 | 仅全跑模式（且子进程） |

`KMP_DUPLICATE_LIB_OK=TRUE` 只能避免 SIGABRT 检测，**不能避免线程池初始化竞态导致的 SIGSEGV**。

## 3. 社区 / 上游已知方案

| 方案 | 来源 | 我们能用？ |
|------|------|----------|
| `import lightgbm` 在 `import torch` **之前** | [pytorch/pytorch#6194](https://github.com/pytorch/pytorch/issues/6194), [autogluon/autogluon#1442](https://github.com/autogluon/autogluon/issues/1442) | ✅ 部分 — 我们项目 retranslate 模式不 import torch，但仍崩 |
| `pip install --no-binary lightgbm lightgbm`（编译时链接系统 libomp）| [LightGBM FAQ](https://lightgbm.readthedocs.io/en/latest/FAQ.html) | ⚠️ 用户需手动重装，破坏现有环境 |
| `conda install -c conda-forge lightgbm`（上游已 patch OpenMP 链接） | [LightGBM FAQ](https://lightgbm.readthedocs.io/en/latest/FAQ.html) | ❌ 我们用 venv 不是 conda |
| symlink `libomp.dylib` 替换所有别名 | [LightGBM FAQ](https://lightgbm.readthedocs.io/en/latest/FAQ.html) | ⚠️ 系统级修改，影响其它 Python 项目 |
| `OMP_NUM_THREADS=1`（禁用 OpenMP 多线程） | 多个 issue | ⚠️ **会拖慢 demucs/whisper**（commit `8df0988` 教训） |
| `nthreads=1` / `num_threads=1` 传给 LightGBM | [Issue #4707](https://github.com/microsoft/LightGBM/issues/4707) | ✅ 单段 prediction，零损 |
| Subprocess 隔离（让 LightGBM 进程独占 libomp） | commit `8df0988` 思路 | ⚠️ predict <1ms，subprocess 启停 100ms，开销过大 |

## 4. 我们项目的最终方案

**分场景多层防御**（commit `<TBD>`）：

### Layer 1 — 时间隔离（pipeline.py 顶部预热）

```python
# pipeline.py:111
from duration_estimator import estimate_duration as _estimate_duration_jieba

# 预热: 在 import 阶段、其它 native 库 (httpx/asyncio 内部线程) 开始工作前,
# 完成 LightGBM + jieba 的 OpenMP 线程池初始化.
try:
    _estimate_duration_jieba("预热")
except Exception:
    pass
```

类比 `commit 8df0988` "demucs 子进程隔离"——它用进程隔离，我们这里用**时间隔离**：让 LightGBM 在没有其它 OpenMP 工作时独占 init 窗口。

### Layer 2 — LightGBM 自身单线程

```python
# duration_estimator.py:_load_lgbm
_LGBM_STATE["model"] = lgb.Booster(
    model_file=model_path,
    params={"num_threads": 1},
)
```

零性能损失：单段 predict 无 batch，多线程没意义。

### Layer 3 — retranslate 路径全局 `OMP_NUM_THREADS=1`

```python
# batch_process.py:_run_with_download_watch
is_retranslate_path = "transcribe" in (config.get("skip_steps") or [])
if is_retranslate_path:
    env["OMP_NUM_THREADS"] = "1"
```

**仅在 retranslate 路径生效**——此场景 demucs/whisper 都被跳过，主进程只有 LightGBM 用 OpenMP，单线程 100% 安全。全跑模式（含 transcribe）不设此变量，让 whisper 全速 transcribe。

### 实测对比（Wordle `v68zYyaEmEA`）

| 配置 | 结果 |
|------|------|
| 仅 Layer 1 预热 | ❌ 崩 |
| 仅 Layer 2 num_threads=1 | ❌ 崩 |
| Layer 1 + Layer 2 | ❌ 崩 |
| Layer 1 + Layer 2 + Layer 3 (OMP_NUM_THREADS=1) | ✅ 通过 |

三层缺一不可。退一步说，Layer 3 是真正的"必要条件"，但 Layer 1+2 是良好工程实践：
- Layer 1 把 LightGBM 加载隔离到 import 期，加快首次预测响应（无需运行时 lazy load）
- Layer 2 为 LightGBM 加 belt-and-suspenders（即使将来 Layer 3 失效，predict 阶段不再多 thread 竞态）

## 5. 历史教训复盘（避免重蹈覆辙）

| commit | 内容 | 结论 |
|--------|------|------|
| `6e09c25` | 设 `KMP_DUPLICATE_LIB_OK=TRUE` | 防 SIGABRT，**不防 SIGSEGV 竞态** |
| `8e9e28c` | 全局 `OMP_NUM_THREADS=1` | 临时解，**拖慢 demucs/whisper** |
| `8df0988` | demucs 改 subprocess 隔离 | 正确解；移除 `OMP_NUM_THREADS=1` |
| `6186903` | 错把 `OMP_NUM_THREADS=1` 加回 pipeline.py 顶部 | **重蹈 8e9e28c 覆辙**；下一个 commit 已撤回 |
| `<本 fix>` | retranslate 路径才设 `OMP_NUM_THREADS=1` + 三层防御 | 兼顾 Wordle 稳定 + 全跑模式不降速 |

**核心教训**：原生库冲突不要无脑全局禁线程。先看哪些路径会同时加载冲突库，**只对那些路径加约束**。

## 6. Q&A

### Q: 为什么只有 Wordle 100% 复现，其它视频偶发？

竞态是非确定的。Wordle 转录段数 (841→178)、anchor 密度 (0.54)、prompt 长度等综合下来命中了某个 deterministic timing window。其它视频可能崩 < 1%（之前未观测到，或被当作"重跑就好了"）。

### Q: 多音字（G2PW）会不会也踩同样的坑？

**默认不会**。`pipeline.py:3383` 调 `_fix_polyphones(text_zh)` 不传 `use_g2pw_fallback`，走本地词典 + pypinyin（纯 Python），不加载 BERT/ONNX。

如果用户 opt-in `use_g2pw_fallback=True`：会加载 `bert-base-chinese`（420MB）+ ONNX runtime + G2PWModel。ONNX runtime 也用 libomp，**同类风险存在**。届时可套用同样的三层防御（pre-warm + nthreads + 选择性 OMP=1）。

### Q: G2PW 路径要不要主动加防御？

暂不加。原因：
- 默认关闭，影响面 0
- 主动加防御会增加 import 耗时 / 占用内存
- 用户启用时再针对性修

未来若发现 G2PW 启用后崩溃，按本文 Layer 1+2 套用即可（可能还需 ONNX-specific 的 `OMP_NUM_THREADS` / `OMP_PLACES`）。

### Q: 为什么 import 顺序方案（lightgbm 在 torch 前）对我们没用？

社区通用建议是针对 lightgbm + torch 共存。我们 retranslate 模式不 import torch，但 numpy 自己就带 libomp（OpenBLAS），LightGBM 与 numpy 之间已经够冲突。`pipeline.py:111` 已经在 numpy 大量使用前 import duration_estimator 并预热，但 Layer 3 仍是必要的。

## 7. 行动项

- [x] commit pipeline.py + duration_estimator.py + batch_process.py 三层修复
- [x] 写本文档
- [ ] 监控未来非 retranslate 路径有没有偶发同类崩溃。若有，考虑改 v4 默认（牺牲精度换稳定）

## 来源

- [LightGBM FAQ — macOS workarounds](https://lightgbm.readthedocs.io/en/latest/FAQ.html)
- [LightGBM #6595 — SegFault on macOS when pytorch installed](https://github.com/microsoft/LightGBM/issues/6595)
- [LightGBM #4897 — Segmentation Fault w/ Torch on macOS](https://github.com/microsoft/LightGBM/issues/4897)
- [LightGBM #4229 — incompatible with libomp 12 and 13](https://github.com/microsoft/LightGBM/issues/4229)
- [autogluon #1442 — LightGBM + Torch macOS segfault](https://github.com/autogluon/autogluon/issues/1442)
- [pytorch #6194 — Import order matters](https://github.com/pytorch/pytorch/issues/6194)
- 本仓库 commit: `6e09c25`, `8e9e28c`, `8df0988`, `6186903`
