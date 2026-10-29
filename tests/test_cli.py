"""CLI 边界的表征测试:区间、覆盖、定位、编码。

这一层的每一条都曾经**照常打印「已导出」、退出码 0,但结果是错的**。
所以断言不只看退出码,还要看内容到底对不对。
"""

import tempfile
import unittest
from pathlib import Path

import support
from support import run_cli, out_text, err_text


class 区间(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _five(self):
        return support.simple_session(self.home, n=5)

    def test_from和last一起给是取头部不是取尾部(self):
        # 判据必须是「有没有给过 --from」而不是「start 是不是 1」,
        # 否则 --from 1 --last 2 会被当成「最后 2 句」,分叉点悄悄跑到尾巴上。
        sid = self._five()
        r = run_cli("--from", "1", "--last", "2", "--stdout", "-s", sid, home=self.home)
        self.assertEqual(r.returncode, 0, err_text(r))
        out = out_text(r)
        self.assertIn("[1] 用户", out)
        self.assertIn("[2] 用户", out)
        self.assertNotIn("[3] 用户", out)
        self.assertIn("第 1 ~ 2 个用户发言", out)

    def test_只给last是取最后N句(self):
        sid = self._five()
        r = run_cli("--last", "2", "--stdout", "-s", sid, home=self.home)
        self.assertEqual(r.returncode, 0, err_text(r))
        out = out_text(r)
        self.assertIn("[4] 用户", out)
        self.assertIn("[5] 用户", out)
        self.assertNotIn("[3] 用户", out)

    def test_from单独用取到结尾(self):
        sid = self._five()
        r = run_cli("--from", "4", "--stdout", "-s", sid, home=self.home)
        out = out_text(r)
        self.assertIn("[4] 用户", out)
        self.assertIn("[5] 用户", out)
        self.assertNotIn("[3] 用户", out)

    def test_from超出总数时明确报错(self):
        sid = self._five()
        r = run_cli("--from", "99", "--stdout", "-s", sid, home=self.home)
        self.assertEqual(r.returncode, 2)
        self.assertIn("没有内容", err_text(r))


class 上下文包不被正文覆盖(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.out = self.home / "out"
        self.out.mkdir()
        self.sid = support.simple_session(self.home, n=3)

    def tearDown(self):
        self._tmp.cleanup()

    def test_out正好指向包时拒绝并保留原内容(self):
        # 包是用户手填的唯一副本。覆盖掉不会有任何提示,而「已导出」照常打印。
        pkg = self.out / f"{self.sid}-package.md"
        pkg.write_text("我手填的包内容", encoding="utf-8")
        r = run_cli("--out", str(pkg), "-s", self.sid, home=self.home)
        self.assertEqual(r.returncode, 2)
        self.assertIn("上下文包本身", err_text(r))
        self.assertEqual(pkg.read_text(encoding="utf-8"), "我手填的包内容")

    def test_out指向改了名字的包也拒绝(self):
        # 用户可能把包另存成别的名字,那是他手填的唯一副本。
        # 按名字认不出,就按开头认。
        renamed = self.out / "我的笔记.md"
        renamed.write_text("## 之前那条线到哪了\n手填的东西", encoding="utf-8")
        r = run_cli("--out", str(renamed), "-s", self.sid, home=self.home)
        self.assertEqual(r.returncode, 2)
        self.assertIn("上下文包本身", err_text(r))
        self.assertIn("手填的东西", renamed.read_text(encoding="utf-8"))

    def test_已有包默认不覆盖(self):
        r = run_cli("--package", "--out", str(self.out / "原文.md"), "-s", self.sid, home=self.home)
        self.assertEqual(r.returncode, 0, err_text(r))
        pkg = self.out / f"{self.sid}-package.md"
        self.assertTrue(pkg.is_file())
        pkg.write_text("已经填好的包", encoding="utf-8")

        r2 = run_cli("--package", "--out", str(self.out / "原文.md"), "-s", self.sid, home=self.home)
        self.assertEqual(r2.returncode, 0, err_text(r2))
        self.assertIn("没覆盖", out_text(r2))
        self.assertEqual(pkg.read_text(encoding="utf-8"), "已经填好的包")

    def test_force才覆盖(self):
        r = run_cli("--package", "--out", str(self.out / "原文.md"), "-s", self.sid, home=self.home)
        self.assertEqual(r.returncode, 0, err_text(r))
        pkg = self.out / f"{self.sid}-package.md"
        pkg.write_text("旧包", encoding="utf-8")

        r2 = run_cli("--package", "--force", "--out", str(self.out / "原文.md"),
                     "-s", self.sid, home=self.home)
        self.assertEqual(r2.returncode, 0, err_text(r2))
        self.assertNotEqual(pkg.read_text(encoding="utf-8"), "旧包")


class 会话定位(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_日志不落盘时报错并列出最近会话(self):
        # 后台/子代理会话的日志常常还没落盘。这时绝不能悄悄换成别的会话,
        # 否则用户拿着另一条线的原文去填包,从输出里看不出来。
        sid = support.simple_session(self.home, n=2)
        r = run_cli("--stdout", home=self.home, session="ffffffffffffffff")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(out_text(r), "")
        err = err_text(r)
        self.assertIn("不在磁盘上", err)
        self.assertIn(sid, err)
        self.assertIn("--newest", err)

    def test_newest才显式降级(self):
        sid = support.simple_session(self.home, n=2)
        r = run_cli("--stdout", "--newest", home=self.home, session="ffffffffffffffff")
        self.assertEqual(r.returncode, 0, err_text(r))
        self.assertIn("第1个问题", out_text(r))
        self.assertIn("--newest", err_text(r))

    def test_会话ID带路径分隔符被拒(self):
        # 不禁的话 --session 就成了任意文件读取
        support.simple_session(self.home, n=1)
        r = run_cli("--stdout", "-s", "../../etc/passwd", home=self.home)
        self.assertEqual(r.returncode, 2)
        self.assertIn("只能含字母", err_text(r))

    def test_文件名与会话ID不一致时按文件里的sessionId反查(self):
        # 接续会话/子代理会话会这样:文件名对不上,ID 对得上。
        real_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        recs = [
            {"type": "session-meta", "timestamp": 0, "sessionId": real_id},
            support.ai_title("反查命中的会话"),
            support.user_turn(1, "反查测试"),
            support.assistant_turn(1, "好"),
        ]
        support.write_session(self.home, recs, session_id="other-file-name")
        r = run_cli("--stdout", "-s", real_id, home=self.home)
        self.assertEqual(r.returncode, 0, err_text(r))
        self.assertIn("反查测试", out_text(r))

    def test_list列得出会话(self):
        sid = support.simple_session(self.home, n=1)
        r = run_cli("--list", home=self.home)
        self.assertEqual(r.returncode, 0, err_text(r))
        self.assertIn(sid, out_text(r))


class 编码(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_中文和emoji走管道不崩(self):
        # 中文 Windows 上只要重定向或走管道就可能是 gbk 输出流,
        # 而模型跑命令时 stdout 永远是被接走的。乱码能忍,崩掉不能。
        recs = [support.ai_title("编码测试"),
                support.user_turn(1, "中文问题 🦄 测试"),
                support.assistant_turn(1, "好的")]
        support.write_session(self.home, recs, "sess0001")
        r = run_cli("--stdout", "-s", "sess0001", home=self.home,
                    extra_env={"PYTHONIOENCODING": "gbk"})
        self.assertEqual(r.returncode, 0, err_text(r))
        out = out_text(r)
        self.assertIn("中文问题", out)
        self.assertIn("🦄", out)

    def test_坏行不影响整份解析(self):
        d = self.home / "projects" / "proj"
        d.mkdir(parents=True)
        import json
        lines = ["{ 这不是 json",
                 json.dumps(support.ai_title("坏行测试"), ensure_ascii=False),
                 json.dumps(support.user_turn(1, "还能读到这句"), ensure_ascii=False),
                 ""]
        (d / "sess0001.jsonl").write_text("\n".join(lines), encoding="utf-8")
        r = run_cli("--stdout", "-s", "sess0001", home=self.home)
        self.assertEqual(r.returncode, 0, err_text(r))
        self.assertIn("还能读到这句", out_text(r))


class 参数互斥(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_stdout和package互斥(self):
        r = run_cli("--stdout", "--package", home=self.home)
        self.assertEqual(r.returncode, 2)

    def test_force必须配package(self):
        r = run_cli("--force", home=self.home)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--package", err_text(r))


class 自检(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_check按节报占比且不判合格(self):
        p = self.home / "pkg.md"
        p.write_text(
            "# 包\n\n## 之前那条线到哪了\n目标是什么。\n\n"
            "## 关键事实\n- 一堆仓库查得到的东西\n",
            encoding="utf-8")
        r = run_cli("--check", str(p), home=self.home)
        self.assertEqual(r.returncode, 0, err_text(r))
        out = out_text(r)
        self.assertIn("之前那条线到哪了", out)
        self.assertIn("关键事实", out)
        self.assertIn("%", out)
        # 只看哪节在膨胀,不给达标线
        self.assertNotIn("合格", out)


if __name__ == "__main__":
    unittest.main()
