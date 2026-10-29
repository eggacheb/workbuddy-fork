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

# 只在真终端里改编码。被管道接走时上游多半按系统码页解码,
# 强设 utf-8 会让中文在 PowerShell 里整段乱码。
try:
    if sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WB_HOME = Path(os.environ.get("WORKBUDDY_HOME") or (Path.home() / ".workbuddy"))
PROJECTS_DIR = WB_HOME / "projects"
TEMP_ROOT = Path(tempfile.gettempdir()).resolve() / "workbuddy-fork"

REMINDER_RE = re.compile(r"<system-reminder[\s\S]*?</system-reminder>")
QUERY_RE = re.compile(r"<user_query>([\s\S]*?)</user_query>")
IMAGE_PATH_RE = re.compile(r"<image_local_path>([^<]+)</image_local_path>")
AT_IMAGE_RE = re.compile(r"@image#?\d*:([^\s,，]+)")

# 平台/队友/后台任务塞进用户消息的块,不是用户说的话
INJECTED_BLOCKS = [
    "task-notification",
    "teammate-message",
    "conversation_history_summary",
    "cb_summary",
    "long_text_quote",
    "previous_user_message",
    "previous_assistant_message",
    "previous_tool_call",
    "user-prompt-submit-hook",
    "additional-data",
    "message-queue",
    "image_local_path",
]
INJECTED_RES = [re.compile(rf"<{tag}\b[\s\S]*?</{tag}>") for tag in INJECTED_BLOCKS]

# 「整段只剩协议碎片」的判断只认这些标签名。
# 用户自己贴 <div> / <config> 之类的代码时不能碰。
KNOWN_TAGS = set(INJECTED_BLOCKS) | {
    "system-reminder", "user_query", "image_local_path", "omitted",
    "result", "summary", "status", "uuid", "tool-use-id", "task-id",
}

# @ 前缀的附件标记,只留文件名。
# long-text / skill 后面常跟一段带引号的预览,整段要一起剥掉,
# 否则预览里的空格会让它断在中间,残下半句进到正文里。
AT_MARK_RE = re.compile(r'@(?:long-text|file|skill|image)#?\d*:(?:"[^"]*"|"[^"]*$|\S*)\s*')

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

# 压缩版的截断长度。说「助手截 200 字」指的是单条消息,
# 一轮里可能有几十条,所以还要一个轮级上限兜住。
COMPACT_USER = 400
COMPACT_SAY = 200
COMPACT_TURN = 500


# ---------- 定位 ----------

def find_log(session_id: str):
    """按会话 ID 找 jsonl,返回 (状态, 命中列表)。

    状态: ok / ambiguous / none。
    前缀匹配只在 ID 够长时才做——`-s 0` 这种会静默命中完全不相干的会话。
    """
    exact = [p for p in PROJECTS_DIR.glob(f"*/{session_id}.jsonl") if p.is_file()]
    if exact:
        return ("ok", [max(exact, key=lambda p: p.stat().st_size)]) if len(exact) == 1 else ("ambiguous", exact)

    if len(session_id) < 8:
        return ("none", [])

    partial = [p for p in PROJECTS_DIR.rglob(f"{session_id}*.jsonl") if p.is_file()]
    if len(partial) == 1:
        return ("ok", partial)
    if len(partial) > 1:
        return ("ambiguous", partial)
    return ("none", [])


def newest_log():
    logs = all_logs()
    return logs[0] if logs else None


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


def read_title_file(path: Path, max_lines: int = 400) -> str:
    """只读文件头找标题,别为了一个字符串把整个日志解析进内存。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                if '"ai-title"' not in line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("type") != "ai-title":
                    continue
                for key in ("title", "text", "content", "aiTitle"):
                    v = r.get(key)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
    except OSError:
        pass
    return ""


def read_cwd(records) -> str:
    """取被导出会话自己的工作目录——不是当前会话的。

    日志里没有 cwd 时宁可留空:填一个当前会话的目录比留空更糟,
    新对话会照着错目录干活。
    """
    seen = Counter(
        r.get("cwd") for r in records
        if isinstance(r.get("cwd"), str) and r.get("cwd").strip()
    )
    if seen:
        return seen.most_common(1)[0][0]
    return ""


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


def protocol_only(text: str) -> bool:
    """整段只剩已知协议标签包着的碎片?只认白名单里的标签名。"""
    tags = set(re.findall(r"</?([a-zA-Z][\w-]*)", text))
    if not tags or not tags <= KNOWN_TAGS:
        return False
    return len(re.sub(r"<[^>]+>", "", text).strip()) < 30


def short_img(path: str) -> str:
    """图片路径太长会把 outline 挤满,只留最后两段。"""
    path = path.strip()
    if len(path) <= 48:
        return path
    parts = [p for p in re.split(r"[\\/]", path) if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else path


def clean_user(text: str) -> str:
    """剥掉平台注入的上下文,只留用户真的说过的话。

    顺序有讲究:先摘附件标记(它们会被当注入剥掉,但文件名是真信息),
    再剥注入块,最后给 <user_query> 就地脱壳而不是把它抽到前面——
    抽出来重排会打乱语序,「帮我看下 <q>这个配置</q>」会变成「这个配置 帮我看下」。
    """
    text = REMINDER_RE.sub("", text)

    # 图片:名字是用户眼里的文件名,路径是唯一能定位文件的东西,两个都要。
    # 只留名字的话,新对话拿一个磁盘上不存在的文件名,想看也看不了。
    # 注意顺序:路径块要先抓(它本身在注入清单里,马上会被剥掉);
    # 名字要等剥完注入再抓,否则压缩摘要里那些历史图片也会被捞出来。
    paths = [p.strip() for p in IMAGE_PATH_RE.findall(text)]

    for pattern in INJECTED_RES:
        text = pattern.sub("", text)

    names = [n for n in AT_IMAGE_RE.findall(text) if n and not n.startswith("http")]
    extras = []
    if paths:
        for i, p in enumerate(paths):
            label = names[i] if len(names) == len(paths) else ""
            gone = "" if Path(p).is_file() else " (文件已不在磁盘)"
            extras.append(f"[图片 {label}]({p}){gone}" if label else f"[图片 {p}]{gone}")
    elif names:
        extras.extend(f"[图片 {short_img(n)}]" for n in names)

    text = AT_MARK_RE.sub("", text).strip()
    for pattern in NOISE_RES:
        text = pattern.sub("", text)
    text = QUERY_RE.sub(lambda m: "\n" + m.group(1).strip() + "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    if extras:
        text = re.sub(r"^\[图片\]\s*$", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # 剥掉 @ 标记后,正文前面常留一个孤零零的逗号
    text = re.sub(r"^[，,、;；:：]+", "", text).strip()

    if text and protocol_only(text):
        text = ""

    parts = extras + ([text] if text else [])
    return "\n\n".join(p for p in parts if p)


def parse_args_of(record) -> dict:
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
        args = parse_args_of(record)
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

    助手先开口(日志被截断或接续)时丢掉那一段:给它编号会让
    outline 的序号和真人对不上,--from 从此指错地方。
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
                if cur is None:
                    continue
                said = msg_text(r).strip()
                if said:
                    cur["blocks"].append(("say", said))
        elif rtype == "function_call":
            if cur is None:
                continue
            args = parse_args_of(r)
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
    kept = " ".join(text[:limit].split())
    return kept + f" …(+{len(text) - len(kept)} 字)"


def compact_blocks(blocks):
    """压缩版:助手发言截断,工具调用只留名字和次数。"""
    says = []
    names = Counter()
    order = []
    for kind, val in blocks:
        if kind == "say":
            says.append(val)
        else:
            name = val.split("(", 1)[0]
            names[name] += 1
            if name not in order:
                order.append(name)
    out = []
    if says:
        out.append(clip("\n\n".join(says), COMPACT_TURN))
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


def is_temp_artifact(path: str) -> bool:
    """上一次跑脚本留下的临时文件,别让它冒充「改过的文件」。"""
    s = path.replace("\\", "/")
    if "/workbuddy-fork/" in s:
        return True
    return bool(re.search(r"-(?:package|from\d+)\.md$", s))


def collect_facts(turns):
    edited = Counter()
    read = Counter()
    cmds = []
    seen_cmd = set()
    errors = []
    seen_err = set()
    urls = []
    seen_url = set()

    for t in turns:
        for c in t["calls"]:
            name, a = c["name"], c["args"]
            if name in EDIT_TOOLS or name == "Read":
                p = file_arg(a)
                if not p or is_temp_artifact(p):
                    continue
                if name == "Read":
                    read[p] += 1
                else:
                    edited[p] += 1
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
            key = e[:90]
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
    if n <= 0 or not items:
        return []
    if len(items) <= n:
        return items
    if n == 1:
        return [items[len(items) // 2]]
    idx = sorted({round(i * (len(items) - 1) / (n - 1)) for i in range(n)})
    return [items[i] for i in idx]


def pick_errors(lines, limit=3):
    """去重后优先留真故障;一条强故障都没有时退回普通报错,别交白卷。"""
    deduped, seen = [], set()
    for e in lines:
        key = e[:90]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    strong = [e for e in deduped if ERR_STRONG_RE.search(e)]
    return spread(strong or deduped, limit)


def rel_path(path: str, base: str) -> str:
    """落进工作目录的路径转相对,省掉每条重抄一遍前缀。"""
    if not base:
        return path
    try:
        return str(Path(path).relative_to(base))
    except (ValueError, OSError):
        return path


def render_facts(facts, base=""):
    lines = []
    if facts["edited"]:
        lines.append("- 改过的文件(按改动次数):")
        for p, n in facts["edited"]:
            lines.append(f"  - `{rel_path(p, base)}` ×{n}")
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
            lines.append(f"  - `{rel_path(p, base)}` ×{n}")
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
    title = meta["title"] or "<一句话说明这条线要干什么>"
    span_label = "全程" if meta["start"] > 1 else ""
    lines = [f"接着做 {title}。", ""]
    lines += [
        "## 之前那条线到哪了",
        "<!-- 2~3 句:目标是什么、做到哪一步。别复述过程 -->",
        "",
        "## 已经定下的",
        "- <!-- 拍板过的决定 — 因为什么。一条一行 -->",
        "",
        "## 试过但不通的",
        "- <!-- 试过的方案 — 断在哪、为什么 -->",
        "",
        "## 关键事实",
        "<!-- 以下由脚本从日志机械提取,可直接删改 -->",
        f"- 来源会话 `{meta['session_id']}`,第 {meta['start']} ~ {meta['end']} 轮(共 {meta['total']} 轮)",
    ]
    if meta.get("project_dir"):
        lines.append(f"- 工作目录 `{meta['project_dir']}`")
    if meta.get("span"):
        lines.append(f"- 时间 {meta['span']}{span_label}")
    lines += render_facts(facts, meta.get("project_dir", ""))
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
    if p.is_dir():
        print(f"--check 要文件路径,这是目录:{path}", file=sys.stderr)
        return 2
    if not p.is_file():
        print(f"找不到文件:{path}", file=sys.stderr)
        return 2
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = p.read_text(encoding="utf-8", errors="replace")
        print("(文件不是 UTF-8,按替换字符读的,字数可能有偏差)\n")
    print(f"{p}\n共 {len(text)} 字符\n")

    # 代码块里的 ## 是 shell / python 注释,不是节标题
    sections, name, buf = [], "(开头)", []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            buf.append(line)
            continue
        if not in_fence and line.startswith("## "):
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
    return 0


def positive_int(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"要整数,收到 {value!r}")
    if n < 1:
        raise argparse.ArgumentTypeError("必须 ≥ 1")
    return n


def main():
    ap = argparse.ArgumentParser(
        description="导出 WorkBuddy 会话主线,用于开新分支",
        epilog="读全量的判据不是轮数,是「包的四个待填节能不能写出内容」。",
    )
    ap.add_argument("--session", "-s", help="会话 ID,默认取当前会话(CODEBUDDY_SESSION_ID)")
    ap.add_argument("--list", "-l", action="store_true", help="列出所有项目最近的会话")
    ap.add_argument("--outline", "-o", action="store_true", help="只列用户发言,挑分叉点用")
    ap.add_argument("--from", dest="start", type=positive_int, default=1,
                    help="从第几个用户发言开始(含这一句)")
    ap.add_argument("--last", type=positive_int, help="只要最近 N 个用户发言(与 --from 二选一)")
    ap.add_argument("--compact", action="store_true", help="压缩版:助手发言截断、工具调用只留次数")
    ap.add_argument("--package", action="store_true", help="同时生成上下文包骨架")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的上下文包")
    ap.add_argument("--check", metavar="FILE", help="按节报篇幅占比,指出哪节在膨胀")
    ap.add_argument("--out", help="输出文件路径,默认写系统临时目录")
    ap.add_argument("--stdout", action="store_true", help="直接打印全文到标准输出")
    args = ap.parse_args()

    if args.check:
        return check_package(args.check)

    if args.list:
        rows = all_logs()
        if not rows:
            print("没有找到任何会话记录。检查 WORKBUDDY_HOME 或 --session。")
            return 1
        print(f"最近的会话(共 {len(rows)} 个,显示前 20):\n")
        for p in rows[:20]:
            title = read_title_file(p) or "(无标题)"
            print(f"{p.stem}  {fmt_time(p.stat().st_mtime)}  {title}")
        return 0

    session_id = args.session or os.environ.get("CODEBUDDY_SESSION_ID")
    if not session_id:
        print("拿不到会话 ID:用 --session 指定,或在 WorkBuddy 会话里运行。", file=sys.stderr)
        print("如果这是后台/子代理会话,日志可能还没落盘——用 --list 挑一个。", file=sys.stderr)
        return 2

    status, hits = find_log(session_id)
    if status == "ambiguous":
        print(f"「{session_id}」匹配到 {len(hits)} 个会话,不猜。用完整 ID:", file=sys.stderr)
        for p in hits[:10]:
            print(f"  {p.stem}  {read_title_file(p) or '(无标题)'}", file=sys.stderr)
        return 2
    if status == "none":
        print(f"找不到会话 {session_id} 的日志。", file=sys.stderr)
        print("后台/子代理会话可能不落盘;用 --list 看看有哪些。", file=sys.stderr)
        return 2

    path = hits[0]
    records = load_records(path)
    turns = build_turns(records)
    if not turns:
        print("这个会话里没有可提取的对话。", file=sys.stderr)
        print("如果日志不是 WorkBuddy 格式(比如装进了 Claude Code),提取会全部落空。", file=sys.stderr)
        return 2

    total = len(turns)

    if args.outline:
        print(f"会话「{read_title(records) or session_id}」共 {total} 个用户发言:\n")
        for i, turn in enumerate(turns, 1):
            said = (turn["user"] or "").replace("\n", " ")
            if len(said) > 72:
                said = said[:72] + "…"
            print(f"  [{i:>3}] {said}")
        print("\n--from N 从第 N 句**含**这一句开始。")
        return 0

    start = args.start
    if args.last:
        if args.start > 1:
            print(f"(--from {args.start} 被 --last {args.last} 覆盖)", file=sys.stderr)
        start = max(1, total - args.last + 1)
    body = render_turns(turns, start, compact=args.compact)
    if not body.strip():
        print(f"从第 {start} 个发言开始没有内容(总共只有 {total} 个)。", file=sys.stderr)
        return 2

    end = total
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
        header.append(f"- 模式:压缩版(单条助手 {COMPACT_SAY} 字、单轮 {COMPACT_TURN} 字、用户 {COMPACT_USER} 字)")
    if start > 1:
        header.append(f"- 本段:第 {start} ~ {end} 个用户发言(前面 {start - 1} 个未包含)")
    doc = "\n".join(header) + "\n\n---\n\n" + body

    if args.stdout and args.package:
        ap.error("--stdout 与 --package 互斥:包总是先落盘再打印")

    if args.stdout:
        sys.stdout.write(doc)
        return 0

    out_path = Path(args.out) if args.out else TEMP_ROOT / f"{session_id}-from{start}.md"
    if out_path.is_dir():
        print(f"--out 需要文件路径,这是目录:{out_path}", file=sys.stderr)
        return 2
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")

    print(f"已导出:{out_path}")
    print(f"规模:{len(doc)} 字符 / {len(doc.splitlines())} 行")
    print(f"跨度:第 {start} ~ {end} 个用户发言(共 {total} 个,工具调用 {calls} 次)")
    if len(doc) > 150_000:
        print("\n注意:这段太长(>150k 字符),整段读会吃掉大量上下文。")
        if args.compact:
            print(f"已经是最紧的模式了。改用 --from {start} --last 40 分两段,各出一份包。")
        else:
            print("加 --compact 压一版,或 --last 40 只取最近一段。")

    if args.package:
        suffix = "" if start == 1 else f"-from{start}"
        pkg_path = out_path.parent / f"{session_id}{suffix}-package.md"
        if pkg_path.exists() and not args.force:
            print(f"\n已有一份包,没覆盖:{pkg_path}")
            print("要重写就加 --force,或换 --out 放到别处——先看看原来那份是不是已经填好了。")
            return 0
        facts = collect_facts(turns[start - 1:])
        meta = {
            "session_id": session_id,
            "title": read_title(records),
            "project_dir": read_cwd(records),
            "start": start,
            "end": end,
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
