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
    """2 条真发言 + 6 种注入 + 一次 run_code 里的三个真工具。"""
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
                      call=("c1", "run_code", {"code": "await tools.bash({...})"})),
        dsh_dispatch(10, "bash", {"command": "ls -la"}, text="total 0"),
        dsh_result(11, "total 0"),
        dsh_user(12, "顺便把 pcb 也看了"),
        dsh_dispatch(13, "read", {"file_path": "README.md"}, text="# demo"),
        dsh_dispatch(14, "edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"},
                     meta={"path": "a.py"}),
        dsh_assistant(15, text="改完了。"),
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
        # run_code 只是 PTC 的容器,露出来等于每轮都写一次「跑了段代码」
        self.assertNotIn("run_code", out)
        # reasoning 是模型的内部独白,和「用户说过什么」无关
        self.assertNotIn("内部思考", out)
        self.assertIn("我先看看目录。", out)

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
        self.assertEqual(stats["整条丢掉"], 0)

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

    def test_剥完只剩标签的发言会被抓出来(self):
        self.跑("--save", str(self.base))
        records = 标准会话() + [
            dsh_user(16, "<system-reminder></system-reminder>"),
            dsh_assistant(17, text="嗯"),
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


if __name__ == "__main__":
    unittest.main()
