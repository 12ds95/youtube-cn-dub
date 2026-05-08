#!/usr/bin/env python3
"""检测 git 暂存区文件是否泄漏 API key / token。

用作 pre-commit hook (退出码 1 阻止提交)。

使用:
  scripts/check_api_keys.py            # 检查暂存区 (pre-commit hook 用)
  scripts/check_api_keys.py file1 ...  # 检查指定文件 (CI / 手动)
  scripts/check_api_keys.py --all      # 扫描整个工作区 (定期审计用)

安装 git hook: scripts/install_git_hooks.sh
"""
import os
import re
import subprocess
import sys
from typing import List, Tuple

# ── 检测模式 ──────────────────────────────────────────────────────
# 每条规则: (pattern, 名称, 是否需 entropy 检查)
PATTERNS: List[Tuple[str, str, bool]] = [
    # 高置信度 (固定前缀, 几乎无误报)
    (r'sk-sp-[a-zA-Z0-9]{20,}',                'sk-sp 风格 key',     False),
    (r'sk-ant-api\d+-[a-zA-Z0-9_\-]{80,}',     'Anthropic API key',  False),
    (r'sk-proj-[a-zA-Z0-9_\-]{40,}',           'OpenAI project key', False),
    (r'sk-[a-zA-Z0-9]{40,}',                   'OpenAI 风格 key',    False),
    (r'xox[pbara]-\d+-\d+-\d+-[a-fA-F0-9]+',   'Slack token',        False),
    (r'\bAKIA[0-9A-Z]{16}\b',                  'AWS access key',     False),
    (r'AIza[0-9A-Za-z_\-]{35}',                'Google API key',     False),
    (r'\bghp_[a-zA-Z0-9]{36}\b',               'GitHub PAT',         False),
    (r'github_pat_[a-zA-Z0-9_]{82}',           'GitHub fine-grained',False),
    (r'\bgho_[a-zA-Z0-9]{36}\b',               'GitHub OAuth token', False),
    (r'\bglpat-[a-zA-Z0-9_\-]{20}\b',          'GitLab PAT',         False),
    # 中置信度 (JSON/YAML 配置中的字段, 配合 entropy 减误报)
    (r'"api[_-]?key"\s*:\s*"([a-zA-Z0-9_\-]{16,})"',     'JSON api_key 字段',     True),
    (r'"secret[_-]?key"\s*:\s*"([a-zA-Z0-9_\-]{16,})"',  'JSON secret_key 字段',  True),
    (r'"access[_-]?token"\s*:\s*"([a-zA-Z0-9_\-]{16,})"','JSON access_token 字段',True),
    (r'(?im)^api[_-]?key\s*[:=]\s*([a-zA-Z0-9_\-]{16,})','配置 api_key 行',       True),
]

# 已知的占位符 (不是真 key)
ALLOWLIST_VALUES = {
    'YOUR_API_KEY', 'YOUR_KEY_HERE', 'your-api-key-here', 'your_api_key',
    '<API_KEY>', '<your_api_key>', 'CHANGE_ME', 'PASTE_YOUR_KEY',
    'replace-with-your-key', 'sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
}

# 跳过的扩展名 (二进制 / 模型 / 资产)
SKIP_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico',
    '.pdf', '.zip', '.tar', '.gz', '.7z',
    '.bin', '.onnx', '.pt', '.pth', '.ct2', '.safetensors',
    '.mp3', '.mp4', '.wav', '.ogg', '.webm',
    '.pyc', '.pyo', '.so', '.dylib', '.dll',
    '.lock',  # uv.lock / package-lock 通常不含 key
}

# 跳过的路径 (示例 / fixture)
SKIP_PATH_KEYWORDS = (
    'config.example.json',
    'tests/fixtures/',
    'docs/',  # docs 中的示例代码片段
    '/.venv/',
    'venv/',
    '__pycache__/',
)


def shannon_entropy(s: str) -> float:
    """简易 Shannon entropy. 真随机 key entropy ≥ 4.0."""
    if not s:
        return 0.0
    from collections import Counter
    import math
    counts = Counter(s)
    total = len(s)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def get_staged_files() -> List[str]:
    """返回 git 暂存区中已添加/修改/重命名的文件列表."""
    try:
        out = subprocess.check_output(
            ['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR'],
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    return [f for f in out.strip().split('\n') if f]


def get_all_tracked_files() -> List[str]:
    out = subprocess.check_output(['git', 'ls-files'], text=True)
    return [f for f in out.strip().split('\n') if f]


def check_file(path: str) -> List[Tuple[str, int, str, str]]:
    """检查单个文件, 返回发现的 (path, line, name, masked_value)."""
    if not os.path.exists(path) or not os.path.isfile(path):
        return []
    if any(path.endswith(ext) for ext in SKIP_EXTENSIONS):
        return []
    if any(kw in path for kw in SKIP_PATH_KEYWORDS):
        return []

    try:
        with open(path, encoding='utf-8') as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return []

    findings: List[Tuple[str, int, str, str]] = []
    for pattern, name, need_entropy in PATTERNS:
        for m in re.finditer(pattern, content):
            full_match = m.group()
            value = m.group(1) if m.groups() else full_match
            if value in ALLOWLIST_VALUES or full_match in ALLOWLIST_VALUES:
                continue
            # entropy 过滤: 配置字段值若 entropy < 3.5 (太规整, 多为占位符)
            if need_entropy and shannon_entropy(value) < 3.5:
                continue
            line_no = content[:m.start()].count('\n') + 1
            masked = value[:6] + '...' + value[-4:] if len(value) > 14 else value
            findings.append((path, line_no, name, masked))
    return findings


def main() -> int:
    args = sys.argv[1:]
    if args == ['--all']:
        files = get_all_tracked_files()
        scope = '工作区全量'
    elif args:
        files = args
        scope = '指定文件'
    else:
        files = get_staged_files()
        scope = '暂存区'

    if not files:
        return 0

    all_findings: List[Tuple[str, int, str, str]] = []
    for f in files:
        all_findings.extend(check_file(f))

    if not all_findings:
        return 0

    print(f'🚨 [{scope}] 检测到可能的 API key/token 泄漏:')
    print()
    for path, line, name, masked in all_findings:
        print(f'  {path}:{line}  [{name}]  {masked}')
    print()
    print('请采取以下行动之一:')
    print('  1. 移除该 key (推荐): git restore --staged <file> && 编辑后重新 add')
    print('  2. 该 key 是占位符: 加到 scripts/check_api_keys.py ALLOWLIST_VALUES')
    print('  3. 整个文件应忽略: 加到 .gitignore 或 SKIP_PATH_KEYWORDS')
    print('  4. 紧急绕过 (不推荐): git commit --no-verify')
    return 1


if __name__ == '__main__':
    sys.exit(main())
