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


def quiet_compare(now, base) -> int:
    """compare 会往 stdout 打对照表,用例里不该把这堆东西漏到测试输出里。"""
    with contextlib.redirect_stdout(io.StringIO()):
        return corpus_check.compare(now, base)


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


class 和基线比(unittest.TestCase):
    def test_持平就算过(self):
        base = {"会话数": 1, "原始 user 消息": 10, "计入发言": 7, "整条丢掉": 3,
                "长引用占位": 2, "幽灵发言": 0}
        self.assertEqual(quiet_compare(dict(base), base), 0)

    def test_变多不算回归(self):
        base = {"计入发言": 7, "长引用占位": 2, "幽灵发言": 0}
        now = {"计入发言": 9, "长引用占位": 2, "幽灵发言": 0}
        self.assertEqual(quiet_compare(now, base), 0)

    def test_变少就是回归(self):
        # 例如 2142 -> 2141:新规则把一条真话吃掉了
        base = {"计入发言": 7, "长引用占位": 2, "幽灵发言": 0}
        now = {"计入发言": 6, "长引用占位": 2, "幽灵发言": 0}
        self.assertEqual(quiet_compare(now, base), 1)

    def test_占位变少也是回归(self):
        base = {"计入发言": 7, "长引用占位": 2, "幽灵发言": 0}
        now = {"计入发言": 7, "长引用占位": 1, "幽灵发言": 0}
        self.assertEqual(quiet_compare(now, base), 1)

    def test_出现幽灵发言直接判失败(self):
        base = {"计入发言": 7, "幽灵发言": 0}
        now = {"计入发言": 8, "幽灵发言": 1}
        self.assertEqual(quiet_compare(now, base), 1)


class 对齐(unittest.TestCase):
    def test_中文按两格算(self):
        self.assertEqual(corpus_check.display_width("会话数"), 6)
        self.assertEqual(corpus_check.display_width("abc"), 3)

    def test_pad到同一显示宽度(self):
        self.assertEqual(corpus_check.display_width(corpus_check.pad("会话数", 10)), 10)


class 取旧版(unittest.TestCase):
    def test_取不到的提交报错退出2(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = corpus_check.equiv("没有这个提交", Path(tmp), 1)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
