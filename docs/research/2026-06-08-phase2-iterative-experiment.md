# Phase 2 迭代翻译优化实验报告

## 实验目标

验证 Self-Refine 式迭代翻译优化 + 句子级 embedding 对齐方案的有效性。

## 方法

### 架构
1. **初始候选生成**: 全文翻译 + ||| 段边界标记（每 N 段一个标记）
2. **外层迭代循环**: 每轮以上一轮最优译文 + 英文原文 + 结构化反馈 → LLM refine
3. **三维评估**:
   - LLM 自评 (忠实度/流畅度/完整性/配音适合度, 各 1-10)
   - Sentence embedding 对齐 (paraphrase-multilingual-MiniLM-L12-v2 cosine sim)
   - Budget 偏差 (DP 切分后 MAE)
4. **终止条件**: 连续 3 轮综合分未改善 (>0.5 差值视为改善)

### 参考文献
- [Self-Refine (NeurIPS 2023)](https://arxiv.org/abs/2303.17651): generate → feedback → refine loop
- [Constraint-Aware Iterative Translation](https://arxiv.org/html/2411.08348v1): 约束驱动的迭代翻译
- [IBUT](https://arxiv.org/html/2410.12543v2): 双语理解对齐判断
- [Bertalign](https://github.com/bfsujason/bertalign): LaBSE + DP 句子对齐
- [EAMT 2024 Iterative Translation Refinement](https://aclanthology.org/2024.eamt-1.17.pdf)

## 5 轮实验结果

| 迭代 | 改动 | 最优分 | 对齐 sim | MAE | ±2字 | 最优轮次 |
|------|------|--------|---------|-----|------|---------|
| 1 | 基线 (无标记) | 72.2 | 0.348 | 1.0 | 70/73 | R0 |
| 2 | 保守 refine (最小改动) | 71.1 | 0.345 | 1.5 | 62/73 | R0 |
| 3 | ||| 标记 (GROUP=5) | **76.3** | **0.457** | **0.6** | **73/73** | R0 |
| 4 | 多样性强制 + 重试机制 | **76.6** | **0.466** | 0.6 | 73/73 | R0 |
| 5 | 更紧标记 (GROUP=3) | 75.6 | 0.391 | 0.6 | 73/73 | R1 |

## 关键发现

### 1. ||| 段边界标记是最有效的优化 (+32% 对齐)

从无标记 (iter 1-2) 到 GROUP_SIZE=5 标记 (iter 3-4):
- 对齐相似度: 0.345 → 0.466 (+35%)
- Budget MAE: 1.5 → 0.6 (-60%)
- 综合评分: 71.1 → 76.6 (+7.7%)

这验证了 ROADMAP 中 **方向 4 (混合策略)** 的可行性: 粗粒度 ||| 标记保持组间顺序，组内自由翻译。

### 2. GROUP_SIZE=5 是最优分组粒度

| GROUP_SIZE | 对齐 sim | 综合分 | 分析 |
|-----------|---------|--------|------|
| 无标记 | 0.345 | 71.1 | 翻译自由但语序错位严重 |
| 5 | **0.466** | **76.6** | 最优平衡点 |
| 3 | 0.391 | 75.6 | 标记太密碎片化翻译 |

### 3. Self-Refine 迭代效果有限

- 5 轮实验中 4 轮最优结果来自 Round 0 (初始候选)
- 仅 iter 5 的 Round 1 微超 Round 0 (+1.2)
- 原因分析:
  - **LLM 倾向返回相同文本**: "最小改动"指令使 LLM 不做任何修改
  - **高温重试导致信息损失**: T=0.9 产出的译文经常大幅压缩 (1479→1097)
  - **反馈不够差异化**: 相同的低对齐段被反复指出，但 LLM 无法从全局改善对齐
  - **LLM 自评天花板**: 评分稳定在 35/40，无法区分细微质量差异

### 4. LLM 自评分辨率不足

- 所有候选的 LLM 自评分都在 32-36/40 范围内
- 无法有效区分对齐好 (0.46) vs 对齐差 (0.35) 的译文
- 建议: 未来可用更细粒度的逐段评估替代全文评估

## 最终最优配置

```python
# phase2_iterative.py 最优参数
GROUP_SIZE = 5          # ||| 标记分组大小
n_initial = 3           # 初始候选数 (T=0.3, 0.5, 0.7)
max_iter = 5            # 最大迭代轮数
termination = 3         # 连续未改善轮数
embedding = "paraphrase-multilingual-MiniLM-L12-v2"
```

## 产出文件

- `phase2_iterative.py`: 迭代翻译优化脚本
- `output/zjMuIxRvygQ/segments_cache_phase2_iterative.json`: 最优切分结果
- `output/zjMuIxRvygQ/audit/phase2_iterative_log.json`: 实验日志
- `output/zjMuIxRvygQ/phase2_best_translation.txt`: 最优全文译文

## 后续方向

1. **方向 2 (Sentence Alignment) 仍有价值**: 可将 embedding 对齐用于 DP 切分的辅助约束，而不仅是评估
2. **方向 3 (LLM 二步切分)**: 让 LLM 输出带 ||| 的全文翻译后，用 LLM 做语义感知切分
3. **多视频验证**: 当前仅在 zjMuIxRvygQ (73段) 上测试，需扩展到不同类型视频
4. **迭代策略改进**: 尝试 "重新翻译特定组" 而非 "全文修改"，避免全局退化
