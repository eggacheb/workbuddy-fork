#!/usr/bin/env bash
# 把这份 clone 里的技能文件同步到 WorkBuddy 实际加载的目录。
#
# 为什么要它:WorkBuddy 读的是 ~/.workbuddy/skills/fork/ 这份副本,
# 在 clone 里改代码、push,那边不会跟着变。忘了同步就等于跑旧代码,
# 而且没有任何提示——和本项目一直在修的那类「静默错数据」是同一个病。
#
# 用法:
#   scripts/sync-skill.sh                  手动同步一次
#   scripts/sync-skill.sh --install-hooks  装 post-commit / post-merge 钩子
#
# 两个目标:
#   ~/.workbuddy/skills/fork/  (WORKBUDDY_SKILLS_DIR 可覆盖)  —— 总是同步
#   ~/.dsh/skills/fork/        (DSH_SKILLS_DIR 可覆盖)        —— 只在装了 dsh 时同步
# 没装 dsh 的人不该因为跑一次同步就多出一个 ~/.dsh 目录。
set -euo pipefail

src="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="${WORKBUDDY_SKILLS_DIR:-$HOME/.workbuddy/skills/fork}"
dsh_dest="${DSH_SKILLS_DIR:-$HOME/.dsh/skills/fork}"
hook_dir="$src/.git/hooks"

install_hooks() {
    [ -d "$hook_dir" ] || { echo "不是 git 仓库,跳过装钩子:$src" >&2; return 0; }
    local hook
    for hook in post-commit post-merge; do
        cat >"$hook_dir/$hook" <<'HOOK'
#!/usr/bin/env bash
# 提交/合并后把技能文件同步到 WorkBuddy 的 skills 目录。
# 同步失败不该挡住提交,所以只警告不退出非零。
root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ -x "$root/scripts/sync-skill.sh" ] || exit 0
"$root/scripts/sync-skill.sh" || echo "(警告:技能同步失败,skills 目录可能还是旧代码)" >&2
HOOK
        chmod +x "$hook_dir/$hook"
        echo "已装钩子:${hook}"
    done

    # 提交前跑测试。这个仓库历史上的 P0 全是「照常打印已导出、退出码 0、内容错的」,
    # 没有这道闸,用例就只是一份文档。
    cat >"$hook_dir/pre-commit" <<'HOOK'
#!/usr/bin/env bash
# 提交前跑全量测试。不过就拦下提交。
# 确实要跳过(比如只改了文档):git commit --no-verify
root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ -d "$root/tests" ] || exit 0
python -m unittest discover -s "$root/tests" -q || {
    echo "" >&2
    echo "测试没过,提交已拦下。修完再提交;确实要跳过用 git commit --no-verify。" >&2
    exit 1
}
HOOK
    chmod +x "$hook_dir/pre-commit"
    echo "已装钩子:pre-commit"
}

sync_into() {
    local target="$1"
    mkdir -p "$target/scripts"

    local f
    for f in SKILL.md README.md LICENSE; do
        [ -f "$src/$f" ] && cp -f "$src/$f" "$target/$f"
    done

    shopt -s nullglob
    local py
    for py in "$src"/scripts/*.py; do
        cp -f "$py" "$target/scripts/"
    done

    # 删掉源里已经没有的旧副本,否则改名/删文件后 skills 目录会留着幽灵脚本
    for py in "$target"/scripts/*.py; do
        [ -f "$src/scripts/$(basename "$py")" ] || rm -f "$py"
    done
    shopt -u nullglob

    echo "已同步到:$target"
}

sync_once() {
    sync_into "$dest"

    # 只在 dsh 的 skills 目录已经存在时同步:没装 dsh 的机器上跑一次同步,
    # 不该凭空多出一个 ~/.dsh。
    local dsh_root
    dsh_root="$(dirname "$dsh_dest")"
    if [ -d "$dsh_root" ] || [ -n "${DSH_SKILLS_DIR:-}" ]; then
        sync_into "$dsh_dest"
    else
        echo "跳过 dsh:$dsh_root 不存在(没装 dsh 就不用管)" >&2
    fi
}

case "${1:-}" in
    --install-hooks) install_hooks ;;
    "") sync_once ;;
    *) echo "用法:$0 [--install-hooks]" >&2; exit 2 ;;
esac
