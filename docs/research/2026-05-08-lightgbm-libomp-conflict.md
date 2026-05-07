# LightGBM 与系统 libomp 冲突 — macOS SIGSEGV 调研

**日期**: 2026-05-08
**触发**: Wordle 视频（`v68zYyaEmEA`）`--retranslate-only` 跑 pipeline.py 时 100% 段错误（exit -11），其它视频偶发 < 1% 崩溃。
**关联代码**: `pipeline.py` 顶部预热, `duration_estimator.py:_load_lgbm/_estimate_v6`

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

**多个 OpenMP 运行时（libomp）在同一进程内并存**，LightGBM 在翻译循环中 lazy-load 时与已存在的 OpenMP 实例（来自 `ctranslate2 / torch / sklearn` 各 wheel 自带的 `libiomp5.dylib`）首次进入临界区时线程池初始化竞态。

`venv` libomp 清单实测：

| 来源 | 携带 | 路径 |
|------|------|------|
| LightGBM (pip wheel) | 不带 | 用 `@rpath/libomp.dylib` → 走系统 homebrew `/usr/local/opt/libomp/lib/libomp.dylib` |
| ctranslate2 | **bundled** | `venv/.../ctranslate2/.dylibs/libiomp5.dylib` |
| torch | **bundled** | `venv/.../torch/lib/libiomp5.dylib` |
| sklearn | **bundled** | `venv/.../sklearn/.dylibs/libomp.dylib` |
| numpy (OpenBLAS) | 不用 OpenMP | `USE_OPENMP=` 编译时未启用，无关 |

`KMP_DUPLICATE_LIB_OK=TRUE` 只能避免 SIGABRT 检测，**不能避免线程池初始化竞态导致的 SIGSEGV**。

## 3. 真正的修复（极简）

**在 pipeline.py 模块导入阶段强制预热 LightGBM**——给它一个独占的 OpenMP init 窗口，远早于任何其它 native 库（httpx、ctranslate2、torch）开始工作。

```python
# pipeline.py:111
from duration_estimator import estimate_duration as _estimate_duration_jieba

# 关键: 字符串 ≥5 有效中文字符, 否则 _estimate_v6 直接降级 v4 不加载 LightGBM!
try:
    _estimate_duration_jieba("预热模型加载用的占位文本以触发 LightGBM 实际加载")
except Exception:
    pass
```

类比 `commit 8df0988` "demucs 子进程隔离 libomp" — 它用**进程隔离**，我们这里用**时间隔离**。本质相同：让冲突的 native 库各自独占一段 init 窗口。

### 实测对比（Wordle `v68zYyaEmEA`，retranslate 模式）

| 配置 | 结果 |
|------|------|
| 短字符串预热 (`"预热"`，2 字符 → fall back v4 → **预热实际无效**) | ❌ 崩 |
| `OMP_NUM_THREADS=1` 全局 + 短预热 | ✅ 通过（OMP=1 单独起作用，预热无效）|
| `Booster(num_threads=1)` + 短预热 | ❌ 崩（都没真正加载 LightGBM）|
| **长字符串预热（≥5 字符 → 真正加载 LightGBM）** | **✅ 通过** |

**`OMP_NUM_THREADS=1` 和 `Booster(num_threads=1)` 都不是必需的**——之前以为它们是修复，是因为同期的预热 dummy 字符串太短没生效。本质上只需要"让 LightGBM 早点加载"。

## 4. 历史教训复盘

| commit | 内容 | 结论 |
|--------|------|------|
| `6e09c25` | 设 `KMP_DUPLICATE_LIB_OK=TRUE` | 防 SIGABRT，**不防 SIGSEGV 竞态** |
| `8e9e28c` | 全局 `OMP_NUM_THREADS=1` | 临时解，**拖慢 demucs/whisper** |
| `8df0988` | demucs 改 subprocess 隔离 | 正确解（进程隔离）；移除 `OMP_NUM_THREADS=1` |
| `6186903`（已撤回）| 错把 `OMP_NUM_THREADS=1` 加回 pipeline.py 顶部 | 重蹈 8e9e28c 覆辙；同时加了无效预热（字符串太短） |
| `c4d50d7`（已撤回 retranslate 路径限定）| 三层防御：retranslate 路径 OMP=1 + Booster num_threads=1 + 短预热 | 不全面（全跑模式仍有风险），且本可避免 |
| `<本 commit>` | 仅靠加长预热字符串，让 LightGBM 在 import 阶段独占 init 窗口 | **真正根治**，全跑/重译路径都受益 |

**核心教训**：
1. 原生库冲突不要无脑全局禁线程；先看是否能用"时间隔离"或"进程隔离"。
2. 任何"防御性参数"都要验证它**确实在生效路径上**。我前一次提交以为预热在保护我，实际上预热里的字符串短到根本没触发 LightGBM 加载——保护是 OMP=1 在做。这种自欺也是浪费时间的根源。

## 5. Q&A

### Q: 为什么只有 Wordle 100% 复现，其它视频偶发？

LightGBM lazy-load 时机所在的"临界窗口"宽度跟 Python 解释器状态、其它 OpenMP 库已加载时长有关。Wordle 转录段数 (841→178)、anchor 密度 (0.54)、prompt 长度等综合影响代码路径耗时，让 LightGBM 加载点恰好命中 deterministic timing window。其它视频崩 < 1%（之前未观测，或被当作"重跑就好了"）。

### Q: 全跑路径（含 transcribe，加载 faster-whisper）现在安全吗？

安全。预热在 import 阶段执行，LightGBM **在 ctranslate2 加载前**就完成了 OpenMP init。各 native 库依次独占 init 窗口：

```
pipeline.py 启动
  ├─ 顶层 import 阶段
  │   ├─ duration_estimator import → jieba load (no OpenMP)
  │   └─ prewarm 调用 → LightGBM 加载 → 系统 libomp init  ← 独占窗口 1
  ├─ download / extract 阶段（无 native lib 加载）
  ├─ separate 阶段 → demucs 子进程（独立地址空间，无关）
  ├─ transcribe 阶段 → ctranslate2 加载 → bundled libiomp5 init  ← 独占窗口 2
  └─ 翻译阶段 → LightGBM predict（已 init，纯 prediction）
```

### Q: 多音字 G2PW（ONNX runtime）会触发同类问题吗？

**默认不加载**。`pipeline.py:3383` 调 `_fix_polyphones(text_zh)` 不传 `use_g2pw_fallback`，走纯 Python 词典 + pypinyin，无 BERT/ONNX。

如果用户 opt-in `use_g2pw_fallback=True`：会加载 `bert-base-chinese`（420MB）+ ONNX runtime + G2PWModel。ONNX runtime 也用 libomp，**理论上有同类风险**。如果届时观察到崩溃，按本文同样的"时间隔离"方案处理：在 pipeline.py 顶部预热 G2PW（调一次 `_get_g2pw().lazy_pinyin("预热用文本")`），让 ONNX runtime 在独占窗口完成 init。

### Q: 既然 lightgbm 用系统 libomp，sklearn/torch/ctranslate2 用 bundled libomp，本质问题是不是 wheel 自带 libomp？终极方案是不是 `pip install --no-binary lightgbm`？

是。LightGBM FAQ 的官方建议有两条根治方向：
- `pip install --no-binary :all: lightgbm onnxruntime ctranslate2`（编译时全用系统 libomp，进程内只剩一份）
- `conda install -c conda-forge ...`（conda-forge 版已 patch OpenMP 链接）

我们项目实际用了 `pip + venv`，让用户重新编译这些库会破坏现有环境。"时间隔离"是低成本中等收益方案。如未来 G2PW / 其它 ONNX 模型陆续启用，所有库的预热会让 import 时间线性变长；那时再考虑根治方案（要求用户用 conda-forge 或 source build）。

## 6. 行动项

- [x] commit pipeline.py 预热字符串修复
- [x] 撤回 `c4d50d7`/`cd1123c` 中无谓的 OMP=1 / num_threads=1 / retranslate 路径检测
- [x] 写本调研文档

## 来源

- [LightGBM FAQ — macOS workarounds](https://lightgbm.readthedocs.io/en/latest/FAQ.html)
- [LightGBM #6595 — SegFault on macOS when pytorch installed](https://github.com/microsoft/LightGBM/issues/6595)
- [LightGBM #4897 — Segmentation Fault w/ Torch on macOS](https://github.com/microsoft/LightGBM/issues/4897)
- [LightGBM #4229 — incompatible with libomp 12 and 13](https://github.com/microsoft/LightGBM/issues/4229)
- [autogluon #1442 — LightGBM + Torch macOS segfault](https://github.com/autogluon/autogluon/issues/1442)
- [pytorch #6194 — Import order matters](https://github.com/pytorch/pytorch/issues/6194)
- 本仓库 commit: `6e09c25`, `8e9e28c`, `8df0988`, `6186903`, `c4d50d7`
