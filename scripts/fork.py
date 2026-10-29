#!/usr/bin/env python3
"""从 WorkBuddy 会话日志里抽出可读的对话主线。

只做机械提取,不做摘要。摘要/压缩交给调用它的模型——
这样即使上下文已经被 compact 过,原文仍然完整。

会话日志位置: ~/.workbuddy/projects/<项目slug>/<sessionId>.jsonl
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WB_HOME = Path(os.environ.get("WORKBUDDY_HOME") or (Path.home() / ".workbuddy"))
PROJECTS_DIR = WB_HOME / "projects"

REMINDER_RE = re.compile(r"<system-reminder[\s\S]*?</system-reminder>")
QUERY_RE = re.compile(r"<user_query>([\s\S]*?)</user_query>")

# 平台/队友/后台任务塞进用户消息的块,不是用户说的话
INJECTED_BLOCKS = [
    "task-notification",
    "teammate-message",
    "conversation_history_summary",
    "cb_summary",
    "previous_user_message",
    "previous_assistant_message",
    "previous_tool_call",
    "user-prompt-submit-hook",
    "additional-data",
    "message-queue",
]
INJECTED_RES = [re.compile(rf"<{tag}\b[\s\S]*?</{tag}>") for tag in INJECTED_BLOCKS]
STARTS_WITH_TAG_RE = re.compile(r"^<[a-zA-Z][\w-]*[\s>/]")

# 标签外面还挂着的 harness 指令,整段都不是用户说的
NOISE_RES = [
    re.compile(r"^Use the \w+ tool with [\s\S]*$"),
    re.compile(r"^Please continue with the conversation based on the summarized context[\s\S]*$"),
    re.compile(r"^This session is being continued from[\s\S]*$"),
]

# 工具调用里最该留下的一行参数
ARG_PRIORITY = ["file_path", "path", "command", "pattern", "url", "query", "prompt", "description"]

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell", "run_command"}

# 报错行:只认像日志的那几行,长行(多半是文件内容)跳过
ERR_LINE_RE = re.compile(
    r"(?i)\b(error|exception|traceback|fatal|failed|denied|invalid|missing|not found|cannot|unable to)\b"
    r"|错误:|失败:|报错:|不存在|不会被识别"
)
ERR_SKIP_RE = re.compile(r"(?i)no (?:errors?|failures?)|0 (?:errors?|failed)|\bwithout error|错误\\?\": ?0")
# 文档/表格里的句子也会命中关键词,按行首形态排除
ERR_SKIP_PREFIX = ("- ", "* ", "|", "#", ">", "+ ")
# 探路时的鸡毛蒜皮,别占位置
ERR_TRIVIAL_RE = re.compile(
    r"(?i)(^|\s)(ls|cat|head|tail|stat|cd): |updater:|\[(warn|info|debug)\]"
    r"|not a git repository|^\s*(except|try|if|for|while|def|class|const|let|var|function|import|export|\}\s*$)"
)
# 真正的故障长这样
ERR_STRONG_RE = re.compile(
    r"(?i)(error:|exception|traceback|fatal|denied|eaddrinuse|missing_|cannot read private"
    r"|invalid output|is not a function|is not callable|module not found|timed out)"
)

# 压缩版的截断长度
COMPACT_USER = 400
COMPACT_SAY = 200


# ---------- 定位 ----------

def find_log(session_id: str):
    """按会话 ID 找 jsonl。不猜项目 slug,直接 glob。"""
    hits = list(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    if not hits:
        hits = list(PROJECTS_DIR.rglob(f"{session_id}*.jsonl"))
    hits = [p for p in hits if p.is_file()]
    if not hits:
        return None
    return max(hits, key=lambda p: p.stat().st_size)


def all_logs():
    return sorted(
        (p for p in PROJECTS_DIR.glob("*/*.jsonl") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def load_records(path: Path):
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def rec_ts(r):
    v = r.get("timestamp")
    return v if isinstance(v, (int, float)) else 0


def read_title(records) -> str:
    for r in records:
        if r.get("type") == "ai-title":
            for key in ("title", "text", "content", "aiTitle"):
                v = r.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return ""


def read_cwd(records) -> str:
    """取被导出会话自己的工作目录——不是当前会话的。"""
    seen = Counter(
        r.get("cwd") for r in records
        if isinstance(r.get("cwd"), str) and r.get("cwd").strip()
    )
    if seen:
        return seen.most_common(1)[0][0]
    return os.environ.get("CODEBUDDY_PROJECT_DIR") or ""


def short_path(text: str, limit: int) -> str:
    """按词截断,别把命令腰斩在半个参数上。"""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


# ---------- 提取 ----------

def part_text(part) -> str:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return ""
    ptype = part.get("type")
    if ptype in ("input_text", "output_text", "text"):
        return part.get("text") or ""
    if ptype in ("image", "input_image", "image_url"):
        return "[图片]"
    return ""


def msg_text(record) -> str:
    content = record.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(part_text(p) for p in content if part_text(p))


def clean_user(text: str) -> str:
    """剥掉平台注入的上下文,只留用户真的说过的话。"""
    text = REMINDER_RE.sub("", text)
    queries = [q.strip() for q in QUERY_RE.findall(text) if q.strip()]
    if queries:
        return "\n\n".join(queries)
    for pattern in INJECTED_RES:
        text = pattern.sub("", text)
    text = text.strip()
    for pattern in NOISE_RES:
        text = pattern.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # 剥完还整段是协议标签的,不是人话
    if STARTS_WITH_TAG_RE.match(text):
        return ""
    return text


def parse_args(record) -> dict:
    args = record.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return {}
    return args if isinstance(args, dict) else {}


def brief_call(record, args=None) -> str:
    name = record.get("name") or "?"
    if args is None:
        args = parse_args(record)
    for key in ARG_PRIORITY:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            detail = short_path(val, 140)
            return f"{name}({detail})"
    return name


def result_text(record) -> str:
    out = record.get("output")
    if isinstance(out, str):
        return out
    if isinstance(out, dict):
        if isinstance(out.get("text"), str):
            return out["text"]
        inner = out.get("content")
        if isinstance(inner, list):
            return "\n".join(part_text(p) for p in inner if part_text(p))
    if isinstance(out, list):
        return "\n".join(part_text(p) for p in out if part_text(p))
    return ""


def scan_errors(text: str):
    """从工具结果里挑出像报错的行。只看前 300 行,长日志尾部没信息量。"""
    hits = []
    for line in text.splitlines()[:300]:
        s = line.strip()
        if not s or len(s) > 260:
            continue
        if s.startswith(ERR_SKIP_PREFIX) or ERR_TRIVIAL_RE.search(s):
            continue
        if ERR_LINE_RE.search(s) and not ERR_SKIP_RE.search(s):
            hits.append(s)
        if len(hits) >= 3:
            break
    return hits


def build_turns(records):
    """按时间排序,把每条用户发言当成一个 turn 的起点。

    顺手把工具调用的原始参数和报错行挂在 turn 上——只留事实,不留全文。
    """
    records = sorted(records, key=rec_ts)
    turns = []
    cur = None
    for r in records:
        rtype = r.get("type")
        if rtype == "message":
            role = r.get("role")
            if role == "user":
                said = clean_user(msg_text(r))
                if not said:
                    continue
                cur = {"user": said, "blocks": [], "calls": [], "errors": []}
                turns.append(cur)
            elif role == "assistant":
                said = msg_text(r).strip()
                if not said:
                    continue
                if cur is None:
                    cur = {"user": None, "blocks": [], "calls": [], "errors": []}
                    turns.append(cur)
                cur["blocks"].append(("say", said))
        elif rtype == "function_call":
            if cur is None:
                continue
            args = parse_args(r)
            cur["blocks"].append(("call", brief_call(r, args)))
            cur["calls"].append({"name": r.get("name") or "?", "args": args})
        elif rtype == "function_call_result":
            if cur is None:
                continue
            for line in scan_errors(result_text(r)):
                cur["errors"].append(line)
    return turns


def merge_blocks(blocks):
    """相邻的助手发言合成一段,相邻的工具调用并成一列。"""
    merged = []
    for kind, val in blocks:
        if merged and merged[-1][0] == kind:
            if kind == "say":
                merged[-1] = ("say", merged[-1][1] + "\n\n" + val)
            else:
                merged[-1][1].append(val)
        else:
            merged.append(("say", val) if kind == "say" else ("call", [val]))
    return merged


def clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return " ".join(text[:limit].split()) + f" …(+{len(text) - limit} 字)"


def compact_blocks(blocks):
    """压缩版:助手发言截断,工具调用只留名字和次数。"""
    says = []
    names = Counter()
    order = []
    for kind, val in blocks:
        if kind == "say":
            says.append(clip(val, COMPACT_SAY))
        else:
            # blocks 里 call 是单条字符串,合并前的原始顺序
            name = val.split("(", 1)[0]
            names[name] += 1
            if name not in order:
                order.append(name)
    out = []
    if says:
        out.append("\n\n".join(says))
    if order:
        out.append("工具:" + " · ".join(f"{n}×{names[n]}" for n in order))
    return out


# ---------- 关键事实(机械提取) ----------

def file_arg(args: dict) -> str:
    for key in ("file_path", "path", "notebook_path"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().strip('"')
    return ""


def collect_facts(turns):
    edited = Counter()
    read = Counter()
    first_seen = {}
    cmds = []
    seen_cmd = set()
    errors = []
    seen_err = set()
    urls = []
    seen_url = set()

    for t in turns:
        for c in t["calls"]:
            name, a = c["name"], c["args"]
            if name in EDIT_TOOLS:
                p = file_arg(a)
                if p:
                    edited[p] += 1
                    first_seen.setdefault(p, len(first_seen))
            elif name == "Read":
                p = file_arg(a)
                if p:
                    read[p] += 1
            elif name in SHELL_TOOLS:
                cmd = a.get("command")
                if isinstance(cmd, str):
                    line = short_path(cmd.strip().split("\n")[0], 110)
                    if line and line not in seen_cmd:
                        seen_cmd.add(line)
                        cmds.append(line)
            elif name == "WebFetch":
                u = a.get("url")
                if isinstance(u, str) and u not in seen_url:
                    seen_url.add(u)
                    urls.append(u)
        for e in t["errors"]:
            key = e[:100]
            if key not in seen_err:
                seen_err.add(key)
                errors.append(e)

    return {
        "edited": edited.most_common(8),
        "read": read.most_common(4),
        "cmds": cmds[-6:],
        "errors": pick_errors(errors),
        "urls": urls[:4],
    }


def spread(items, n):
    """在整段里均匀取几个,别总是尾巴上的那几条。"""
    if len(items) <= n:
        return items
    if n == 1:
        return [items[-1]]
    idx = sorted({round(i * (len(items) - 1) / (n - 1)) for i in range(n)})
    return [items[i] for i in idx]


def pick_errors(lines, limit=4):
    """去重后优先留真故障,其次才留轻的。"""
    deduped, seen = [], set()
    for e in lines:
        key = e[:90]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    strong = [e for e in deduped if ERR_STRONG_RE.search(e)]
    return spread(strong, min(3, limit))


def render_facts(facts):
    lines = []
    if facts["edited"]:
        lines.append("- 改过的文件(按改动次数):")
        for p, n in facts["edited"]:
            lines.append(f"  - `{p}` ×{n}")
    if facts["cmds"]:
        lines.append("- 跑过的命令(有代表性):")
        for c in facts["cmds"]:
            lines.append(f"  - `{c}`")
    if facts["errors"]:
        lines.append("- 踩到的报错(原文):")
        for e in facts["errors"]:
            lines.append(f"  - {e}")
    if facts["urls"]:
        lines.append("- 查过的链接:")
        for u in facts["urls"]:
            lines.append(f"  - {u}")
    if facts["read"]:
        lines.append("- 读得最多的文件:")
        for p, n in facts["read"]:
            lines.append(f"  - `{p}` ×{n}")
    return lines


# ---------- 输出 ----------

def render_turns(turns, start=1, compact=False):
    parts = []
    for idx, turn in enumerate(turns, 1):
        if idx < start:
            continue
        user = turn["user"] or ""
        if compact:
            user = clip(user, COMPACT_USER)
        parts.append(f"## [{idx}] 用户\n\n{user}\n")
        if compact:
            chunks = compact_blocks(turn["blocks"])
            if chunks:
                parts.append(f"## [{idx}] 助手\n\n" + "\n\n".join(chunks) + "\n")
            continue
        merged = merge_blocks(turn["blocks"])
        if merged:
            buf = [f"## [{idx}] 助手\n"]
            for kind, val in merged:
                if kind == "say":
                    buf.append(val + "\n")
                else:
                    buf.append("调用的工具:\n" + "\n".join(f"- {c}" for c in val) + "\n")
            parts.append("\n".join(buf))
    return "\n".join(parts)


def fmt_time(value):
    """接受毫秒(日志内)或秒(文件 mtime),按量级区分。"""
    if not value:
        return "?"
    try:
        secs = value if value < 1e11 else value / 1000
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(secs))
    except Exception:
        return "?"


def package_skeleton(meta, facts):
    """给模型填空的上下文包骨架。机械部分先写好,人话部分留空。"""
    lines = [f"接着做 {meta['title'] or '<一句话说明这条线要干什么>'}。", ""]
    lines += [
        "## 之前那条线到哪了",
        "<!-- 2~3 句:目标是什么、做到哪一步。别复述过程 -->",
        "",
        "## 已经定下的(别再问、别推翻)",
        "- <!-- 拍板过的决定,一条一行 -->",
        "",
        "## 已经排除的(别重走)",
        "- <!-- 方案 — 因为理由 -->",
        "",
        "## 关键事实",
        "<!-- 以下由脚本从日志机械提取,可直接删改 -->",
        f"- 来源会话 `{meta['session_id']}`,第 {meta['start']} ~ {meta['total']} 轮(共 {meta['total']} 轮)",
    ]
    if meta.get("project_dir"):
        lines.append(f"- 工作目录 `{meta['project_dir']}`")
    if meta.get("span"):
        lines.append(f"- 时间 {meta['span']}")
    lines += render_facts(facts)
    lines += [
        "",
        "## 这次要做",
        "<!-- 新方向,或「接着上一步往下走」 -->",
        "",
        "<!-- 填完删掉所有注释。长度由内容定:每一句都该是新对话查不回来的东西 -->",
    ]
    return "\n".join(lines)


def check_package(path: str) -> int:
    """按节报篇幅占比,把注意力引到「哪节在膨胀」,而不是「总数超没超」。

    刻意不判定合格与否:一旦有达标线,就会有人为了够数去合并条目、砍掉
    本该独立成条的决策。长度是症状,病根在某一节。
    """
    p = Path(path)
    if not p.is_file():
        print(f"找不到文件:{path}", file=sys.stderr)
        return 2
    text = p.read_text(encoding="utf-8")
    print(f"{p}\n共 {len(text)} 字符\n")

    sections, name, buf = [], "(开头)", []
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((name, buf))
            name, buf = line[3:].strip(), []
        else:
            buf.append(line)
    sections.append((name, buf))

    total = max(len(text), 1)
    for name, buf in sections:
        n = len("\n".join(buf).strip())
        if not n:
            continue
        share = n * 100 // total
        width = min(28, max(1, share // 3))
        print(f"  {name:<16}{n:>6} 字符{share:>4}%  {'█' * width}")

    if "<!--" in text:
        print("\n还有没删干净的 `<!--` 注释")

    print("\n看哪一节在膨胀,不看总数:")
    print("  关键事实占大头   → 里面多半是仓库查得到的东西,换成路径")
    print("  某一节在讲过程   → 只留结论和原因,过程砍掉")
    print("  节数比常规多     → 可能跨了好几条线,按 outline 拆包")
    return 0


def main():
    ap = argparse.ArgumentParser(description="导出 WorkBuddy 会话主线,用于开新分支")
    ap.add_argument("--session", "-s", help="会话 ID,默认取当前会话")
    ap.add_argument("--list", "-l", action="store_true", help="列出本项目最近的会话")
    ap.add_argument("--outline", "-o", action="store_true", help="只列用户发言,挑分叉点用")
    ap.add_argument("--from", dest="start", type=int, default=1, help="从第几个用户发言开始(含)")
    ap.add_argument("--last", type=int, help="只要最近 N 个用户发言")
    ap.add_argument("--compact", action="store_true", help="压缩版:助手发言截断、工具调用只留次数")
    ap.add_argument("--package", action="store_true", help="同时生成上下文包骨架(<会话>-package.md)")
    ap.add_argument("--check", metavar="FILE", help="按节报篇幅占比,指出哪节在膨胀")
    ap.add_argument("--out", help="输出文件路径,默认写系统临时目录")
    ap.add_argument("--stdout", action="store_true", help="直接打印全文到标准输出")
    args = ap.parse_args()

    if args.check:
        return check_package(args.check)

    if args.list:
        rows = all_logs()
        if not rows:
            print("没有找到任何会话记录。")
            return 1
        print(f"最近的会话({len(rows)} 个中有记录的前 20 个):\n")
        for p in rows[:20]:
            recs = load_records(p)
            title = read_title(recs) or "(无标题)"
            print(f"{p.stem}  {fmt_time(p.stat().st_mtime)}  {title}")
        return 0

    session_id = args.session or os.environ.get("CODEBUDDY_SESSION_ID")
    if not session_id:
        print("拿不到会话 ID:请用 --session 指定,或在 WorkBuddy 会话里运行。", file=sys.stderr)
        return 2

    path = find_log(session_id)
    if not path:
        print(f"找不到会话 {session_id} 的日志。用 --list 看看有哪些。", file=sys.stderr)
        return 2

    records = load_records(path)
    turns = build_turns(records)
    if not turns:
        print("这个会话里没有可提取的对话。", file=sys.stderr)
        return 2

    total = len(turns)

    if args.outline:
        print(f"会话「{read_title(records) or session_id}」共 {total} 个用户发言:\n")
        for i, turn in enumerate(turns, 1):
            said = (turn["user"] or "").replace("\n", " ")
            if len(said) > 72:
                said = said[:72] + "…"
            print(f"  [{i:>3}] {said}")
        if total > 60:
            est = sum(len(t["user"] or "") for t in turns)
            est += sum(len(v) for t in turns for k, v in t["blocks"] if k == "say")
            est += 70 * sum(1 for t in turns for k, _ in t["blocks"] if k == "call")
            tail = max(1, total - 39)
            print(f"\n轮数偏多({total} 轮),全量约 {est // 1000}k 字符,一次读不完。建议:")
            print(f"  --compact            全量压缩版,保留每一轮(推荐先跑这个看全貌)")
            print(f"  --last 40            只带最近 40 轮")
            print(f"  --from {tail}            从第 {tail} 轮往后(配 --compact 找转折点)")
            print("  分段带的时候,每段各生成一份包,新对话里只贴真正需要的那份。")
        else:
            print("\n用 --from N 从第 N 个发言开始提取。")
        return 0

    start = max(1, args.start)
    if args.last:
        start = max(start, total - args.last + 1)
    body = render_turns(turns, start, compact=args.compact)
    if not body.strip():
        print(f"从第 {start} 个发言开始没有内容(总共只有 {total} 个)。", file=sys.stderr)
        return 2

    calls = sum(1 for r in records if r.get("type") == "function_call")
    times = [rec_ts(r) for r in records if rec_ts(r)]
    span = f"{fmt_time(min(times) if times else 0)} ~ {fmt_time(max(times) if times else 0)}"
    header = [
        f"# 会话记录:{read_title(records) or '(无标题)'}",
        "",
        f"- 会话 ID:`{session_id}`",
        f"- 工作目录:`{read_cwd(records) or '未知'}`",
        f"- 时间:{span}",
        f"- 全量:用户发言 {total} 次,工具调用 {calls} 次",
    ]
    if args.compact:
        header.append(f"- 模式:压缩版(助手发言截到 {COMPACT_SAY} 字、用户发言截到 {COMPACT_USER} 字)")
    if start > 1:
        header.append(f"- 本段:第 {start} ~ {total} 个用户发言(前面 {start - 1} 个未包含)")
    doc = "\n".join(header) + "\n\n---\n\n" + body

    if args.stdout:
        sys.stdout.write(doc)
        return 0

    out_path = Path(args.out) if args.out else Path(tempfile.gettempdir()) / "workbuddy-fork" / f"{session_id}-from{start}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")

    print(f"已导出:{out_path}")
    print(f"规模:{len(doc)} 字符 / {len(doc.splitlines())} 行")
    print(f"跨度:第 {start} ~ {total} 个用户发言(共 {total} 个,工具调用 {calls} 次)")
    if len(doc) > 150_000:
        print("\n注意:这段太长(>150k 字符),整段读会吃掉大量上下文。")
        print("建议加 --compact 压一版,或 --last 40 只取最近一段。")

    if args.package:
        suffix = "" if start == 1 else f"-from{start}"
        pkg_path = out_path.parent / f"{session_id}{suffix}-package.md"
        facts = collect_facts(turns[start - 1:])
        meta = {
            "session_id": session_id,
            "title": read_title(records),
            "project_dir": read_cwd(records),
            "start": start,
            "total": total,
            "span": span,
        }
        skeleton = package_skeleton(meta, facts)
        pkg_path.write_text(skeleton, encoding="utf-8")
        print(f"\n上下文包骨架:{pkg_path}({len(skeleton)} 字符)")
        print("\n--- 骨架预览(填完删注释,再跑 --check 看哪节膨胀)---\n")
        print(skeleton)
    return 0


if __name__ == "__main__":
    sys.exit(main())
