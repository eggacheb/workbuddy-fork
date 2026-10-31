#!/usr/bin/env python3
"""从 WorkBuddy 会话日志里抽出可读的对话主线。

只做机械提取,不做摘要。摘要/压缩交给调用它的模型——
这样即使上下文已经被 compact 过,原文仍然完整。

会话日志位置: ~/.workbuddy/projects/<项目slug>/<sessionId>.jsonl
"""

import argparse
import io
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# 无条件 utf-8 + replace。
# 按 isatty 分流看起来更聪明,代价是中文 Windows 上只要重定向或走管道
# 就抛 UnicodeEncodeError —— 而模型跑命令时 stdout 永远是被接走的。
# 乱码能忍,崩掉不能。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

WB_HOME = Path(os.environ.get("WORKBUDDY_HOME") or (Path.home() / ".workbuddy"))
PROJECTS_DIR = WB_HOME / "projects"
TEMP_ROOT = Path(tempfile.gettempdir()).resolve() / "workbuddy-fork"

# dsh(DeepSeek Harness)的会话:~/./.dsh/sessions/<项目slug>/<会话id>/session.v4.jsonl.zstd
# 导出包是同一个 session.v4.jsonl,外加 media/(文件名带冒号,Windows 上解不出来,
# 所以只读 jsonl,不碰 media)。
DSH_HOME = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
DSH_SESSIONS = DSH_HOME / "sessions"
DSH_LOG_NAME = "session.v4.jsonl"
DSH_LOG_ZSTD = DSH_LOG_NAME + ".zstd"

# dsh 把「这是不是人话」写在 source.kind 里:只有 user 是用户真说的,其余全是
# 平台拼的。判据是来源,不是措辞——那些注入的正文里连标签都未必有(ADR-0007)。
DSH_USER_KIND = "user"
# 认识的非用户 kind。不认识的也丢,但要在 stderr 说出来:默认丢 + 不出声,
# 正是这个脚本历史上每个 P0 的形状。
DSH_KNOWN_KINDS = {
    "ptc-mode",
    "agent-instructions",
    "runtime-context",
    "skill-catalog",
    "skill-invocation",
    "compact-checkpoint",
    "repeat-tool-reminder",
    "plugin:ptc-plus",
    "system-prompt",
    # 子代理的往来。全库 137 条,内容是子代理自己写完的报告和「我干完了」——
    # 是执行细节,不是用户说的话。主代理的发言里已经有结论,这里再抄一遍只是更长的重复。
    "agent-message",
    "subagent-settled",
    # 环境通知:模型换了、时间采样、审批策略变了。全是平台在自言自语。
    "model-selection",
    "time-context",
    "user-approval",
}

REMINDER_RE = re.compile(r"<system-reminder[\s\S]*?</system-reminder>")

# 「这串东西在行内代码里」只有这一条判据,三处共用。用户讨论这个工具时,
# 必然要把 `<user_query>`、`@image#1:a.png` 写成字面文本;认了它们,切分就会
# 从错误的位置开始——注入被当原话、原话被当注入(ADR-0004)。
NOT_IN_CODE_LEFT = r"(?<![`])"
NOT_IN_CODE_RIGHT = r"(?![`])"


def in_inline_code(text: str, pos: int) -> bool:
    """pos 紧跟在反引号后面,说明这一处在行内代码里。"""
    return pos > 0 and text[pos - 1] == "`"


# 带 data-role 的 system-reminder 整块是平台拼的信封。必须在切体内外之前剥掉:
# AGENTS.md 会被整篇注进 user-context,而它里面就写着 `<user_query>` 这条纪律,
# 留着信封,非贪婪的 QUERY_RE 就会从那个字面标签开始匹配到真闭合标签——
# 前半段注入变成「体内」、真原话变成「体外」。
ENVELOPE_RE = re.compile(r'<system-reminder\s+data-role="[^"]*"[\s\S]*?</system-reminder>')
# 标签被反引号包着时不算结构。两头都要判——只判开标签,非贪婪就会从真开标签
# 一路吃到反引号里的假闭合。
QUERY_RE = re.compile(
    rf"{NOT_IN_CODE_LEFT}<user_query>([\s\S]*?)</user_query>{NOT_IN_CODE_RIGHT}")
IMAGE_PATH_RE = re.compile(r"<image_local_path>([^<]+)</image_local_path>")

# 标记名的字符集只收 ASCII。看起来苛刻,但这是唯一不会吃正文的写法:
# 中文正文常紧贴标记("@image#1:a.png，这样改对不对"),一旦按「到空白为止」
# 去抓,整句话都会当成文件名删掉。宁可丢一个中文文件名,也不能吃用户的话。
NAME_CHARS = r"[A-Za-z0-9_./\\:\-~%+=@]"
# 反引号里的是用户在正文里举例讨论这个标记,不是附件。判据看位置、
# 不看「有没有 <image_local_path>」:平台偶尔漏记路径,但图确实贴了,
# 按路径判会把真附件当成讨论原样丢给新对话。
AT_IMAGE_RE = re.compile(rf"{NOT_IN_CODE_LEFT}@image#?\d*:({NAME_CHARS}+)")

# 两种标记共用一套语法,所以只认一遍:改一处就不会漏另一处。
# 分组:1 = 标记种类,2 = 引号里的内容,3 = 裸路径,4 = 后面跟的空白。
AT_MARK_RE = re.compile(rf'@(long-text|image)#?\d*:(?:"([^"]*)"|({NAME_CHARS}*))(\s*)')
LONG_TEXT_PREVIEW_MAX = 60

# 平台真实发出的文件引用是 `@"…"`,不是 `@file:"…"`。完整路径只在
# <user_references> 里出现一次,而那一块是平台注入,会被整块删掉;体内留的
# `@"…"` 和 C# 的 verbatim string 长得一模一样,所以只能靠「这块清单里
# 有没有它」来认——那是平台盖的章,也是唯一可靠的判据。
REF_BLOCK_RE = re.compile(r"<user_references>([\s\S]*?)</user_references>")
QUOTED_PATH_RE = re.compile(r'"([^"]+)"')
REF_TOKEN_RE = re.compile(r'@"([^"]*)"')

# @skill: / @file: 是「用户当时点了哪个技能、哪个文件」的唯一记录。
# 删掉它等于抹掉「这句话是 /code-review 触发的」——而这件事在新对话里
# 没有任何文件能查回来,只能靠上下文包自己说。
SKILL_MARK_RE = re.compile(rf'@skill#?\d*:(?:"([^"]*)"|({NAME_CHARS}+))')
FILE_MARK_RE = re.compile(rf'@file#?\d*:(?:"([^"]*)"|({NAME_CHARS}+))')

# 摘要类注入:里面是历史重放,先整块删掉,再谈别的
SUMMARY_BLOCKS = ["cb_summary", "conversation_history_summary"]

# 平台/队友/后台任务塞进用户消息的块,不是用户说的话
# 注意 long_text_quote 不在这里:它的内容是用户 @long-text 粘进来的正文,要留
INJECTED_BLOCKS = [
    "task-notification",
    "teammate-message",
    "previous_user_message",
    "previous_assistant_message",
    "previous_tool_call",
    "user-prompt-submit-hook",
    "additional-data",
    "message-queue",
    "image_local_path",
]

# 「整段只剩协议碎片」的判断只认这些标签名。
# 用户自己贴 <div> / <config> 之类的代码时不能碰。
KNOWN_TAGS = set(SUMMARY_BLOCKS) | set(INJECTED_BLOCKS) | {
    "system-reminder", "user_query",
    "omitted", "result", "summary", "status", "uuid", "tool-use-id", "task-id",
}

# 标签外面还挂着的 harness 指令,整段都不是用户说的
NOISE_RES = [
    re.compile(r"^Use the \w+ tool with [\s\S]*$"),
    re.compile(r"^Please continue with the conversation based on the summarized context[\s\S]*$"),
    re.compile(r"^This session is being continued from[\s\S]*$"),
]

# 工具调用里最该留下的一行参数
ARG_PRIORITY = ["file_path", "path", "command", "pattern", "url", "query", "prompt", "description"]

# dsh 的工具名是小写的 bash/read/write/edit,WorkBuddy 是 Bash/Read/Write。
# 两套都收进来:这里判的是「这个工具动了文件还是跑了命令」,与来源无关。
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "edit", "write"}
SHELL_TOOLS = {"Bash", "PowerShell", "run_command", "bash", "shell"}
READ_TOOLS = {"Read", "read"}

# 报错行:只认像日志的那几行,长行(多半是文件内容)跳过
ERR_LINE_RE = re.compile(
    r"(?i)\b(error|exception|traceback|fatal|failed|denied|invalid|missing|not found|cannot|unable to)\b"
    r"|\w*Error\b|Errno|No module named|command not found|Permission denied"
    r"|错误:|失败:|报错:|不存在|不会被识别"
)
ERR_SKIP_RE = re.compile(r"(?i)no (?:errors?|failures?)|0 (?:errors?|failed)|\bwithout error|错误\\?\": ?0")
# 文档/表格里的句子也会命中关键词,按行首形态排除
ERR_SKIP_PREFIX = ("- ", "* ", "|", "#", ">", "+ ")
# 探路时的鸡毛蒜皮,别占位置
ERR_TRIVIAL_RE = re.compile(
    r"(?i)(^|\s)(ls|cat|head|tail|stat|cd): |updater:|\[(warn|info|debug)\]"
    r"|not a git repository|^\s*(except|try|if|for|while|def|class|const|let|var|function|import|export|\}\s*$)"
    r"|^Traceback \(most recent call last\):\s*$"
)
# 真正的故障长这样
ERR_STRONG_RE = re.compile(
    r"(?i)(error:|exception|traceback|fatal|denied|eaddrinuse|missing_|cannot read private"
    r"|invalid output|is not a function|is not callable|module not found|timed out"
    r"|\w*Error\b|Errno)")
# 光是这一行没有任何信息量,单独出现就别留(已并入 ERR_TRIVIAL_RE)

# 压缩版的截断长度:单条 + 单轮,两道都要有
COMPACT_USER = 400
COMPACT_SAY = 200
COMPACT_TURN = 500


# ---------- 定位 ----------

def find_log(session_id: str):
    """按会话 ID 找 jsonl,返回 (状态, 命中列表, 备注)。

    状态: ok / ambiguous / none。
    前缀匹配只在 ID 够长时才做——`-s 0` 这种会静默命中完全不相干的会话。
    """
    exact = [p for p in PROJECTS_DIR.glob(f"*/{session_id}.jsonl") if p.is_file()]
    if exact:
        if len(exact) == 1:
            return ("ok", exact, "")
        # 完整 ID 还撞多个(改过项目目录名),用户已经无路可走,取最大的
        if len(session_id) >= 32:
            best = max(exact, key=lambda p: p.stat().st_size)
            return ("ok", [best], f"同 ID 出现在 {len(exact)} 个项目目录里,取了最大的那份")
        return ("ambiguous", exact, "")

    if len(session_id) < 8:
        return ("none", [], "")

    partial = [p for p in PROJECTS_DIR.rglob(f"{session_id}*.jsonl") if p.is_file()]
    if len(partial) == 1:
        return ("ok", partial, "")
    if len(partial) > 1:
        return ("ambiguous", partial, "")
    return ("none", [], "")


def all_logs():
    return sorted(
        (p for p in PROJECTS_DIR.glob("*/*.jsonl") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def newest_log():
    logs = all_logs()
    return logs[0] if logs else None


class ZstdUnavailable(RuntimeError):
    """日志是 zstd,而这台机器上没有解压手段。宁可报错,也不出空包。"""


def _zstd_stream(raw, label: str):
    """把 zstd 二进制流解成明文流:先试 stdlib(3.14+),再试 zstandard 模块。

    三级降级的判据是「能不能 import」,不是「解出来像不像 JSON」——
    后者会把「依赖缺失」变成「这个会话没有内容」。
    """
    hint = (f"读不了 zstd 压缩的日志:{label}\n"
            "  需要 Python 3.14+ 的 compression.zstd,或者 `pip install zstandard`。\n"
            "  也可以把会话导出成 zip,用 --path 指过去。")
    by_path = isinstance(raw, (str, os.PathLike))
    try:
        from compression import zstd  # Python 3.14+
    except (ImportError, AttributeError):
        try:
            import zstandard
        except ImportError:
            raise ZstdUnavailable(hint)
        stream = open(raw, "rb") if by_path else raw
        return zstandard.ZstdDecompressor().stream_reader(stream, closefd=True)

    # stdlib 的 ZstdFile 按路径开;已经拿到流(在 zip 里)时走模块级 decompress。
    if by_path:
        return zstd.open(str(raw), "rb")
    try:
        return io.BytesIO(zstd.decompress(raw.read()))
    except AttributeError:
        raise ZstdUnavailable(hint)


def _open_text_stream(path: Path):
    """打开「一行一条 JSON」的日志,透明处理 zip / zstd / 明文。"""
    name = path.name.lower()
    if name.endswith(".zip"):
        import zipfile
        z = zipfile.ZipFile(path)
        members = [n for n in z.namelist()
                   if n.rsplit("/", 1)[-1] in (DSH_LOG_NAME, DSH_LOG_ZSTD)]
        if not members:
            members = [n for n in z.namelist() if n.endswith(".jsonl")]
        if not members:
            z.close()
            raise ValueError(f"压缩包里没有 {DSH_LOG_NAME}")
        member = members[0]
        raw = z.open(member)
        # 包里的成员也可能自己还是 zstd(有人把活日志直接塞进 zip):
        # 按明文读的话每一行都 JSON 解析失败,最后报「没有用户发言」——
        # 正是「不许出空包」要拦的形态。
        if member.lower().endswith(".zstd"):
            raw = _zstd_stream(raw, f"{path}!{member}")
        return io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
    if name.endswith(".zstd"):
        return io.TextIOWrapper(_zstd_stream(path, str(path)), encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def iter_lines(path: Path):
    with _open_text_stream(path) as f:
        for line in f:
            yield line


def load_records(path: Path):
    out = []
    for line in iter_lines(path):
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


def peek_first_record(path: Path):
    """只看第一条能解析的记录,用来判来源,不把整本日志读进内存。"""
    for line in iter_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def sniff_source(path: Path) -> str:
    """按日志内容判来源,判据是首行类型。认不出来返回空串,不猜。"""
    try:
        first = peek_first_record(path)
    except (OSError, ValueError, ZstdUnavailable) as e:
        print(f"读不了这份日志:{path}\n  {e}", file=sys.stderr)
        return ""
    if not first:
        return ""
    if first.get("type") == "session" and first.get("version") == 4:
        return "dsh"
    return "workbuddy"


def rec_ts(r):
    v = r.get("timestamp")
    return v if isinstance(v, (int, float)) else 0


TITLE_KEYS = ("title", "text", "content", "aiTitle")


def title_of(record) -> str:
    for key in TITLE_KEYS:
        v = record.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def read_title(records) -> str:
    for r in records:
        if r.get("type") == "ai-title":
            got = title_of(r)
            if got:
                return got
    return ""


def read_title_file(path: Path, max_lines: int = 2000) -> str:
    """只读前若干行找标题,别为了一个字符串把整个日志解析进内存。"""
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
                if r.get("type") == "ai-title":
                    got = title_of(r)
                    if got:
                        return got
    except OSError:
        pass
    return ""


def read_session_id(records) -> str:
    """日志自己记的会话 ID,取不到返回空串。

    文件名叫什么不算数:接续会话、子代理会话的日志文件名和日志里的 sessionId
    并不是一回事。包里的「来源会话」是给新对话回头查用的,填错等于给个假线索。
    """
    for r in records:
        v = r.get("sessionId")
        if isinstance(v, str) and v.strip():
            return v.strip()
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
    return seen.most_common(1)[0][0] if seen else ""


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


def marker_name(raw: str, depth: int = 1) -> str:
    """把标记里的路径缩成一个能看懂的名字。

    depth=1 只留文件名——技能名本身就是标识,多带目录没用。
    depth=2 留最后两段——文件只留 `y.py` 的话,新对话不知道是哪个 `y.py`。
    """
    raw = raw.strip()
    parts = [p for p in re.split(r"[\\/]", raw) if p]
    if not parts:
        return raw
    # @skill:"…/writing-for-agents/SKILL.md" 里,技能名是目录名,不是 SKILL.md
    if parts[-1].lower() in ("skill.md", "skill.markdown") and len(parts) >= 2:
        return parts[-2]
    return "/".join(parts[-depth:])


def render_markers(text: str) -> str:
    """把 @skill: / @file: 换成看得见的占位,而不是删掉。

    这两样记的是「用户点了什么」。技能名一旦抹掉,新对话就看不出这句话
    是普通请求还是 `/code-review`、`/to-spec` 触发的——而这决定了它该不该
    按那套流程办事。仓库里查不到这条,只能靠包自己保留。
    """
    text = SKILL_MARK_RE.sub(
        lambda m: f"[技能 {marker_name(m.group(1) or m.group(2))}]", text)
    text = FILE_MARK_RE.sub(
        lambda m: f"[文件 {marker_name(m.group(1) or m.group(2), depth=2)}]", text)
    return text


def render_marks(text: str) -> str:
    """处理 @long-text / @image:该删的删,该留的换成看得见的占位。

    - `@image:…` 是附件,图片另有一份 `[图片 x]` 记录,标记本身删掉。
    - `@long-text` 逐个标记判断:从这个标记到下一个标记之间,如果跟着
      `<long_text_quote>`,这串预览就是正文前二十字的重复,删掉;没跟就是
      这次引用留下的唯一痕迹,换成 `[长引用 「…」]`——用户在正文里讨论这个
      标记本身时,原来的删法会留下一对空反引号,原话消失还看不出来。

    判据是「这个标记后面跟没跟引文块」,不是「这一段里有没有」:后者会让
    一个真的引用把同一句里讨论用的标记一起带走。位置比语义可靠。
    """
    out, pos = [], 0
    matches = list(AT_MARK_RE.finditer(text))
    for i, m in enumerate(matches):
        out.append(text[pos:m.start()])
        pos = m.end()
        if m.group(1) == "image":
            # 反引号里的是用户在正文里举例讨论它,原样留着;否则是真附件,
            # 标记删掉,由 [图片 …] 占位补回来。删掉讨论用的那个,会留下
            # 空反引号,还会在消息开头凭空多出一个 [图片 …]。
            if in_inline_code(text, m.start()):
                out.append(m.group(0))
            continue

        nxt = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        if "<long_text_quote" in text[m.end():nxt]:
            continue  # 预览是正文的重复

        preview = (m.group(2) if m.group(2) is not None else (m.group(3) or "")).strip()
        if len(preview) > LONG_TEXT_PREVIEW_MAX:
            preview = preview[:LONG_TEXT_PREVIEW_MAX] + "…"
        out.append(f"[长引用 「{preview}」]" if preview else "[长引用]")
        out.append(m.group(4))  # 保住标记后面原来的空白
    out.append(text[pos:])
    return "".join(out)


def norm_ref(path: str) -> str:
    """比对用的规范形式:日志里的路径是双反斜杠,统一成单斜杠再比。"""
    return path.replace("\\\\", "\\").replace("\\", "/").strip("/")


def referenced_paths(text: str):
    """平台盖章的文件引用清单。

    `<user_references>` 里明确列了「这几条是用户引用的文件」。这是区分
    `@"a.py"` 和 C# 里 `@"verbatim string"` 的唯一信号,所以只认它。
    """
    return [p for block in REF_BLOCK_RE.findall(text)
            for p in QUOTED_PATH_RE.findall(block)]


def render_file_refs(text: str, refs) -> str:
    """把体内 `@"…"` 换成 `[文件 …]`,只认平台列过的那些路径。

    对不上的一律原样留着——宁可留一句看着像噪音的 `@"…"`,
    也不能把代码里的 verbatim string 改写掉。
    """
    if not refs:
        return text
    known = [norm_ref(r) for r in refs]

    def repl(m) -> str:
        raw = m.group(1)
        target = norm_ref(raw)
        if target and any(target == k or k.endswith("/" + target) for k in known):
            return f"[文件 {marker_name(raw, depth=2)}]"
        return m.group(0)

    return REF_TOKEN_RE.sub(repl, text)


def drop_tag_blocks(text: str, tags) -> str:
    """整块删掉一批已知标签连同内容。"""
    for tag in tags:
        text = re.sub(rf"<{tag}\b[\s\S]*?</{tag}>", "", text)
        text = re.sub(rf"<{tag}\b[^>]*/>", "", text)
    return text


def protocol_only(text: str) -> bool:
    """整段被一个已知协议标签包住,才算碎片。

    判据是「整段包裹」而不是「去标签后没剩几个字」——后者会误杀
    「帮我解释这段 <system-reminder>示例</system-reminder> 是什么」这种
    用户正在讨论标签的原话。同一个标签出现两次,说明中间夹着正文,也不是碎片。
    """
    if not text.strip():
        return False
    # query 标签配不齐说明被截断了,里面是用户原话,不能当碎片清掉
    if text.count("<user_query>") != text.count("</user_query>"):
        return False
    m = re.fullmatch(r"\s*<([a-zA-Z][\w-]*)\b[^>]*>[\s\S]*</\1>\s*", text)
    if not (m and m.group(1) in KNOWN_TAGS):
        return False
    return text.count(f"<{m.group(1)}") == 1


def clean_outer(outer: str):
    """清理 <user_query> 之外的平台注入。返回 (剩余文字, 图片路径, 图片名)。

    这里是「确定不是用户说的话」的部分,可以放开剥:system-reminder、
    摘要、后台任务、队友消息、@ 标记、harness 指令,全是平台拼的。
    """
    paths = [p.strip() for p in IMAGE_PATH_RE.findall(outer)]
    names = AT_IMAGE_RE.findall(outer)

    outer = REMINDER_RE.sub("", outer)
    outer = drop_tag_blocks(outer, SUMMARY_BLOCKS)
    outer = drop_tag_blocks(outer, INJECTED_BLOCKS)
    outer = render_markers(outer)
    outer = AT_MARK_RE.sub("", outer)
    # 先收紧空白再匹配:NOISE_RES 的 ^ 锚点,前面多一个空行就没命中,
    # 那些 harness 指令会当成用户发言混进 outline。
    outer = re.sub(r"\n{3,}", "\n\n", outer).strip()
    for pattern in NOISE_RES:
        outer = pattern.sub("", outer)

    outer = re.sub(r"\n{3,}", "\n\n", outer).strip()
    # 标记剥掉后开头会留一个孤零零的逗号
    outer = outer.lstrip("，,、;；:：").strip()
    if protocol_only(outer):
        outer = ""
    return outer, paths, names


def clean_body(body: str, refs=()):
    """清理 <user_query> 体内,返回 (正文, 图片路径, 图片名)。

    体内是用户原话——包括他 @long-text 粘进来的代码、他贴出来讨论的标签本身
    ——所以只摘附件、不碰排版。缩进和空行原样留着,代码块和栈才不会压平。

    refs 是平台在 <user_references> 里列过的文件路径(在体外,拆开之前就取好了)。
    体内那句 `@"…"` 只有对上这里面的某一条才当成文件引用,否则原样留着。

    允许"删"的只有 image_local_path 和带 data-role 的 system-reminder,
    它们是平台拼进来的。@skill / @file / @long-text 不删,换成看得见的占位。

    整段只有一个例外:体内除了一个已知协议标签什么都没有——那不是用户说的话。
    判据和体外同一个 protocol_only,所以「帮我解释 <system-reminder>示例
    </system-reminder> 是什么」这类讨论标签的原话不会被误伤:它整段不是被标签包住的。
    """
    paths = [p.strip() for p in IMAGE_PATH_RE.findall(body)]
    names = AT_IMAGE_RE.findall(body)

    body = render_markers(body)
    body = render_marks(body)
    body = render_file_refs(body, refs)
    body = IMAGE_PATH_RE.sub("", body)
    body = ENVELOPE_RE.sub("", body)
    # long_text_quote 是用户 @ 引用粘进来的正文,脱壳但正文留着
    body = re.sub(r"</?long_text_quote[^>]*>", "\n", body)

    # 这里刻意不压空行。压的是空行不是缩进,所以压不平代码块——但用户贴进来的
    # 终端输出、markdown 原文里,空行本身是内容。`\n{3,}` → `\n\n` 属于「顺手」
    # 那类改动:不报错、不崩,只是内容悄悄变了。代价实测过(见 ADR-0003):
    # 全库 2156 条发言里只有 27 条的正文含 3 连以上空行,换来的是一句能说清的
    # 规则——体内除了平台拼进来的东西和首尾空白,字节一律不动。
    body = body.strip()
    # 标记剥掉后开头会留一个孤零零的逗号
    body = body.lstrip("，,、;；:：").strip()
    # 剥完只剩一个协议标签,说明这一轮用户其实没说话。留着它会凭空多出一条
    # 发言,把后面所有编号顶掉,--from N 从此指错地方。
    if protocol_only(body):
        body = ""
    return body, paths, names


def split_query(text: str):
    """切出「体外原文」和「体内各段」。没有 query 时体内是空列表。

    摘要块先删、信封先剥,两步都必须在切分之前:<cb_summary> 里嵌着上一轮的
    <user_query>,信封里可能躺着字面标签(AGENTS.md 整篇注在 user-context 里)。
    顺序错了会从错误的位置切开——注入被当原话、原话被当注入。
    """
    text = drop_tag_blocks(text, SUMMARY_BLOCKS)
    text = ENVELOPE_RE.sub("", text)
    parts = QUERY_RE.split(text)  # 奇数位是 query 体内
    return "\n".join(parts[0::2]), parts[1::2]


def outer_residue(text: str) -> str:
    """有 <user_query> 的发言,体外剥干净之后还剩什么(附件占位不算)。

    只给回归脚本用。没 query 的消息返回空串——那种消息的体外就是用户原话,
    不是「残留」。剩下的每个字都是平台塞进来、而按标签剥的规则没认出来的东西:
    它是「我们又漏了一种注入」的指标,也可能是「真话被挪到 query 外面了」的警报。
    """
    outer, bodies = split_query(text)
    if not bodies:
        return ""
    remaining, _, _ = clean_outer(outer)
    return re.sub(r"\[图片[^\]]*\]", "", remaining).strip()


def clean_user(text: str) -> str:
    """剥掉平台注入的上下文,只留用户真的说过的话。

    先把 <user_query> 体内和体外彻底分开,再各走各的规则:
    体内是用户原话,只摘附件不动排版;体外是平台每轮塞的注入,整块丢掉。
    混在一起剥,一定会误伤其中一边。

    摘要块必须先删再拆体内外——<cb_summary> 里嵌着历史消息的 <user_query>,
    那是上一轮的重复,不是这一轮说的话。
    """
    # 文件引用清单在 <user_references> 里,而那一块在体外、随后会被整块删掉。
    # 所以必须在这里先取好,再拆体内外。摘要块里嵌着上一轮的消息,那里的清单
    # 不算这一轮的——先删摘要,再取清单。
    refs = referenced_paths(drop_tag_blocks(text, SUMMARY_BLOCKS))

    outer, bodies = split_query(text)
    # 只要这一轮有 <user_query>,平台就把原话包好了:体外剩下的全是它自己拼的
    # 注入——包括那些不在标签里、按标签剥碰不到的一整段(`de650383` 里有 420 字
    # 的「**必须剥的注入**:…」躺在闭合标签后面)。全库实测:2135 条带 query 的
    # 发言里,体外残留非空的只有 49 条,47 条是 clean_outer 自己渲染出的重复
    # [图片](附件另有 [图片 名字](路径) 记录),2 条是那段裸注入,没有一条是
    # 用户说的话。反过来,**没有** query 的老消息体外就是原话,一个字都不能碰。
    remaining, paths, names = clean_outer(outer)

    texts = []
    for body in bodies:
        cleaned, body_paths, body_names = clean_body(body, refs)
        paths.extend(body_paths)
        names.extend(body_names)
        if cleaned:
            texts.append(cleaned)
    if remaining and not bodies:
        texts.append(remaining)

    extras = []
    if paths:
        for i, p in enumerate(paths):
            label = names[i] if len(names) == len(paths) else ""
            gone = "" if Path(p).is_file() else " (文件已不在磁盘)"
            extras.append(f"[图片 {label}]({p}){gone}" if label else f"[图片 {p}]{gone}")
    elif names:
        extras.extend(f"[图片 {short_img(n)}]" for n in names)

    return "\n\n".join(p for p in extras + texts if p)


def short_img(path: str) -> str:
    """只有名字没有路径时,太长的名字掐短一点。"""
    path = path.strip()
    if len(path) <= 48:
        return path
    parts = [p for p in re.split(r"[\\/]", path) if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else path


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
            return f"{name}({short_path(val, 140)})"
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
                cur = {"user": said, "blocks": [], "calls": [], "errors": [], "ts": rec_ts(r)}
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


# ---------- dsh 来源 ----------

def dsh_data(record) -> dict:
    d = record.get("data")
    return d if isinstance(d, dict) else {}


def dsh_message(record) -> dict:
    """承载 content 的那一层:user/message 直接挂在 data 上,其余在 data.message 里。"""
    d = dsh_data(record)
    m = d.get("message")
    return m if isinstance(m, dict) else d


def dsh_parts(message) -> list:
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def dsh_text(message) -> str:
    """只取 type=text 的块。

    reasoning 块的正文也在 `text` 字段里,不过滤就等着把模型的内部独白当成
    它说过的话——实测一份会话里 reasoning 有 17.8 万字,是正文的 7 倍。
    """
    return "\n".join(
        p.get("text") or "" for p in dsh_parts(message)
        if isinstance(p, dict) and p.get("type") == "text")


def dsh_kind(record) -> str:
    src = dsh_message(record).get("source")
    return (src.get("kind") or "") if isinstance(src, dict) else ""


def dsh_ts(record):
    v = record.get("time")
    return v if isinstance(v, (int, float)) else 0


def dsh_containers(records) -> set:
    """哪些 tool/call 只是 PTC 容器(run_code),不是真工具。

    PTC 模式里真工具挂在容器下面的 ptc-dispatch 里;直接模式里 tool/call 自己
    就是真工具(read/bash)。判据是「有没有东西挂在它下面」,不是名字——按名字
    认 run_code,平台改个名就又开始静默漏工具,而导出照常、退出码 0。
    """
    roots = {dsh_data(r).get("rootCallId") for r in records
             if r.get("type") == "tool/ptc-dispatch"}
    roots.discard(None)
    return roots


def build_turns_dsh(records):
    """dsh:只认 source.kind == "user" 的发言,其余按来源丢弃并计数。

    返回 (turns, stats)。turn dict 与 build_turns 同一套 schema,所以渲染、
    --from / --last、包骨架一行都不用改。

    turn 不作为编号单位:一个 turn 里可以有多条发言(用户在助手干活时插话),
    按 turn 编号会把第二条吞进第一条,--from 从此指错地方。
    """
    turns, cur = [], None
    stats = {"ignored": 0, "compactions": 0, "calls": 0, "unknown": Counter()}

    containers = dsh_containers(records)

    for r in records:
        rtype = r.get("type")

        if rtype == "user/message":
            kind = dsh_kind(r)
            if kind == DSH_USER_KIND:
                said = dsh_text(dsh_message(r)).strip()
                if not said:
                    continue
                cur = {"user": said, "blocks": [], "calls": [], "errors": [], "ts": dsh_ts(r)}
                turns.append(cur)
                continue
            stats["ignored"] += 1
            if kind == "compact-checkpoint":
                stats["compactions"] += 1
            elif kind and kind not in DSH_KNOWN_KINDS:
                stats["unknown"][kind] += 1

        elif rtype == "assistant/message":
            if cur is None:
                continue
            for part in dsh_parts(dsh_message(r)):
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                said = (part.get("text") or "").strip()
                if said:
                    cur["blocks"].append(("say", said))

        elif rtype in ("tool/call", "tool/ptc-dispatch"):
            if cur is None:
                continue
            d = dsh_data(r)
            if rtype == "tool/call" and d.get("callId") in containers:
                continue  # PTC 容器,真工具在它下面的 ptc-dispatch 里
            args = d.get("arguments")
            if isinstance(args, str):
                # 直接模式里 arguments 是 JSON 字符串,PTC 里已经是 dict
                args = parse_args_of(d)
            if not isinstance(args, dict):
                args = {}
            call = {"name": d.get("name") or "?", "args": args}
            cur["blocks"].append(("call", brief_call(call, args)))
            cur["calls"].append(call)
            stats["calls"] += 1
            if d.get("isError"):
                body = "\n".join(p.get("text") or "" for p in d.get("content") or []
                                 if isinstance(p, dict))
                cur["errors"].extend(scan_errors(body))

        elif rtype == "tool/result":
            if cur is None:
                continue
            cur["errors"].extend(scan_errors(dsh_text(dsh_message(r))))

    return turns, stats


def read_dsh_header(records) -> dict:
    for r in records:
        if r.get("type") == "session" and isinstance(r.get("version"), int):
            return r
    return {}


def read_dsh_title(records) -> str:
    """标题先写一个 fallback 再被 provider 覆盖,所以取最后一条,不是第一条。

    标题常常是第一句话的截断,而第一句话可能是 `# 任务:…`。原样放进 header
    会变成 `# 会话记录:# 任务:…`,把一整个文档的标题层级搞乱,所以剥掉开头的
    Markdown 井号——那本来也不是标题的一部分。
    """
    title = ""
    for r in records:
        if r.get("type") == "session/title":
            v = dsh_data(r).get("title")
            if isinstance(v, str) and v.strip():
                title = v.strip()
    stripped = re.sub(r"^#+\s*", "", title).strip()
    return stripped or title


def read_dsh_title_file(path: Path, max_lines: int = 400) -> str:
    """只读开头若干行找标题,别为了一个字符串把整本日志解压进内存。"""
    title = ""
    try:
        for i, line in enumerate(iter_lines(path)):
            if i >= max_lines:
                break
            if '"session/title"' not in line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            v = dsh_data(r).get("title")
            if isinstance(v, str) and v.strip():
                title = v.strip()
    except (OSError, ValueError, ZstdUnavailable):
        return ""
    return title


def dsh_stem(path: Path, session_id: str) -> str:
    """给导出文件起名用的 stem。

    dsh 的日志文件名恒为 session.v4.jsonl,直接拿它当 stem 会让所有会话撞到
    同一份包上;会话 ID 是唯一且稳定的,用它。
    """
    if session_id:
        return session_id
    if path.name in (DSH_LOG_NAME, DSH_LOG_ZSTD):
        return path.parent.name
    return path.stem


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
    """压缩版:单条助手发言截断,合并后再按单轮截一次;工具调用只留次数。"""
    says = []
    names = Counter()
    order = []
    for kind, val in blocks:
        if kind == "say":
            says.append(clip(val, COMPACT_SAY))
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
    """上一次跑脚本留下的临时文件,别让它冒充「改过的文件」。

    判据是「在不在临时导出目录里」,而不是「路径里有没有 workbuddy-fork」——
    这个仓库自己就叫这个名字,按名字判会把它下面改过的文件全滤掉,
    「改过的文件」那一节永远空着,而包照常交付、看不出来。
    """
    p = Path(path)
    try:
        if p.resolve().is_relative_to(TEMP_ROOT):
            return True
    except (OSError, ValueError):
        pass
    return bool(re.search(r"-(?:package|from\d+(?:-package)?)\.md$", p.as_posix()))


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
            if name in EDIT_TOOLS or name in READ_TOOLS:
                p = file_arg(a)
                if not p or is_temp_artifact(p):
                    continue
                (read if name in READ_TOOLS else edited)[p] += 1
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
        return str(Path(path).relative_to(base)).replace("\\", "/")
    except (ValueError, OSError):
        return path


def code_span(s: str) -> str:
    """行内代码:内容带反引号就换双反引号围栏,别把后面的 Markdown 全吃掉。"""
    return f"`` {s} ``" if "`" in s else f"`{s}`"


def plain(s: str) -> str:
    """报错原文原样保留,但把反引号换成安全字符,免得开启代码段。"""
    return s.replace("`", "'")


def render_facts(facts, base=""):
    lines = []
    if facts["edited"]:
        lines.append("- 改过的文件(按改动次数):")
        for p, n in facts["edited"]:
            lines.append(f"  - {code_span(rel_path(p, base))} ×{n}")
    if facts["cmds"]:
        lines.append("- 跑过的命令(有代表性):")
        for c in facts["cmds"]:
            lines.append(f"  - {code_span(c)}")
    if facts["errors"]:
        lines.append("- 踩到的报错(原文):")
        for e in facts["errors"]:
            lines.append(f"  - {plain(e)}")
    if facts["urls"]:
        lines.append("- 查过的链接:")
        for u in facts["urls"]:
            lines.append(f"  - {u}")
    if facts["read"]:
        lines.append("- 读得最多的文件:")
        for p, n in facts["read"]:
            lines.append(f"  - {code_span(rel_path(p, base))} ×{n}")
    return lines


# ---------- 输出 ----------

# 日志里没有 sessionId 时,ID 是文件名顶上去的。交付物里必须标出来,否则
# 「日志自己说的」和「我们按文件名猜的」在纸面上长得一模一样。
ID_GUESS_NOTE = "(日志里没有 sessionId,按文件名填)"


def id_note(meta) -> str:
    """ID 是猜的就带上说明。header 和包里的「来源会话」都用它,免得各写各的。"""
    return ID_GUESS_NOTE if meta.get("id_from_filename") else ""


def render_turns(turns, start=1, end=None, compact=False):
    last = end if end is not None else len(turns)
    parts = []
    for idx, turn in enumerate(turns, 1):
        if idx < start or idx > last:
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
        f"- 来源会话 `{meta['session_id']}`{id_note(meta)}"
        f",第 {meta['start']} ~ {meta['end']} 个用户发言(共 {meta['total']} 个)",
    ]
    if meta.get("source") == "dsh":
        lines.append("- 来源:dsh 会话导出(session.v4.jsonl)")
    if meta.get("project_dir"):
        lines.append(f"- 工作目录 `{meta['project_dir']}`")
    if meta.get("span"):
        suffix = "(这段)" if meta["start"] > 1 else ""
        lines.append(f"- 时间 {meta['span']}{suffix}")
    lines += render_facts(facts, meta.get("project_dir", ""))
    lines += [
        "",
        "## 这次要做",
        "<!-- 新方向,或「接着上一步往下走」 -->",
        "",
        "<!-- 填完删掉所有注释。长度由内容定:每一句都该是新对话查不回来的东西 -->",
    ]
    return "\n".join(lines)


def read_text_any(path: Path):
    """按顺序试几种编码,别直接进 errors='replace' —— 那样节名会全乱。"""
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "gbk", "big5"):
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace"), None


# 导出文件的第一行固定是它。认「这是不是包」之前先拿它排除「这是自己写出去的
# 东西」:导出正文里可能就带着包的小节名(用户把包贴回新对话,那句话又落进正文),
# 不先排除的话,同一个文件名第二次导出会被认成包,永久挡住。
EXPORT_HEAD = "# 会话记录:"


def looks_like_package(path: Path) -> bool:
    """这个路径是不是一份上下文包?

    先排除「这是导出」——包里永远没有 `# 会话记录:` 这一行,导出一定有。
    这一步必须排在按开头认包之前,否则导出的正文会把判据骗过去。

    剩下的按名字认(`<会话ID>[-fromN]-package.md`),再按开头认一遍——
    用户可能把包另存成了别的名字,而那是他手填的唯一副本。
    """
    if not path.is_file():
        return False
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:400]
    except OSError:
        # 读不出来(被占用、没权限)不能因此判成「不是包」——那正好会让正文
        # 盖掉用户手填的那一份。退回按名字判:宁可多挡一次。
        return path.name.endswith("-package.md")
    if head.lstrip("\ufeff \t\r\n").startswith(EXPORT_HEAD):
        return False
    if path.name.endswith("-package.md"):
        return True
    return "<!-- 填完删" in head or "## 之前那条线到哪了" in head


def write_atomic(path: Path, text: str):
    """先写同目录临时文件再 rename,读侧不会读到半截。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".fork-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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

    text, enc = read_text_any(p)
    if enc is None:
        print("(文件编码认不出来,按替换字符读的,节名可能不准)\n")
    print(f"{p}\n共 {len(text)} 字符\n")
    if not text.strip():
        print("空文件,没有可看的篇幅分布。是不是存错了文件?")
        return 0

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

    print("\n看哪一节在膨胀,不看总数:")
    print("  关键事实占大头   → 里面多半是仓库查得到的东西,换成路径")
    print("  某一节在讲过程   → 只留结论和原因,过程砍掉")
    print("  节数比常规多     → 可能跨了好几条线,按 outline 拆包")
    return 0


def positive_int(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"要整数,收到 {value!r}")
    if n < 1:
        raise argparse.ArgumentTypeError("必须 ≥ 1")
    return n


# ---------- 子命令 ----------

def dsh_logs():
    """本机 dsh 会话,按 mtime 倒序。会话目录名就是会话 ID。"""
    rows = []
    for name in (DSH_LOG_ZSTD, DSH_LOG_NAME):
        rows.extend(p for p in DSH_SESSIONS.glob(f"*/*/{name}") if p.is_file())
    return sorted(rows, key=lambda p: p.stat().st_mtime, reverse=True)


def find_dsh_log(session_id: str):
    """按 ID 找 dsh 会话,返回 (状态, 命中列表, 备注)。前缀匹配同样要 ID 够长。"""
    exact = [p for p in dsh_logs() if p.parent.name == session_id]
    if exact:
        return ("ok", exact, "")
    if len(session_id) < 8:
        return ("none", [], "")
    partial = [p for p in dsh_logs() if p.parent.name.startswith(session_id)]
    if len(partial) == 1:
        return ("ok", partial, "")
    if len(partial) > 1:
        return ("ambiguous", partial, "")
    return ("none", [], "")


def dsh_log_in(path: Path):
    """给一个目录,找出里面那本 dsh 日志(解压后的导出目录就是这种形状)。

    先看目录本身,再看往下**一层**:有些工具解压时会多套一层以会话 ID 命名的
    目录。往下只认唯一那一本——命中多本就返回 None,让调用方报错,不替用户挑。
    """
    for name in (DSH_LOG_ZSTD, DSH_LOG_NAME):
        cand = path / name
        if cand.is_file():
            return cand
    deeper = []
    for name in (DSH_LOG_ZSTD, DSH_LOG_NAME):
        deeper.extend(p for p in path.glob(f"*/{name}") if p.is_file())
    return deeper[0] if len(deeper) == 1 else None


def cmd_list(args) -> int:
    source = args.source
    if source == "dsh":
        rows = dsh_logs()
        if not rows:
            print(f"没有找到任何 dsh 会话记录。检查 DSH_HOME 或 --path。({DSH_SESSIONS})")
            return 1
        print(f"最近的 dsh 会话(共 {len(rows)} 个,只显示最近 20 个):\n")
        for p in rows[:20]:
            title = read_dsh_title_file(p) or "(无标题)"
            print(f"{p.parent.name}  {fmt_time(p.stat().st_mtime)}  {p.parent.parent.name}  {title}")
        return 0

    rows = all_logs()
    if not rows:
        print("没有找到任何会话记录。检查 WORKBUDDY_HOME 或 --session。")
        return 1
    print(f"最近的会话(共 {len(rows)} 个,只显示最近 20 个;更早的用 --session <ID> 指):\n")
    for p in rows[:20]:
        title = read_title_file(p) or "(无标题)"
        print(f"{p.stem}  {fmt_time(p.stat().st_mtime)}  {p.parent.name}  {title}")
    return 0


def find_by_content(session_id: str, limit: int = 60):
    """文件名对不上时,按 jsonl 里的 sessionId 字段反查。

    有些会话的日志文件名和会话 ID 不一致(接续会话、子代理会话)。只按
    文件名找会扑空,而扑空后如果直接退回「最近一个会话」,就会拿着
    另一条线的原文去填包——用户完全看不出来。
    """
    for p in all_logs()[:limit]:
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            continue
        if f'"sessionId":"{session_id}"' in head or f'"sessionId": "{session_id}"' in head:
            return p
    return None


def recent_lines(n: int = 5):
    """最近几个会话的一行摘要,用来在定位失败时给用户挑。"""
    rows = []
    for p in all_logs()[:n]:
        rows.append(f"  {p.stem}  {fmt_time(p.stat().st_mtime)}  {p.parent.name}  "
                    f"{read_title_file(p) or '(无标题)'}")
    return rows


def resolve_session(args):
    """定位会话,返回 (路径, 备注);失败返回 (None, 错误信息或空串)。

    对不上就是错误,不猜。退到别的会话要用户显式加 --newest——因为
    「导出成功」和「导出了你要的那条」是两件事,而后者错了没人看得出来。
    """
    # --path 直接给文件,不走任何定位:用户已经指明是哪一本了。
    if args.path:
        p = Path(args.path).expanduser()
        if p.is_dir():
            inner = dsh_log_in(p)
            if inner:
                p = inner
        if not p.exists():
            return None, f"路径不存在:{p}"
        if p.is_dir():
            return None, (f"这是个目录,里面没有 {DSH_LOG_NAME}:{p}\n"
                          "指到解压出来的会话目录,或者直接给 zip。")
        return p, ""

    if args.source == "dsh":
        return resolve_dsh(args)

    explicit = bool(args.session)
    session_id = args.session or os.environ.get("CODEBUDDY_SESSION_ID")

    if session_id and not re.fullmatch(r"[A-Za-z0-9._\-]+", session_id):
        return None, ("会话 ID 只能含字母、数字和 - _ .  "
                      f"(收到 {session_id!r};带路径分隔符会把 --session 变成任意文件读取)")

    def fallback(reason: str):
        newest = newest_log()
        if args.newest and newest:
            return newest, f"{reason};已按 --newest 退到「{read_title_file(newest) or newest.stem}」"
        print(f"{reason}", file=sys.stderr)
        print("要导出最近一次会话就加 --newest;要指定某一本就先 --list 挑。最近的会话:", file=sys.stderr)
        for line in recent_lines():
            print(line, file=sys.stderr)
        return None, ""

    if not session_id:
        return fallback("拿不到会话 ID(不在 WorkBuddy 会话里运行?)")

    status, hits, note = find_log(session_id)
    if status == "ambiguous":
        print(f"「{session_id}」匹配到 {len(hits)} 个会话,不猜。用更长的 ID:", file=sys.stderr)
        for p in hits[:10]:
            print(f"  {p.stem}  {p.parent.name}  {read_title_file(p) or '(无标题)'}", file=sys.stderr)
        return None, ""
    if status == "none":
        by_content = find_by_content(session_id)
        if by_content:
            return by_content, f"日志文件名和会话 ID 不一致,按文件里的 sessionId 找到了 {by_content.name}"
        if explicit:
            return None, f"找不到会话 {session_id} 的日志。用 --list 看看有哪些。"
        return fallback(f"会话 {session_id} 的日志不在磁盘上(后台/子代理会话常见)")

    return hits[0], note


def resolve_dsh(args):
    """在 ~/.dsh/sessions 里按 ID 找。判据和 WorkBuddy 那边一样:不猜。"""
    session_id = args.session or ""
    if session_id and not re.fullmatch(r"[A-Za-z0-9._\-]+", session_id):
        return None, ("会话 ID 只能含字母、数字和 - _ .  "
                      f"(收到 {session_id!r};带路径分隔符会把 --session 变成任意文件读取)")

    def fallback(reason: str):
        rows = dsh_logs()
        if args.newest and rows:
            return rows[0], f"{reason};已按 --newest 退到「{rows[0].parent.name}」"
        print(reason, file=sys.stderr)
        print("要指定某一本就先 --list --source dsh 挑;要最近的就加 --newest;"
              "也可以 --path 直接给导出包。最近的 dsh 会话:", file=sys.stderr)
        for p in rows[:5]:
            print(f"  {p.parent.name}  {fmt_time(p.stat().st_mtime)}  "
                  f"{read_dsh_title_file(p) or '(无标题)'}", file=sys.stderr)
        return None, ""

    if not session_id:
        return fallback("没有给会话 ID")

    status, hits, note = find_dsh_log(session_id)
    if status == "ambiguous":
        print(f"「{session_id}」匹配到 {len(hits)} 个 dsh 会话,不猜。用更长的 ID:", file=sys.stderr)
        for p in hits[:10]:
            print(f"  {p.parent.name}", file=sys.stderr)
        return None, ""
    if status == "none":
        return fallback(f"找不到 dsh 会话 {session_id} 的日志")
    return hits[0], note


@dataclass
class Session:
    """被导出的那一份会话:原文记录 + 已经切成轮次的主线。"""

    id: str
    records: list
    turns: list
    title: str
    # 日志文件名,只用来给导出的文件起名。文件名不等于会话 ID,而「已有的包
    # 不覆盖」这条保证靠的就是名字稳定——口径换了,老版本手填的包会看不见。
    stem: str
    # 日志里没有 sessionId,id 是文件名顶上去的。交付物里要标出来,
    # 否则读包的人分不清「日志说的 ID」和「我们猜的 ID」。
    id_from_filename: bool = False
    # 来源:workbuddy / dsh。header 里要写出来——两个来源的包长得一模一样,
    # 半年后没人分得清哪个是从哪导的。
    source: str = "workbuddy"
    # dsh 把工作目录写在 header 里,比 WorkBuddy 的「取众数」硬得多。
    cwd: str = ""
    calls: int = 0
    # dsh:被白名单挡掉的非用户消息条数 / 压缩检查点处数。两者都要写进 header,
    # 否则「静默少吃」在纸面上看不出来。
    ignored: int = 0
    compactions: int = 0


def open_session(args):
    """定位会话并抽出主线,返回 (Session, 退出码);失败时 Session 是 None。

    定位失败就是错误,不猜:「导出成功」和「导出了你要的那条」是两件事,
    而后者错了没人看得出来。
    """
    path, note = resolve_session(args)
    if not path:
        if note:
            print(note, file=sys.stderr)
        return None, 2
    if note:
        print(f"({note})", file=sys.stderr)

    source = args.source if args.source != "auto" else sniff_source(path)
    if source == "dsh":
        return open_dsh_session(path)
    if source != "workbuddy":
        print(f"认不出这是哪种会话日志:{path}", file=sys.stderr)
        print("用 --source workbuddy|dsh 显式指定,或换个文件。", file=sys.stderr)
        return None, 2
    return open_workbuddy_session(path)


def open_workbuddy_session(path: Path):
    records = load_records(path)
    turns = build_turns(records)
    if not turns:
        print("这个会话里没有可提取的对话。", file=sys.stderr)
        print("如果日志不是 WorkBuddy 格式(比如 dsh 导出的),用 --source dsh 或 --path 指过去。",
              file=sys.stderr)
        return None, 2

    sid, guessed = session_identity(path, records)
    if guessed:
        print(f"(日志里没有 sessionId,来源会话按文件名填:{path.name})", file=sys.stderr)
    elif sid != path.stem:
        # 用户拿文件名当 ID 找过来,header 里却是另一个 ID。不吭一声的话,
        # 两处对不上看着就像脚本写错了。
        print(f"(日志里的 sessionId 是 {sid},和文件名 {path.stem} 不一致;来源会话按日志写)",
              file=sys.stderr)
    calls = sum(1 for r in records if r.get("type") == "function_call")
    return Session(sid, records, turns, read_title(records), path.stem, guessed,
                   calls=calls), 0


def open_dsh_session(path: Path):
    try:
        records = load_records(path)
    except ZstdUnavailable as e:
        print(str(e), file=sys.stderr)
        return None, 2
    except (OSError, ValueError) as e:
        print(f"读不了这份日志:{path}\n  {e}", file=sys.stderr)
        return None, 2

    turns, stats = build_turns_dsh(records)
    if not turns:
        print("这份 dsh 会话里没有用户发言,提取会全部落空。", file=sys.stderr)
        print(f"(日志里有 {stats['ignored']} 条非用户消息,没有一条 source.kind=user)",
              file=sys.stderr)
        return None, 2

    header = read_dsh_header(records)
    raw_id = header.get("id") if isinstance(header.get("id"), str) else ""
    raw_id = raw_id.strip()
    guessed = not raw_id
    sid = raw_id or dsh_stem(path, "")
    cwd = header.get("cwd") if isinstance(header.get("cwd"), str) else ""
    if guessed:
        print(f"(日志里没有会话 id,来源会话按文件名填:{sid})", file=sys.stderr)
    if stats["unknown"]:
        kinds = ",".join(f"{k}({n} 条)" for k, n in stats["unknown"].most_common())
        print(f"(遇到没见过的 source.kind:{kinds};已按注入丢弃)", file=sys.stderr)

    return Session(
        id=sid, records=records, turns=turns, title=read_dsh_title(records),
        stem=dsh_stem(path, sid), id_from_filename=guessed, source="dsh",
        cwd=cwd.strip(), calls=stats["calls"],
        ignored=stats["ignored"], compactions=stats["compactions"],
    ), 0


def session_identity(path: Path, records):
    """(会话 ID, 是不是按文件名凑的)。取不到真正的 ID 才退回文件名。"""
    real = read_session_id(records)
    return (real, False) if real else (path.stem, True)


def cmd_outline(turns, title, session_id) -> int:
    print(f"会话「{title or session_id}」共 {len(turns)} 个用户发言:\n")
    for i, turn in enumerate(turns, 1):
        said = (turn["user"] or "").replace("\n", " ")
        if len(said) > 72:
            said = said[:72] + "…"
        print(f"  [{i:>3}] {said}")
    print("\n--from N 从第 N 个用户发言**含**这一句开始。")
    return 0


# ---------- 命令行 ----------

# 超过这个长度,整段读会吃掉大量上下文,该建议降级
LONG_DOC = 150_000


def build_parser():
    ap = argparse.ArgumentParser(
        description="导出 WorkBuddy 会话主线,用于开新分支",
        epilog="读全量的判据不是轮数,是「包的四个待填节能不能写出内容」。",
    )
    ap.add_argument("--session", "-s", help="会话 ID,默认取当前会话(CODEBUDDY_SESSION_ID)")
    ap.add_argument("--path", help="直接给文件:dsh 导出 zip、解压目录,或 session.v4.jsonl(.zstd)")
    ap.add_argument("--source", choices=["auto", "workbuddy", "dsh"], default="auto",
                    help="日志来源;给了 --path 时按内容认,不给时按 workbuddy 找")
    ap.add_argument("--list", "-l", action="store_true", help="列出所有项目最近的会话")
    ap.add_argument("--outline", "-o", action="store_true", help="只列用户发言,挑分叉点用")
    ap.add_argument("--from", dest="start", type=positive_int, default=None,
                    help="从第几个用户发言开始(含这一句);不给就是从第 1 个")
    ap.add_argument("--last", type=positive_int,
                    help="取 N 个;配 --from 时是「从 N 往后取 M 个」,不配时取最后 N 个")
    ap.add_argument("--newest", action="store_true",
                    help="会话 ID 对不上时,改用最近活跃的会话(默认是报错,不猜)")
    ap.add_argument("--compact", action="store_true", help="压缩版:助手发言截断、工具调用只留次数")
    ap.add_argument("--package", action="store_true", help="同时生成上下文包骨架")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的上下文包")
    ap.add_argument("--check", metavar="FILE", help="按节报篇幅占比,指出哪节在膨胀")
    ap.add_argument("--out", help="输出文件路径,默认写系统临时目录")
    ap.add_argument("--stdout", action="store_true", help="直接打印全文到标准输出")
    return ap


def validate_args(ap, args):
    """互斥检查。--check / --list 不看正文,所以这一步排在它们后面跑。"""
    if args.stdout and args.package:
        ap.error("--stdout 与 --package 互斥:包总是先落盘再打印")
    if args.force and not args.package:
        ap.error("--force 只对 --package 有意义:它管的是「不要覆盖已有的包」")
    if args.stdout and args.out:
        ap.error("--stdout 与 --out 互斥:一个往屏幕写,一个往文件写")
    if args.path and args.session:
        ap.error("--path 与 -s 互斥:一个是给文件,一个是给会话 ID")


def compute_range(args, total):
    """把 --from / --last 换算成 1-based 闭区间 (start, end)。

    --from 是起点,--last 是长度,两个可以一起给。判据必须是「用户有没有
    给过 --from」而不是「start 是不是 1」——否则 --from 1 --last 40 会被
    当成「最后 40 句」,分叉点悄悄跑到尾巴上去。
    """
    start = args.start if args.start is not None else 1
    end = total
    if args.last:
        if args.start is not None:
            end = min(total, start + args.last - 1)
        else:
            start = max(1, total - args.last + 1)
    return start, end


def segment_span(turns, start, end):
    """这段发言的时间跨度。没有时间戳就写 ?,不编。"""
    stamps = [t["ts"] for t in turns[start - 1:end] if t.get("ts")]
    return f"{fmt_time(min(stamps))} ~ {fmt_time(max(stamps))}" if stamps else "?"


def is_partial(meta) -> bool:
    """这次导出的是全量还是一段。

    header 里的「本段」和包名里的 `-fromN` 都靠它,只写一处——两处各写一遍
    「start > 1 or end < total」的话,改其中一处时另一处极容易漏掉。
    """
    return meta["start"] > 1 or meta["end"] < meta["total"]


def document_header(meta, calls, compact):
    """导出的文件头。meta 里的会话 ID / 工作目录 / 时间跨度是模型猜不出来的真事实。"""
    is_dsh = meta.get("source") == "dsh"
    lines = [
        f"{EXPORT_HEAD}{meta['title'] or '(无标题)'}",
        "",
        f"- 会话 ID:`{meta['session_id']}`{id_note(meta)}",
    ]
    if is_dsh:
        lines.append("- 来源:dsh 会话导出(session.v4.jsonl)")
    lines += [
        f"- 工作目录:`{meta['project_dir'] or '未知'}`",
        f"- 时间:{meta['span']}",
        f"- 全量:用户发言 {meta['total']} 次,工具调用 {calls} 次",
    ]
    if is_dsh:
        # 丢了多少必须写出来。白名单之外的东西不进正文是对的,但「不进」和
        # 「少吃了」在纸面上必须能分开——不写这一行,两者长得一模一样。
        # 压缩检查点单独数,不混进「非用户消息」以外的说法里(见 GLOSSARY)。
        line = f"- 忽略:非用户消息 {meta.get('ignored', 0)} 条"
        if meta.get("compactions"):
            line += f",其中压缩检查点 {meta['compactions']} 处(原文完整保留)"
        lines.append(line)
    if compact:
        lines.append(f"- 模式:压缩版(单条助手 {COMPACT_SAY} 字、单轮 {COMPACT_TURN} 字、用户 {COMPACT_USER} 字)")
    if is_partial(meta):
        lines.append(f"- 本段:第 {meta['start']} ~ {meta['end']} 个用户发言"
                     f"(共 {meta['total']} 个,其余未包含)")
    return lines


def package_path_for(out_path: Path, meta) -> Path:
    """包和正文落在同一个目录,靠名字区分。分段导出才带 -fromN。

    名字用日志文件名而不是会话 ID:同一个会话每轮导出要落到同一份包上,
    「已有的包默认不覆盖」才成立。两者不一致时优先照顾这条。
    """
    suffix = f"-from{meta['start']}" if is_partial(meta) else ""
    return out_path.parent / f"{meta['file_stem']}{suffix}-package.md"


def warn_too_long(compact, start):
    print("\n注意:这段太长(>150k 字符),整段读会吃掉大量上下文。")
    if compact:
        print(f"已经是最紧的模式了。改用 --from {start} --last 20 分两段,各出一份包。")
    else:
        print("加 --compact 压一版,或 --last 40 只取最近一段。")


def emit_package(pkg_path: Path, seg, meta, force: bool) -> int:
    """写包骨架。已有包默认不覆盖——那是用户手填的唯一副本。"""
    if pkg_path.exists() and not force:
        print(f"\n已有一份包,没覆盖:{pkg_path}")
        print("下面是它现在的内容,确认是不是已经填好了;要重写加 --force。\n")
        print(read_text_any(pkg_path)[0])
        return 0

    skeleton = package_skeleton(meta, collect_facts(seg))
    try:
        write_atomic(pkg_path, skeleton)
    except OSError as e:
        print(f"包写不进去:{pkg_path}\n  {e}", file=sys.stderr)
        return 2

    print(f"\n上下文包骨架:{pkg_path}({len(skeleton)} 字符)")
    print("\n--- 骨架预览(填完删注释,再跑 --check 看哪节膨胀)---\n")
    print(skeleton)
    return 0


def write_export(out_path: Path, pkg_path: Path, doc: str, meta, calls: int, compact: bool) -> int:
    """把正文落盘,并打印给人看的规模摘要。返回退出码。"""
    if out_path.is_dir():
        print(f"--out 需要文件路径,这是目录:{out_path}", file=sys.stderr)
        return 2

    # 不管这次有没有 --package,都不能让正文落在包上。包是用户手填的唯一
    # 副本,覆盖掉不会有任何提示,而「导出成功」还会照常打印。
    if out_path.resolve() == pkg_path.resolve() or looks_like_package(out_path):
        print(f"--out 指向了上下文包本身,正文会把它覆盖掉:{out_path}", file=sys.stderr)
        print("换一个文件名导出正文;要重写包就用 --package --force。", file=sys.stderr)
        return 2

    try:
        write_atomic(out_path, doc)
    except OSError as e:
        print(f"写不进去:{out_path}\n  {e}", file=sys.stderr)
        return 2

    print(f"已导出:{out_path}")
    print(f"规模:{len(doc)} 字符 / {len(doc.splitlines())} 行")
    print(f"跨度:第 {meta['start']} ~ {meta['end']} 个用户发言"
          f"(共 {meta['total']} 个,工具调用 {calls} 次)")
    if len(doc) > LONG_DOC:
        warn_too_long(compact, meta["start"])
    return 0


def main():
    ap = build_parser()
    args = ap.parse_args()

    if args.check:
        return check_package(args.check)
    if args.list:
        return cmd_list(args)
    validate_args(ap, args)

    sess, code = open_session(args)
    if sess is None:
        return code

    total = len(sess.turns)
    if args.outline:
        return cmd_outline(sess.turns, sess.title, sess.id)

    start, end = compute_range(args, total)
    body = render_turns(sess.turns, start, end, compact=args.compact)
    if not body.strip():
        print(f"从第 {start} 个发言开始没有内容(总共只有 {total} 个)。", file=sys.stderr)
        return 2

    calls = sess.calls
    meta = {
        "session_id": sess.id,
        "file_stem": sess.stem,
        "id_from_filename": sess.id_from_filename,
        "title": sess.title,
        # dsh 的 header 里就有 cwd;没有才退回「取众数」。留空比填错好。
        "project_dir": sess.cwd or read_cwd(sess.records),
        "start": start,
        "end": end,
        "total": total,
        "span": segment_span(sess.turns, start, end),
        "source": sess.source,
        "ignored": sess.ignored,
        "compactions": sess.compactions,
    }
    doc = "\n".join(document_header(meta, calls, args.compact)) + "\n\n---\n\n" + body

    if args.stdout:
        if len(doc) > LONG_DOC:
            print(f"注意:这段 {len(doc)} 字符,整段读会吃掉大量上下文。"
                  "加 --compact,或用 --last 只取一段。", file=sys.stderr)
        sys.stdout.write(doc)
        return 0

    out_path = Path(args.out) if args.out else TEMP_ROOT / f"{sess.stem}-from{start}.md"
    pkg_path = package_path_for(out_path, meta)
    code = write_export(out_path, pkg_path, doc, meta, calls, args.compact)
    if code:
        return code

    if args.package:
        return emit_package(pkg_path, sess.turns[start - 1:end], meta, args.force)
    return 0


if __name__ == "__main__":
    _code = main()
    try:
        # 显式 flush:留到退出时写,下游 `| head` 已经关掉管道,Python 只能
        # 在解释器收尾时报错并给退出码 120,调用方会当成失败。Windows 上
        # 抛的是 OSError(EINVAL) 而不是 BrokenPipeError,所以两种都接。
        sys.stdout.flush()
    except OSError:
        os._exit(0)
    sys.exit(_code)
