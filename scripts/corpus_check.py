#!/usr/bin/env python3
"""全库语料回归:把「剥噪音有没有顺手删掉真信息」变成一条能重跑的命令。

单测只覆盖想得到的边界;这里跑的是这台机器上真实发生过的全部会话。
改完剥离规则先跑它,再看数字有没有掉。

    python scripts/corpus_check.py                       报当前数字
    python scripts/corpus_check.py --save base.json      存成基线
    python scripts/corpus_check.py --compare base.json   和基线比
    python scripts/corpus_check.py --equiv HEAD~1        和某个提交的版本比输出

退出码:0 正常;1 有指标变差(内容被吃掉 / 体外残留变多 / 有幽灵发言)/ 输出有差异;
2 用法或环境出错。

为什么临时文件走 tempfile.gettempdir():Git Bash 的 /tmp 在 Windows 版
Python 眼里不是那个路径,照着写会静默比对两份不存在的文件——这个坑踩过一次,
那次 18 组「全部一致」是假的。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fork  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FORK_PY = REPO / "scripts" / "fork.py"

# 会话数会随聊天增长,原始消息数也是;所以只把「变少」当回归信号。
GROWING = {"会话数", "原始 user 消息"}

# 这一项反过来:**变多**才是坏消息。它数的是「有 query 的发言里,体外剥完还剩字」
# 的条数——剩下的每个字都是平台塞进来而我们的规则没认出来的东西,或者是「真话被
# 挪到 query 外面」的早期警报。纯 [图片] 占位不算(那是脚本自己渲染的)。
# 注意「计入发言」发现不了后一种:真话挪到体外时体内照样有字,计数不会掉。
BAD_WHEN_UP = {"体外残留"}


def collect(projects_dir: Path) -> dict:
    """扫一遍,返回各项计数。"""
    stats = {
        "会话数": 0,
        "原始 user 消息": 0,
        "计入发言": 0,
        "整条丢掉": 0,
        "长引用占位": 0,
        "幽灵发言": 0,
        "体外残留": 0,
    }
    for p in sorted(projects_dir.glob("*/*.jsonl")):
        if not p.is_file():
            continue
        stats["会话数"] += 1
        for r in fork.load_records(p):
            if r.get("type") != "message" or r.get("role") != "user":
                continue
            stats["原始 user 消息"] += 1
            raw = fork.msg_text(r)
            if fork.outer_residue(raw):
                stats["体外残留"] += 1
            said = fork.clean_user(raw)
            if not said:
                stats["整条丢掉"] += 1
                continue
            stats["计入发言"] += 1
            stats["长引用占位"] += said.count("[长引用")
            # 剥完只剩标签就说明有一条发言是凭空多出来的,编号会被顶掉
            if not re.sub(r"<[^>]+>", "", said).strip():
                stats["幽灵发言"] += 1
    return stats


def display_width(text: str) -> int:
    """中文按两格算,否则这张表在等宽字体里是歪的。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def print_stats(stats: dict):
    width = max(display_width(k) for k in stats)
    for key, value in stats.items():
        print(f"  {pad(key, width)}  {value:>7}")


def compare(now: dict, base: dict) -> int:
    """任何一项变差都算回归。

    方向有两类,不写反:**吃掉真实内容**的指标(计入发言、长引用占位)变少是坏;
    **体外残留**变多是坏。两者都不看「变好」的方向——那说明规则认出了新的东西,
    只该改基线,不该报警。
    """
    rows, worse = [], []
    for key, old in base.items():
        new = now.get(key)
        if new is None or key in GROWING:
            continue
        delta = new - old
        rows.append((key, old, new, delta))
        bad_direction = delta > 0 if key in BAD_WHEN_UP else delta < 0
        if bad_direction:
            worse.append((key, old, new, delta))

    for key, old, new, delta in rows:
        if key in BAD_WHEN_UP:
            mark = "  ← 变多了" if delta > 0 else ""
        else:
            mark = "  ← 变少了" if delta < 0 else ""
        print(f"  {pad(key, 16)} {old:>7} → {new:>7}  ({delta:+d}){mark}")

    if now.get("幽灵发言"):
        print(f"\n幽灵发言 {now['幽灵发言']} 条:体内只剩标签的注入被算成了用户发言。")
        return 1
    if worse:
        lost = [w for w in worse if w[0] not in BAD_WHEN_UP]
        grew = [w for w in worse if w[0] in BAD_WHEN_UP]
        if lost:
            print(f"\n{len(lost)} 项指标变少,疑似剥掉了真实内容。逐条回原文核对。")
        if grew:
            print(f"\n{len(grew)} 项指标变多(体外残留):平台往 <user_query> 外面塞了"
                  "没认出来的东西。先确认那不是用户说的话——是的话,判据要改,不要放行。")
        return 1
    print("\n没有指标变差。")
    return 0


def equiv(ref: str, projects_dir: Path, limit: int) -> int:
    """和某个提交的版本逐字节比输出:stdout、stderr、退出码三样都比。"""
    old = subprocess.run(["git", "-C", str(REPO), "show", f"{ref}:scripts/fork.py"],
                         capture_output=True)
    if old.returncode:
        print(f"取不到 {ref}:scripts/fork.py", file=sys.stderr)
        return 2

    tmp = Path(tempfile.gettempdir()) / f"corpus_equiv_{os.getpid()}.py"
    tmp.write_bytes(old.stdout)
    try:
        logs = sorted((p for p in projects_dir.glob("*/*.jsonl") if p.is_file()),
                      key=lambda p: p.stat().st_size, reverse=True)
        # 当前会话还在长,导出结果必然不同,排除掉
        current = os.environ.get("CODEBUDDY_SESSION_ID", "")
        targets = [p for p in logs if p.stem != current][:limit]
        if not targets:
            print("没有可比的会话。", file=sys.stderr)
            return 2

        modes = (["--compact"], [], ["--from", "2", "--last", "5"])
        differing = 0
        for p in targets:
            before = differing
            for mode in modes:
                a = subprocess.run([sys.executable, str(tmp), "-s", p.stem, "--stdout", *mode],
                                   capture_output=True)
                b = subprocess.run([sys.executable, str(FORK_PY), "-s", p.stem, "--stdout", *mode],
                                   capture_output=True)
                same = (a.returncode == b.returncode
                        and a.stdout == b.stdout
                        and a.stderr == b.stderr)
                if not same:
                    differing += 1
                    print(f"  差异  {p.stem[:8]}  {str(mode):26}  "
                          f"rc={a.returncode}/{b.returncode}  {len(a.stdout)}/{len(b.stdout)} 字节")
            if differing == before:
                print(f"  一致  {p.stem[:8]}")
        total = len(targets) * len(modes)
        print(f"\n{total - differing}/{total} 组一致(vs {ref})")
        return 1 if differing else 0
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="全库语料回归")
    ap.add_argument("--home", help="覆盖 projects 目录(默认取 WORKBUDDY_HOME)")
    ap.add_argument("--save", metavar="FILE", help="把当前数字存成基线")
    ap.add_argument("--compare", metavar="FILE", help="和基线比,任何一项变少就非零退出")
    ap.add_argument("--equiv", metavar="REF",
                    help="和这个提交的 fork.py 比输出(如 HEAD~1)")
    ap.add_argument("--limit", type=int, default=8, help="--equiv 比几个会话,默认 8")
    args = ap.parse_args()

    projects_dir = Path(args.home) if args.home else fork.PROJECTS_DIR
    if not projects_dir.is_dir():
        print(f"找不到会话目录:{projects_dir}", file=sys.stderr)
        print("用 --home 指定,或检查 WORKBUDDY_HOME。", file=sys.stderr)
        return 2

    if args.equiv:
        return equiv(args.equiv, projects_dir, args.limit)

    stats = collect(projects_dir)
    print(f"{projects_dir}\n")
    print_stats(stats)

    if args.save:
        Path(args.save).write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\n基线已存:{args.save}")
        return 0

    if args.compare:
        base = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        print(f"\n和基线比({args.compare}):")
        return compare(stats, base)

    return 0


if __name__ == "__main__":
    sys.exit(main())
