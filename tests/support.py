"""测试共用:造 jsonl 夹具、起 CLI 子进程。

所有测试都靠 WORKBUDDY_HOME 指到临时目录,**绝不读真实会话**——
否则用例的结果取决于这台机器上聊过什么。
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
SCRIPTS = REPO / "scripts"
FORK_PY = SCRIPTS / "fork.py"

# 函数级用例要 import fork。import 时它会读 WORKBUDDY_HOME 定下 PROJECTS_DIR,
# 所以先把家目录指到一个空临时目录,免得误扫真实会话。
# 不能用 setdefault:环境里本来就有这个变量时它会静默沿用,PROJECTS_DIR 就指到
# 真实家目录去了——那样用例的结果取决于这台机器上聊过什么。
_IMPORT_HOME = tempfile.mkdtemp(prefix="wbfork-import-")
os.environ["WORKBUDDY_HOME"] = _IMPORT_HOME
# dsh 的家目录同理:--list/--newest 会去扫 ~/.dsh/sessions,指到空目录才不会
# 让用例的结果取决于这台机器上有哪些会话。
os.environ["DSH_HOME"] = tempfile.mkdtemp(prefix="dshfork-import-")
sys.path.insert(0, str(SCRIPTS))

import fork  # noqa: E402  (必须放在指好家目录之后)


# ---------- 造记录 ----------

def ai_title(text, ts=0):
    return {"type": "ai-title", "timestamp": ts, "title": text}


def user_turn(i, text):
    return {
        "type": "message",
        "role": "user",
        "timestamp": 1_000 + i * 10_000,
        "content": [{"type": "input_text", "text": text}],
    }


def assistant_turn(i, text):
    return {
        "type": "message",
        "role": "assistant",
        "timestamp": 1_000 + i * 10_000 + 5_000,
        "content": [{"type": "text", "text": text}],
    }


def write_session(home, records, session_id="sess0001", project="proj"):
    """把记录写成 <home>/projects/<project>/<session_id>.jsonl。"""
    d = Path(home) / "projects" / project
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    with open(path, "w", encoding="utf-8", newline="") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def simple_session(home, n=5, session_id="sess0001", cwd=None):
    """n 个用户发言的会话,每个都带一句助手回复。

    cwd 给了就写进每条用户记录——脚本从日志里读工作目录,读不到宁可留空,
    所以想测「工作目录」那行就必须真的写进去。
    """
    recs = [ai_title("测试会话")]
    for i in range(1, n + 1):
        u = user_turn(i, f"第{i}个问题")
        if cwd:
            u["cwd"] = cwd
        recs.append(u)
        recs.append(assistant_turn(i, f"第{i}个回答"))
    write_session(home, recs, session_id)
    return session_id


# ---------- 造 dsh 记录 ----------
#
# 结构与真实日志逐字段对齐(见 docs/adr/0007)。少写一个字段就会让用例
# 测到一个真实日志里不存在的形状。

DSH_SESSION_ID = "session-11111111-2222-3333-4444-555555555555"


def dsh_event(etype, seq, data, ts=None, **extra):
    rec = {
        "type": etype,
        "seq": seq,
        "time": 1_700_000_000_000 + seq * 1000 if ts is None else ts,
        "data": data,
    }
    rec.update(extra)
    return rec


def dsh_header(session_id=DSH_SESSION_ID, cwd="D:\\code\\demo"):
    """第一条记录没有 seq/time —— 真实日志就是这样。"""
    return {
        "type": "session",
        "version": 4,
        "id": session_id,
        "createdAt": 1_700_000_000_000,
        "cwd": cwd,
        "isSeeded": False,
        "delegationDepth": 0,
        "agentPreset": "ptc-minimal",
    }


def dsh_title(text, seq=2):
    return dsh_event("session/title", seq, {
        "title": text,
        "messageSeqs": [3],
        "source": {"kind": "provider", "provider": "session-title-first-prompt-llm"},
    })


def dsh_user(seq, text, kind="user"):
    """kind=user 是用户真说的话;其余 kind 都是平台注入。"""
    source = ({"kind": "user", "rpcId": f"rpc-{seq}", "clientTimeZone": "Asia/Shanghai"}
              if kind == "user" else {"kind": kind})
    return dsh_event("user/message", seq, {
        "content": [{"type": "text", "text": text}],
        "source": source,
        "role": "user",
        "id": f"m{seq}",
    })


def dsh_assistant(seq, text=None, reasoning=None, call=None, turn=1, step=1):
    """text/reasoning 各是一段正文;call=(callId, name, args) 时带上 tool-call 块。"""
    parts = []
    if reasoning:
        parts.append({"type": "reasoning", "text": reasoning})
    if text:
        parts.append({"type": "text", "text": text})
    if call:
        cid, name, args = call
        parts.append({"type": "tool-call", "id": cid, "name": name,
                      "arguments": json.dumps(args, ensure_ascii=False)})
    return dsh_event("assistant/message", seq, {
        "turn": turn,
        "step": step,
        "message": {
            "role": "assistant",
            "content": parts,
            "source": {"kind": "model", "provider": "deepseek-official", "model": "m"},
            "id": f"a{seq}",
        },
        "usage": {"inputTokens": 1, "outputTokens": 1},
    })


def dsh_tool_call(seq, name, args, call_id="run-1", turn=1, step=1):
    """直接模式下的工具调用:名字就是真工具(bash/read/edit),不是 run_code 容器。

    和 PTC 模式的区别只在「有没有 ptc-dispatch 挂在它下面」——判据是结构,
    不是名字。
    """
    return dsh_event("tool/call", seq, {
        "turn": turn,
        "step": step,
        "callId": call_id,
        "name": name,
        "arguments": json.dumps(args, ensure_ascii=False),
    })


def dsh_dispatch(seq, name, args, text="", is_error=False, meta=None, turn=1, step=1):
    return dsh_event("tool/ptc-dispatch", seq, {
        "turn": turn,
        "step": step,
        "callId": f"sub-{seq}",
        "rootCallId": "run-1",
        "parentCallId": f"sub-{seq}",
        "subCallId": f"sub-{seq}",
        "name": name,
        "arguments": args,
        "isError": is_error,
        "content": [{"type": "text", "text": text}],
        "meta": meta or {},
    })


def dsh_result(seq, text, is_error=False, turn=1, step=1):
    return dsh_event("tool/result", seq, {
        "turn": turn,
        "step": step,
        "message": {
            "role": "tool",
            "source": {"kind": "tool", "callId": "run-1"},
            "toolCallId": "run-1",
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
            "id": f"r{seq}",
        },
        "meta": {},
    })


def dsh_checkpoint(seq, text, start_seq, end_seq, sources=()):
    """压缩检查点:用一份摘要替换掉第 start_seq~end_seq 条对模型可见的事件。"""
    return dsh_event("user/message", seq, {
        "content": [{"type": "text", "text": text}],
        "source": {"kind": "compact-checkpoint"},
        "role": "user",
        "id": f"cp{seq}",
    }, surfaceOp={"op": "replace", "startSeq": start_seq, "endSeq": end_seq},
        sourceEventSeqs=list(sources))


def dsh_turn(seq, n=1):
    return dsh_event("turn/start", seq, {"turn": n})


def write_dsh_records(directory, records, name="session.v4.jsonl"):
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    with open(path, "w", encoding="utf-8", newline="") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def write_dsh_layout(home, records, session_id=DSH_SESSION_ID, project="proj"):
    """<home>/sessions/<项目>/<会话id>/session.v4.jsonl —— 与 dsh 本地布局一致。"""
    d = Path(home) / "sessions" / project / session_id
    write_dsh_records(d, records)
    return d


def write_dsh_zip(path, records, name="session.v4.jsonl"):
    """导出包:一个 session.v4.jsonl + media/(这里不造 media)。"""
    import zipfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, body)
    return path


def zstd_backend() -> str:
    """本机能不能压/解 zstd。没有就跳过相关用例,而不是让整套测试挂在一个可选依赖上。"""
    try:
        from compression import zstd  # noqa: F401
        return "stdlib"
    except (ImportError, AttributeError):
        pass
    try:
        import zstandard  # noqa: F401
        return "zstandard"
    except ImportError:
        return ""


def compress_zstd(body: bytes) -> bytes:
    backend = zstd_backend()
    if backend == "stdlib":
        from compression import zstd
        return zstd.compress(body)
    if backend == "zstandard":
        import zstandard
        return zstandard.ZstdCompressor().compress(body)
    raise RuntimeError("本机没有 zstd 实现")


def dsh_body(records) -> bytes:
    return ("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n").encode("utf-8")


def write_dsh_zip_zstd(path, records, name="session.v4.jsonl.zstd"):
    """包里装的成员自己也是 zstd —— 有人会把活日志原样塞进 zip。"""
    import zipfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, compress_zstd(dsh_body(records)))
    return path


def write_dsh_zstd(directory, records, name="session.v4.jsonl.zstd"):
    """本机活日志的样子:~/.dsh/sessions/<项目>/<会话>/session.v4.jsonl.zstd。"""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_bytes(compress_zstd(dsh_body(records)))
    return path


# ---------- 跑 CLI ----------

def run_cli(*args, home=None, session=None, extra_env=None, dsh_home=None):
    """跑真实的 fork.py 子进程,返回 CompletedProcess(bytes)。"""
    env = {k: v for k, v in os.environ.items() if k != "CODEBUDDY_SESSION_ID"}
    if home is not None:
        env["WORKBUDDY_HOME"] = str(home)
    if dsh_home is not None:
        env["DSH_HOME"] = str(dsh_home)
    if session is not None:
        env["CODEBUDDY_SESSION_ID"] = str(session)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(FORK_PY), *args],
        capture_output=True, env=env, timeout=120,
    )


def out_text(proc):
    return proc.stdout.decode("utf-8", "replace")


def err_text(proc):
    return proc.stderr.decode("utf-8", "replace")
