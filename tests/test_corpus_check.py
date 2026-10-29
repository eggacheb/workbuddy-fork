"""全库回归脚本自己的用例。

它扫的是真实语料,但这里的用例全部跑在临时目录上——不然结果取决于
这台机器上聊过什么。
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  设好 WORKBUDDY_HOME 并挂上 scripts/
import corpus_check


def quiet(fn, *args) -> int:
    """在用例里跑一个会打印的函数,stdout 和 stderr 一起接走。

    compare 打对照表、equiv 打「取不到 …」,不接走就混进测试输出,看着像用例
    自己崩了。两个流一起接:真跑命令的人要看见的提示,这里一句也不删。
    """
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args)


class 统计(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self._tmp.name) / "projects"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, sid, records):
        support.write_session(self.projects.parent, records, sid)

    def test_各项计数(self):
        self._write("sess0001", [
            support.user_turn(1, "第一句真话"),
            support.user_turn(2, "第二句真话"),
            support.user_turn(3, "<user_query><system-reminder>注入</system-reminder></user_query>"),
        ])
        self._write("sess0002", [
            # 标记只有落在 <user_query> 体内才换成占位;体外那一区当平台注入删掉
            support.user_turn(1, '<user_query>看 @long-text:"预览" 这个改法对不对</user_query>'),
        ])

        stats = corpus_check.collect(self.projects)
        self.assertEqual(stats["会话数"], 2)
        self.assertEqual(stats["原始 user 消息"], 4)
        self.assertEqual(stats["计入发言"], 3)      # 那条纯注入的不算
        self.assertEqual(stats["整条丢掉"], 1)
        self.assertEqual(stats["长引用占位"], 1)
        self.assertEqual(stats["幽灵发言"], 0)

    def test_空目录不炸(self):
        self.projects.mkdir(parents=True)
        stats = corpus_check.collect(self.projects)
        self.assertEqual(stats["会话数"], 0)
        self.assertEqual(stats["计入发言"], 0)

    def test_体外残留只数没被认出来的那些(self):
        # 这一项是「平台又往 query 外面塞了新东西」的警报,所以计数口径必须是
        # 「剥完还剩字」:重复的 [图片] 占位是脚本自己渲染的,不算。
        self._write("sess0003", [
            support.user_turn(1, '<user_query>真话</user_query>\n平台又塞了一段不在标签里的'),
            support.user_turn(2, '<user_query>带图</user_query>\n[图片]\n'
                                 '<image_local_path>C:/x/a.png</image_local_path>'),
            support.user_turn(3, "没有 query 的原话,体外就是它自己"),
        ])
        stats = corpus_check.collect(self.projects)
        self.assertEqual(stats["体外残留"], 1)


class 和基线比(unittest.TestCase):
    def test_持平就算过(self):
        base = {"会话数": 1, "原始 user 消息": 10, "计入发言": 7, "整条丢掉": 3,
                "长引用占位": 2, "幽灵发言": 0}
        self.assertEqual(quiet(corpus_check.compare, dict(base), base), 0)

    def test_变多不算回归(self):
        base = {"计入发言": 7, "长引用占位": 2, "幽灵发言": 0}
        now = {"计入发言": 9, "长引用占位": 2, "幽灵发言": 0}
        self.assertEqual(quiet(corpus_check.compare, now, base), 0)

    def test_变少就是回归(self):
        # 例如 2142 -> 2141:新规则把一条真话吃掉了
        base = {"计入发言": 7, "长引用占位": 2, "幽灵发言": 0}
        now = {"计入发言": 6, "长引用占位": 2, "幽灵发言": 0}
        self.assertEqual(quiet(corpus_check.compare, now, base), 1)

    def test_占位变少也是回归(self):
        base = {"计入发言": 7, "长引用占位": 2, "幽灵发言": 0}
        now = {"计入发言": 7, "长引用占位": 1, "幽灵发言": 0}
        self.assertEqual(quiet(corpus_check.compare, now, base), 1)

    def test_出现幽灵发言直接判失败(self):
        base = {"计入发言": 7, "幽灵发言": 0}
        now = {"计入发言": 8, "幽灵发言": 1}
        self.assertEqual(quiet(corpus_check.compare, now, base), 1)

    def test_体外残留变多就是回归(self):
        # 这一项方向相反:「计入发言」发现不了真话被挪到体外(体内照样有字),
        # 只能靠它。多出一条,就说明平台往 query 外面塞了没认出来的东西。
        base = {"计入发言": 7, "体外残留": 2}
        now = {"计入发言": 7, "体外残留": 3}
        self.assertEqual(quiet(corpus_check.compare, now, base), 1)

    def test_体外残留变少不报警(self):
        # 少下去说明规则认出了更多注入,是好事——该去改基线,不该报警。
        base = {"计入发言": 7, "体外残留": 2}
        now = {"计入发言": 7, "体外残留": 0}
        self.assertEqual(quiet(corpus_check.compare, now, base), 0)


class 对齐(unittest.TestCase):
    def test_中文按两格算(self):
        self.assertEqual(corpus_check.display_width("会话数"), 6)
        self.assertEqual(corpus_check.display_width("abc"), 3)

    def test_pad到同一显示宽度(self):
        self.assertEqual(corpus_check.display_width(corpus_check.pad("会话数", 10)), 10)


class 取旧版(unittest.TestCase):
    def test_取不到的提交报错退出2(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = quiet(corpus_check.equiv, "没有这个提交", Path(tmp), 1)
        self.assertEqual(code, 2)

    def test_取不到的原因仍然写在stderr(self):
        # 接走噪音不等于把提示删掉:真跑 --equiv 的人得看见为什么没比成。
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(err):
            corpus_check.equiv("没有这个提交", Path(tmp), 1)
        self.assertIn("取不到", err.getvalue())


if __name__ == "__main__":
    unittest.main()
