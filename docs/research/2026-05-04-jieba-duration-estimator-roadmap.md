# Jieba Duration Estimator 优化规划

> 当前版本: Ridge v2 (R²=0.92, 6 视频 3009 样本)
> 模块: `duration_estimator.py`

## 现状分析

当前模型用 jieba 分词后按**词长桶**分配固定时长：

| 特征 | 时长 |
|------|------|
| 单字词（的/是） | 138ms |
| 双字词（今天） | 361ms |
| 三字词（计算机） | 506ms |
| 4+字词 | 223ms/字 |
| 英文字母 | 31ms/字符 |
| 数字 | 311ms/字符 |
| 标点 | 197ms |
| 截距 | +1210ms |

### 已知缺陷

1. **同字数不同时长**: "的"(轻声, ~100ms) vs "是"(四声, ~160ms) 被视为相同
2. **无韵律建模**: 句末延长、语气词拖音、逗号停顿 vs 句号停顿无区分
3. **无音节级特征**: 声母韵母组合影响时长（如 "zhuang" 比 "a" 长），未利用
4. **截距过大**: +1210ms 的全局截距说明模型欠拟合短句
5. **无语境效应**: 词在句首/句中/句末位置影响时长，未建模

## 优化方向

### v3: 音节级特征 (pypinyin)

**依赖**: pypinyin (已在项目中)

**新增特征**:
- **声调**: 轻声(~0.7x) < 一声/四声(1.0x) < 二声(1.05x) < 三声(1.15x)
  - 三声（全上）最长，轻声最短，研究文献一致支持
- **韵母长度**: 简单韵母 a/i/u (~150ms) vs 复合韵母 uang/iang (~200ms)
  - 可用 pypinyin FINALS_TONE3 提取韵母
- **声母有无**: 零声母音节（如 "啊"）比有声母的（如 "他"）短

**实现思路**:
```python
from pypinyin import pinyin, Style

def _syllable_features(char):
    """提取单字的音节级特征"""
    py = pinyin(char, style=Style.TONE3)[0][0]
    final = pinyin(char, style=Style.FINALS_TONE3)[0][0]
    initial = pinyin(char, style=Style.INITIALS)[0][0]
    tone = int(py[-1]) if py[-1].isdigit() else 0  # 0=轻声
    return {
        'tone': tone,
        'final_len': len(final.rstrip('01234')),
        'has_initial': len(initial) > 0,
    }
```

**预期提升**: R² 0.92 → 0.95+, 短句误差显著改善

### v4: 韵律边界建模

**新增特征**:
- **标点类型区分**: 逗号(~150ms) vs 句号(~250ms) vs 感叹号(~200ms) vs 省略号(~350ms)
  - 当前所有标点统一 197ms
- **句末延长**: 每个韵律短语末尾音节自然延长 ~1.2x
  - 利用标点位置识别韵律边界
- **语气词**: "吧/呢/啊/嘛" 等句末语气词通常拖音
  - 可建白名单，+50ms

**参考文献**:
- [Pause Duration Prediction for Mandarin TTS](https://www.isca-archive.org/tal_2006/tao06_tal.html) — 韵律层级停顿模型
- [Duration Prediction in Mandarin TTS](https://www.isca-archive.org/speechprosody_2006/guo06_speechprosody.pdf) — 音节时长受位置和声调影响
- CN110534089A — 基于音素和韵律结构的中文语音合成

### v5: 数据驱动校准

**方法**: 用已有 TTS 输出音频的实际时长回归

1. 从 `output/*/tts_segments/*.mp3` 批量提取实际时长 (pydub)
2. 与对应 `text_zh` 配对，构建 (特征向量, 实际时长ms) 训练集
3. 用 Ridge/Lasso 回归拟合，自动学习最优系数
4. 可按 TTS 引擎分别校准（edge-tts vs VITS 语速不同）

**已有基础**: `calibrate_tts_duration.py` 已实现类似流程

### v6: 轻量神经网络 (可选)

如果线性模型天花板明显（R² < 0.96），可考虑:
- 2层 MLP (输入: 音节特征序列的统计量, 输出: 时长ms)
- 训练数据: v5 积累的 TTS 实测数据
- 推理开销极低（无GPU需求），适合 CPU 环境

## 实施优先级

```
v3 音节级特征 ← 最高优先，pypinyin 已有，改动小，收益明确
v4 韵律边界   ← 中优先，标点区分容易做，语气词白名单可快速验证
v5 数据驱动   ← 积累数据后自然可做
v6 神经网络   ← 低优先，线性模型天花板前不需要
```

## 模块接口设计

`duration_estimator.py` 当前暴露单一函数 `estimate_duration(text_zh) -> float`。
优化后保持接口不变，内部实现升级：

```python
# v3 接口不变，内部自动使用 pypinyin
estimate_duration("计算机图形学")  # -> 更准确的 ms 值

# 可选: 暴露详细分解供调试
estimate_duration_detail("计算机图形学")  # -> {total_ms, words: [{text, ms, features}]}
```
