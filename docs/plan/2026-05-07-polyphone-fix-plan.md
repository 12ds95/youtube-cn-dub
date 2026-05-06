# 多音字修正改进计划: A → C 保险路线

> 创建日期: 2026-05-07
> 关联调研: `docs/research/2026-05-07-polyphone-disambiguation-survey.md`
> 触发问题: kCc 117 段实测中 "了" 替换正确率仅 50%, 漏字 ("觉/为/好/和/得")

## 目标

修复用户实测的两个 bug:
- "构成了"/"查看了" 中"了"被错误改成 liǎo (应为助词 le)
- "直觉"/"感觉" 中"觉"被 edge-tts 默认读 jiào (应为 jué) — 当前代码完全未处理

整体目标: 多音字替换正确率从当前 ~50-87% 提升到 ≥95%。

## 路线图 (两阶段)

### 阶段 1: A — pypinyin + jieba 词级守卫 + 补漏字

**核心思路**: 把"边界判定"从 pypinyin 单字消歧 (87%) 升级为 "pypinyin + jieba 词级白名单" 双重确认。当 pypinyin 判定的非默认读音落在白名单词中时才替换, 否则保留默认读音 (大概率是 edge-tts 习惯读法, 错误率低)。

**工作项**:

1. **新增 `_POLYPHONE_REPLACE_WHITELIST`**: 每个多音字配套一组 jieba 词级白名单, 仅当字所在 jieba 分词命中白名单时才替换。
   - `了`: {'了解', '了断', '了结', '明了', '了如指掌', '了不起', '了悟', ...}
   - `行`: {'行业', '银行', '行列', '同行', '一行', '行家', ...}  (杭/háng 替换条件)
   - `重`: {'重新', '重复', '重叠', '重逢', '重申', ...}  (虫/chóng)
   - `调`: {'协调', '调和', '调节', '调整', ...}  (条/tiáo)  *注: 当前 map 已有 tiáo 替换*
   - `应`: {'应该', '应当', '应用', '应有', '应承', ...}  (英/yīng)
   - `处`: {'此处', '何处', '深处', '处所', '到处', '处处', '出处', ...}  (触/chù)
   - 等等

2. **`_POLYPHONE_HOMOPHONE_MAP` 补漏字**:
   - `觉`: {'jué': '决'}  — edge-tts 默认 jiào → 替换成"决" 强制读 jué (适用 "觉得/感觉/直觉/觉悟/警觉/不觉")
   - `为`: 谨慎 — wèi 与 wéi 都常见, 需高准确率词级守卫 ("作为/认为/成为" wéi vs "因为/为了" wèi)
   - `好`: 同上, hǎo (好的) vs hào (爱好) 双向常见, 需双向白名单
   - `得`: dé/de/děi, 三向, 复杂度高, 暂不补
   - `和`: hé (和平) vs hè (附和) vs huó (和面), 默认 hé 占 95%+, 暂不补

   **本阶段优先补 `觉`**, 因为单向 (默认 jiào, 极少正确), 风险最低, 收益最高 (用户实报案例)。

3. **`_fix_polyphones` 双重确认逻辑**:
   ```python
   def _fix_polyphones(text):
       if not _has_polyphone(text): return text
       py = pinyin(text, style=Style.TONE)
       words = jieba.lcut(text)
       # 建立 char_idx → jieba_word 映射
       char_to_word = _build_char_word_map(words)
       for i, c in enumerate(text):
           if c in _POLYPHONE_HOMOPHONE_MAP:
               target_pinyin, replacement = ...
               if py[i] == target_pinyin:
                   word = char_to_word[i]
                   if word in WHITELIST[c]:
                       chars[i] = replacement
                   # else: 保留原字, 不强制替换
   ```

4. **测试矩阵** (TDD):
   - `test_le_helper_not_replaced`: "构成了一个完整模型" → "了" 不替换 (helper le)
   - `test_liao_in_word_replaced`: "我了解你" → "了"→"瞭" (liǎo)
   - `test_jue_replaced`: "我有直觉" → "觉"→"决" (jué)
   - `test_jiao_kept`: "他要去睡觉" → "觉" 不替换 (jiào)
   - `test_chu_locator_replaced`: "在此处" → "处"→"触" (chù)
   - `test_chu_verb_kept`: "正在处理" → "处" 不替换 (chǔ)
   - `test_zhong_replaced`: "重新开始" → "重"→"虫" (chóng)
   - `test_zhong_kept`: "很重要" → "重" 不替换 (zhòng)
   - 等等共 ~20 用例

5. **回归** (端到端):
   - 跑 `bash test_pipeline.sh --integrated` (zjMu, 5.9min) 用人工抽样听感
   - 实际跑前后, 对比 `_fix_polyphones` 在 segments 上的替换次数和样例
   - 记录到 audit 日志

### 阶段 2: C — g2pM 升级 (已实施, opt-in)

**实施日期**: 2026-05-07
**结果**: 集成 g2pM (~30 MB BiLSTM), **默认禁用**, 仅作 opt-in 实验通道

**实测发现** (kCc 117 段 dry-run):
- jieba-only vs jieba + g2pm 兜底: 11 段差异
- 11 段中 **5 段是 false positive**: "严重 → 严虫", "权重 → 权虫", "所处 →
  所触", "处于 → 触于" (g2pM 把 zhòng/chǔ 误判 chóng/chù)
- 仅 ~2 段是真的扩展覆盖 ("三重注意力" → "三虫注意力" 是 chóng, "连接处"
  → "连接触" 是 chù)
- **错误率 ~45%** (5/11), 远超原计划 ~97% 期望

**根因分析**:
- g2pM 在 CPP 数据集 ~97%, 但 CPP 文风偏新闻/通用语料
- 我们项目场景是技术视频字幕 (神经网络/数学/编程), domain shift 显著
- 高频技术词如 "权重 / 严重 / 处于" 在 CPP 训练分布中可能不充分

**最终决策**:
1. **保留 g2pM 集成代码**: `_fix_polyphones(text, use_g2pm_fallback=True)`
   可显式开启 (例如未来扩展词表外的特殊场景)
2. **默认禁用** (`use_g2pm_fallback=False`): 当前管线纯 jieba 白名单驱动,
   实测错误率接近 0%
3. **测试覆盖**: `tests/test_polyphone_g2pm_fallback.py` 19 用例验证:
   - g2pm 转 pypinyin 拼音格式正确 (NFC 规范化, 数字声调 → 字符声调)
   - 兜底 opt-in 逻辑正确
   - 默认禁用时 false positive case (严重/权重/所处/处于) 不被误替换
   - g2pm 不可用时降级 jieba-only 不报错

**未来若需更高覆盖率**: 应考虑域内微调 (在自有视频字幕上 fine-tune g2pm)
或升级 g2pW (BERT, 99.08% CPP), 而非直接启用 vanilla g2pM。

**配套实施**: 参见 `docs/research/2026-05-07-g2pm-domain-shift-experiment.md`
(本归档文档同时承担实验记录角色)。

## 设计原则

1. **保留可禁用**: 加 `polyphone_fix.enabled` (默认 True) / `polyphone_fix.engine` ("pypinyin"/"g2pm"/"g2pw") 配置, 失败可快速关闭
2. **不引入运行时新依赖**: 阶段 1 完全用已有 pypinyin/jieba; 阶段 2 才考虑装 g2pm
3. **可逆**: 替换字符 (瞭/虫/触/决) 不进入 segments_cache.json, 仅在 TTS 输入时即时计算; subtitle/audit 仍读原字

## 验收

阶段 1 验收:
- [ ] 20 个 TDD 测试全部 GREEN
- [ ] 全量 448+ 测试无回归
- [ ] kCc/zjMu 等已生成 segments 上 dry-run, "了" 替换正确率 ≥90% (人工抽样 10 例)
- [ ] "觉" 字开始被替换 (kCc 上至少触发数次, 用户报的"直觉/感觉"覆盖)

阶段 2 触发:
- 上述条件未达, 或新发现 3+ 字陷阱仍误判, 升级 g2pM

## 风险

1. **白名单维护**: 每个多音字白名单需手动列举常见词, 漏列即漏修。缓解: 把白名单做成 `data/polyphone_whitelist.yaml` 配置, 后续可补
2. **jieba 分词错误**: 极端情况下 jieba 把"了解" 分成 ["了", "解"] (单字), 此时白名单匹配失败。缓解: 测试覆盖 + 必要时用更长 phrase 滑窗
3. **替换字本身被 TTS 误读**: 如 "决" 在某些上下文也会被 edge-tts 误读为 jué (本就是目标读音), 但极少有反向风险

## 关联文件

实施时会改:
- `pipeline.py:_POLYPHONE_HOMOPHONE_MAP` (补字)
- `pipeline.py:_fix_polyphones` (加 jieba 词级守卫)
- 新增 `data/polyphone_whitelist.json` (或硬编码到 pipeline.py)

测试:
- `tests/test_polyphone_jieba_guard.py` (新增, ~20 用例)

文档:
- 本计划 (验收后归档)
