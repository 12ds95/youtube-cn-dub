# Project Guidelines

## API Key 安全规则

**绝对禁止**在脚本、代码、文档中硬编码真实 API Key。

- `config.json` 已在 `.gitignore` 中，所有 API Key 只能存放在 `config.json`
- Shell 脚本需要 API Key 时，必须从 `config.json` 动态读取：
  ```bash
  LLM_API_KEY=$(python3 -c "import json; c=json.load(open('config.json')); print(c.get('llm',{}).get('api_key',''))")
  ```
- 生成临时配置文件时，用变量引用 `$LLM_API_KEY`，不写明文
- `config.example.json` 中用 `""` 或 `"sk-your-key-here"` 作为占位符
- 如果发现已泄漏的 Key，提醒用户立即轮换

## 文档归档规则

WebSearch 调研、plan mode 计划、研究型探索实验等工作，必须将细节归档记录到 `docs/` 目录下，方便回顾：

- `docs/research/` — WebSearch 调研、算法设计、实验过程与结论
- `docs/plan/` — 实施计划、架构设计
- `docs/spec/` — 功能规格、路线图

命名格式: `YYYY-MM-DD-<topic-slug>.md`

归档内容应包含：问题背景、调研的方案对比、选择理由、算法/实现细节、实验数据与结论。
