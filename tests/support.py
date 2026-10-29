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


# ---------- 跑 CLI ----------

def run_cli(*args, home=None, session=None, extra_env=None):
    """跑真实的 fork.py 子进程,返回 CompletedProcess(bytes)。"""
    env = {k: v for k, v in os.environ.items() if k != "CODEBUDDY_SESSION_ID"}
    if home is not None:
        env["WORKBUDDY_HOME"] = str(home)
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
