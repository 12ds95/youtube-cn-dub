#!/usr/bin/env bash
# 安装 git pre-commit hooks (API key 泄漏检测).
#
# 用法: bash scripts/install_git_hooks.sh
#
# 安装后, 每次 git commit 自动跑 scripts/check_api_keys.py 扫描暂存区.
# 检测到泄漏退出码 1, 阻止提交.
#
# 紧急绕过: git commit --no-verify (不推荐, 仅在确认误报时使用).
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_DIR="$(git rev-parse --git-dir)/hooks"
HOOK_FILE="$HOOK_DIR/pre-commit"

mkdir -p "$HOOK_DIR"

# 备份已有 hook (若不是我们安装的)
if [ -f "$HOOK_FILE" ] && ! grep -q "check_api_keys.py" "$HOOK_FILE"; then
    BACKUP="$HOOK_FILE.backup.$(date +%Y%m%d_%H%M%S)"
    mv "$HOOK_FILE" "$BACKUP"
    echo "ℹ️  已有 pre-commit hook 备份到: $BACKUP"
fi

cat > "$HOOK_FILE" <<'EOF'
#!/usr/bin/env bash
# Auto-installed by scripts/install_git_hooks.sh
# 阻止含 API key 的提交.
set -e
REPO_ROOT="$(git rev-parse --show-toplevel)"
PY="${REPO_ROOT}/venv/bin/python3"
[ -x "$PY" ] || PY="python3"
exec "$PY" "${REPO_ROOT}/scripts/check_api_keys.py"
EOF
chmod +x "$HOOK_FILE"

echo "✅ pre-commit hook 已安装: $HOOK_FILE"
echo "   每次 git commit 会自动检测 API key 泄漏."
echo ""
echo "测试: bash scripts/install_git_hooks.sh && venv/bin/python3 scripts/check_api_keys.py --all"
