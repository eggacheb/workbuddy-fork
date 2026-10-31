"""dsh 来源的表征测试:一种字段完全不同的日志,不能靠文本猜。

判据全部落在 `data.source.kind` 上(见 docs/adr/0007),所以这里钉的不是
「正则认不认得某种注入」,而是「白名单之外的东西一条都不许进正文,而且
丢了多少必须写在 header 上」。
"""

import json
import tempfile
import unittest
from pathlib import Path

import support
from support import (
    dsh_assistant, dsh_dispatch, dsh_header, dsh_result, dsh_title, dsh_turn,
    dsh_user, err_text, out_text, run_cli,
)


def 标准会话():
    """2 条真发言 + 6 种注入 + PTC 模式:run_code 容器里挂三个真工具。"""
    return [
        dsh_header(cwd="D:\\code\\demo"),
        dsh_title("整理项目结构"),
        dsh_turn(1),
        dsh_user(2, "<system-reminder>技能清单</system-reminder>", kind="skill-catalog"),
        dsh_user(3, "当前运行上下文", kind="runtime-context"),
        dsh_user(4, "本仓库的规矩", kind="agent-instructions"),
        dsh_user(5, "<skill_content name=\"x\">技能正文</skill_content>", kind="skill-invocation"),
        dsh_user(6, "PTC Plus 不可用", kind="plugin:ptc-plus"),
        dsh_user(7, "<path>/tmp/a.png</path><type>image</type>", kind="ptc-mode"),
        dsh_user(8, "把项目结构理一遍"),
        dsh_assistant(9, text="我先看看目录。", reasoning="这是内部思考,不该出现在导出里",
                      call=("run-1", "run_code", {"code": "await tools.bash({...})"})),
        support.dsh_tool_call(10, "run_code", {"code": "await tools.bash({...})"}),
        dsh_dispatch(11, "bash", {"command": "ls -la"}, text="total 0"),
        dsh_result(12, "total 0"),
        dsh_user(13, "顺便把 pcb 也看了"),
        dsh_dispatch(14, "read", {"file_path": "README.md"}, text="# demo"),
        dsh_dispatch(15, "edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"},
                     meta={"path": "a.py"}),
        dsh_dispatch(16, "ops_replace", {"path": "b.py", "pattern": "x", "replacement": "y"}),
        dsh_assistant(17, text="改完了。"),
    ]


def 直接模式会话():
    """没有 PTC:tool/call 里就是真工具本身(真实样本 session-195b8a08 就是这样)。"""
    return [
        dsh_header(cwd="F:\\毕设\\stm32智能门禁系统"),
        dsh_title("直接模式的会话"),
        dsh_turn(1),
        dsh_user(2, "这个 README 什么意思"),
        dsh_assistant(3, text="我读一下。",
                      call=("toolu_a", "read", {"file_path": "README.md"})),
        support.dsh_tool_call(4, "read", {"file_path": "README.md"}, call_id="toolu_a"),
        dsh_result(5, "1: 标题\n2: 介绍"),
        dsh_assistant(6, text="明白了。"),
    ]


class _DshHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.out = self.home / "out"
        self.out.mkdir()
        self.sid = support.DSH_SESSION_ID

    def tearDown(self):
        self._tmp.cleanup()

    def 跑(self, *args, records=None):
        write_dsh_layout(self.home, 标准会话() if records is None else records)
        return run_cli(*args, dsh_home=self.home)


def write_dsh_layout(home, records, session_id=None):
    return support.write_dsh_layout(home, records, session_id or support.DSH_SESSION_ID)


class 清洗(_DshHome):
    def test_白名单之外的发言一条都不进正文(self):
        r = self.跑("--stdout", "--source", "dsh", "-s", self.sid)

        self.assertEqual(r.returncode, 0, err_text(r))
        out = out_text(r)
        self.assertIn("把项目结构理一遍", out)
        self.assertIn("顺便把 pcb 也看了", out)
        for 噪音 in ("技能清单", "当前运行上下文", "本仓库的规矩", "技能正文",
                     "PTC Plus 不可用", "/tmp/a.png"):
            self.assertNotIn(噪音, out)
        # 丢了 6 条注入,这个数字必须写在纸面上,否则「静默少吃」看不出来
        self.assertIn("- 忽略:非用户消息 6 条", out)
        self.assertIn("- 来源:dsh 会话导出(session.v4.jsonl)", out)

    def test_工具行来自ptc_dispatch而不是run_code(self):
        r = self.跑("--stdout", "--source", "dsh", "-s", self.sid)

        out = out_text(r)
        self.assertIn("- bash(", out)
        self.assertIn("- read(", out)
        self.assertIn("- edit(", out)
        self.assertIn("- ops_replace(", out)
        # run_code 只是 PTC 的容器,露出来等于每轮都写一次「跑了段代码」
        self.assertNotIn("run_code", out)
        # reasoning 是模型的内部独白,和「用户说过什么」无关
        self.assertNotIn("内部思考", out)
        self.assertIn("我先看看目录。", out)

    def test_直接模式的工具调用也要出来(self):
        # 真实样本 session-195b8a08:没有 PTC,工具就是 tool/call 里的 read/bash。
        # 只认 ptc-dispatch 的话,这种会话导出的工具数是 0,而退出码照样是 0。
        r = self.跑("--stdout", "--source", "dsh", "-s", self.sid, records=直接模式会话())

        self.assertEqual(r.returncode, 0, err_text(r))
        out = out_text(r)
        self.assertIn("- read(README.md)", out)
        self.assertIn("工具调用 1 次", out)

    def test_一个turn里的第二条发言独立成节(self):
        # 真实日志里 turn 4 有两条发言,中间隔了 16 个 step。按 turn 编号
        # 会把第二条吞掉,--from 从此指错地方。
        r = self.跑("--stdout", "--source", "dsh", "-s", self.sid)

        out = out_text(r)
        self.assertIn("## [2] 用户", out)
        self.assertIn("顺便把 pcb 也看了", out)
        self.assertIn("## [2] 助手", out)
        # 编号单位是发言,不是 turn:两条发言就是 [1] 和 [2]
        self.assertIn("全量:用户发言 2 次", out)


    def test_包里的关键事实认得dsh的工具名(self):
        # 事实提取靠工具名认「改过哪些文件」。认不出 dsh 的小写名字,
        # 包里「关键事实」就会整节空着,而包照常交付。
        r = self.跑("--package", "--out", str(self.out / "x.md"),
                    "--source", "dsh", "-s", self.sid)

        self.assertEqual(r.returncode, 0, err_text(r))
        pkg = self.out / f"{self.sid}-package.md"
        text = pkg.read_text(encoding="utf-8")
        self.assertIn("- 改过的文件", text)
        self.assertIn("a.py", text)
        self.assertIn("b.py", text)


class 压缩(_DshHome):
    def 压缩过的会话(self):
        return [
            dsh_header(cwd="D:\\code\\demo"),
            dsh_title("压缩过的会话"),
            dsh_turn(1),
            dsh_user(3, "第一句话"),
            dsh_assistant(4, text="第一句的回答"),
            support.dsh_checkpoint(5, "【压缩摘要】前半段被压成了这段", 3, 4, sources=[3, 4]),
            dsh_turn(6, 2),
            dsh_user(7, "第二句话"),
            dsh_assistant(8, text="第二句的回答"),
        ]

    def test_检查点正文不导出_原文照留(self):
        r = self.跑("--stdout", "--source", "dsh", "-s", self.sid, records=self.压缩过的会话())

        self.assertEqual(r.returncode, 0, err_text(r))
        out = out_text(r)
        # 摘要是原文的机器摘要,信息量严格更小 —— 留着就是同一段内容出两份
        self.assertNotIn("压缩摘要", out)
        # 被 surface 替换掉的那段原文仍然在:日志是 append-only 的
        self.assertIn("第一句话", out)
        self.assertIn("第二句话", out)
        self.assertIn("压缩检查点 1 处(原文完整保留)", out)

    def test_检查点算进忽略条数里(self):
        r = self.跑("--stdout", "--source", "dsh", "-s", self.sid, records=self.压缩过的会话())

        out = out_text(r)
        self.assertIn("- 忽略:非用户消息 1 条", out)
        self.assertIn("其中压缩检查点 1 处", out)


class 分数段(_DshHome):
    def test_from二从第二条发言开始(self):
        r = self.跑("--from", "2", "--stdout", "--source", "dsh", "-s", self.sid)

        self.assertEqual(r.returncode, 0, err_text(r))
        out = out_text(r)
        self.assertIn("## [2] 用户", out)
        self.assertIn("顺便把 pcb 也看了", out)
        self.assertNotIn("## [1] 用户", out)
        self.assertIn("本段:第 2 ~ 2 个用户发言", out)

    def test_outline只列真发言(self):
        r = self.跑("--outline", "--source", "dsh", "-s", self.sid)

        self.assertEqual(r.returncode, 0, err_text(r))
        out = out_text(r)
        self.assertIn("共 2 个用户发言", out)
        self.assertIn("[  1] 把项目结构理一遍", out)
        self.assertNotIn("技能清单", out)


class 定位(_DshHome):
    def test_list只列dsh会话(self):
        self.跑("--outline", "--source", "dsh", "-s", self.sid)  # 顺手写一份会话
        r = run_cli("--list", "--source", "dsh", dsh_home=self.home)

        self.assertEqual(r.returncode, 0, err_text(r))
        out = out_text(r)
        self.assertIn(self.sid, out)
        self.assertIn("最近的 dsh 会话", out)

    def test_newest不给ID也能用(self):
        self.跑("--outline", "--source", "dsh", "-s", self.sid)
        r = run_cli("--newest", "--outline", "--source", "dsh", dsh_home=self.home)

        self.assertEqual(r.returncode, 0, err_text(r))
        self.assertIn("共 2 个用户发言", out_text(r))

    def test_没有ID也没有newest就报错不猜(self):
        self.跑("--outline", "--source", "dsh", "-s", self.sid)
        r = run_cli("--outline", "--source", "dsh", dsh_home=self.home)

        self.assertEqual(r.returncode, 2)
        self.assertIn("没有给会话 ID", err_text(r))


class 语料(unittest.TestCase):
    """全库回归:不变量破了必须非零退出,而不是打印一行数字了事。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.base = self.home / "base.json"
        support.write_dsh_layout(self.home, 标准会话())

    def tearDown(self):
        self._tmp.cleanup()

    def 跑(self, *args):
        import subprocess
        import sys
        return subprocess.run(
            [sys.executable, str(support.SCRIPTS / "corpus_check.py"),
             "--source", "dsh", "--home", str(self.home / "sessions"), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)

    def test_只数真发言_注入单独计(self):
        r = self.跑("--save", str(self.base))

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("原始发言", r.stdout)
        stats = json.loads(self.base.read_text(encoding="utf-8"))
        self.assertEqual(stats["原始发言"], 2)
        self.assertEqual(stats["计入发言"], 2)
        self.assertEqual(stats["注入了多少"], 6)

    def test_计入发言变少就是回归(self):
        self.跑("--save", str(self.base))
        stats = json.loads(self.base.read_text(encoding="utf-8"))
        stats["计入发言"] = 3  # 基线里本来有三条,现在只剩两条
        self.base.write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")

        r = self.跑("--compare", str(self.base))

        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("变少", r.stdout)

    def test_不带compare也查不变量(self):
        # 一次不带参数的回归跑完照样要能红,否则「今天没问题」是一句没人验证过的断言
        records = 标准会话() + [
            dsh_user(16, "平台新加的一种注入", kind="brand-new-kind"),
            dsh_assistant(17, text="嗯"),
        ]
        support.write_dsh_layout(self.home, records)

        r = self.跑()

        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("不认识的", r.stdout)

    def test_读不了的会话判失败(self):
        # 「跳过」和「没问题」不是一回事:一个会话都没扫到时报 0 个问题,
        # 等于把「没扫到」说成「没问题」。
        broken = self.home / "sessions" / "proj" / "session-broken"
        broken.mkdir(parents=True)
        (broken / "session.v4.jsonl.zstd").write_bytes(b"garbage")

        r = self.跑()

        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("读不了", r.stdout)

    def test_剥完只剩标签的发言会被抓出来(self):
        self.跑("--save", str(self.base))
        records = 标准会话() + [
            dsh_user(20, "<system-reminder></system-reminder>"),
            dsh_assistant(21, text="嗯"),
        ]
        support.write_dsh_layout(self.home, records)

        r = self.跑("--compare", str(self.base))

        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("幽灵发言", r.stdout)


class 输入(_DshHome):
    def test_导出zip可以直接读(self):
        records = 标准会话()
        zip_path = support.write_dsh_zip(self.out / "dsh-session-x.zip", records)
        r = run_cli("--path", str(zip_path), "--stdout", dsh_home=self.home)

        self.assertEqual(r.returncode, 0, err_text(r))
        out = out_text(r)
        self.assertIn("把项目结构理一遍", out)
        # 来源按内容认出来了,不用显式 --source
        self.assertIn("- 来源:dsh 会话导出(session.v4.jsonl)", out)

    def test_路径不存在就报错不猜(self):
        r = run_cli("--path", str(self.out / "没有这个文件.zip"), "--stdout",
                    dsh_home=self.home)

        self.assertEqual(r.returncode, 2)
        self.assertIn("路径不存在", err_text(r))

    def test_解压目录多套一层也能认(self):
        # 有些工具解压时会多套一层以会话 ID 命名的目录
        nested = self.out / "dsh-session-x" / support.DSH_SESSION_ID
        support.write_dsh_records(nested, 标准会话())

        r = run_cli("--path", str(self.out / "dsh-session-x"), "--outline",
                    dsh_home=self.home)

        self.assertEqual(r.returncode, 0, err_text(r))
        self.assertIn("共 2 个用户发言", out_text(r))

    @unittest.skipUnless(support.zstd_backend(), "本机没有 zstd 实现")
    def test_包里成员自己是zstd也能读(self):
        # 按明文读的话每行 JSON 都解析失败,最后报「没有用户发言」——
        # 那正是「不许出空包」要拦的形态。
        zip_path = support.write_dsh_zip_zstd(self.out / "z.zip", 标准会话())

        r = run_cli("--path", str(zip_path), "--outline", dsh_home=self.home)

        self.assertEqual(r.returncode, 0, err_text(r))
        self.assertIn("共 2 个用户发言", out_text(r))

    @unittest.skipUnless(support.zstd_backend(), "本机没有 zstd 实现")
    def test_本机活日志的zstd也能读(self):
        # 这才是 dsh 里的自然用法:~/.dsh/sessions/<slug>/<id>/session.v4.jsonl.zstd
        support.write_dsh_zstd(self.home / "sessions" / "proj" / self.sid, 标准会话())

        r = run_cli("--outline", "--source", "dsh", "--newest", dsh_home=self.home)

        self.assertEqual(r.returncode, 0, err_text(r))
        self.assertIn("共 2 个用户发言", out_text(r))

    def test_坏掉的zstd报错_不装成没内容(self):
        d = self.home / "sessions" / "proj" / self.sid
        d.mkdir(parents=True)
        (d / "session.v4.jsonl.zstd").write_bytes(b"this is not zstd at all")

        r = run_cli("--stdout", "--source", "dsh", "-s", self.sid, dsh_home=self.home)

        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(err_text(r).strip(), "读不了也要说一句人话,不能静默")
        self.assertEqual(out_text(r), "")


class 文档口径(_DshHome):
    """SKILL.md 是模型实际加载的那一份。

    它写的 header 字面量和代码输出对不上,等于承诺失效——而这类漂移没有任何
    东西会报错:代码照常跑、测试照常绿。所以把两边钉在一起。
    """

    # 契约只有这一处:代码要输出它,SKILL.md / README.md 要写着它。
    # 数字(忽略几条)不进契约,文档里写不完,只钉前缀。
    FRAGMENTS = (
        "- 来源:dsh 会话导出(session.v4.jsonl)",
        "- 忽略:非用户消息",
    )

    def test_文档和代码说的是同一句话(self):
        r = self.跑("--stdout", "--source", "dsh", "-s", self.sid)
        self.assertEqual(r.returncode, 0, err_text(r))
        out = out_text(r)

        for frag in self.FRAGMENTS:
            with self.subTest(frag=frag):
                self.assertIn(frag, out, "代码已经不输出这句话了,文档却还写着")
                for doc in ("SKILL.md", "README.md"):
                    text = (support.REPO / doc).read_text(encoding="utf-8")
                    self.assertIn(frag, text, f"{doc} 里的字面量漂了")


class 普查(_DshHome):
    """接新来源时先看这一眼。它只摆事实,不做取舍。"""

    def 跑普查(self, *extra):
        import subprocess
        import sys
        return subprocess.run(
            [sys.executable, str(support.SCRIPTS / "corpus_check.py"),
             "--source", "dsh", "--census", "--home", str(self.home / "sessions"), *extra],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)

    def test_列出工具名和会话形态(self):
        support.write_dsh_layout(self.home, 标准会话())
        support.write_dsh_layout(self.home, 直接模式会话(), session_id="session-22222222-2222-3333-4444-555555555555",
                                 project="proj2")

        r = self.跑普查()

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tool/call", r.stdout)
        self.assertIn("tool/ptc-dispatch", r.stdout)
        self.assertIn("PTC", r.stdout)
        self.assertIn("直接", r.stdout)
        # 真工具的名字要能看见,容器的名字不该混进来
        self.assertIn("bash", r.stdout)
        self.assertIn("read", r.stdout)


if __name__ == "__main__":
    unittest.main()
