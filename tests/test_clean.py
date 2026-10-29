"""剥噪音 / 删真信息的边界。

这个文件里的每一条都对应一次真实踩坑:**剥掉了平台噪音,顺手把真信息也带走了。**
照常输出、退出码 0,只有人肉比对原文才看得出来——所以必须钉成用例。
"""

import unittest

import support  # noqa: F401  设好 WORKBUDDY_HOME 并挂上 scripts/
import fork


class 剥图片标记(unittest.TestCase):
    def test_中文紧贴标记时不吃正文(self):
        # 中文正文常紧贴标记。按「到空白为止」抓文件名,整句话都会被当文件名删掉。
        out = fork.clean_user("@image#1:a.png，这样改对不对")
        self.assertIn("这样改对不对", out)
        self.assertIn("[图片 a.png]", out)

    def test_带数字编号的图片标记(self):
        out = fork.clean_user("@image#3:b.jpg 这个呢")
        self.assertIn("[图片 b.jpg]", out)
        self.assertIn("这个呢", out)


class 剥技能与文件标记(unittest.TestCase):
    def test_技能名渲染成可见占位而不是删掉(self):
        # 删了之后新对话看不出这句话是 /code-review 触发的,而仓库里查不回来。
        out = fork.clean_user('<user_query>@skill:"/x/code-review/SKILL.md" 重写的代码没 review</user_query>')
        self.assertIn("[技能 code-review]", out)
        self.assertIn("重写的代码没 review", out)

    def test_技能名取目录名不是SKILL文件(self):
        self.assertEqual(fork.marker_name(r"C:\a\writing-for-agents\SKILL.md"),
                         "writing-for-agents")

    def test_文件标记取文件名(self):
        out = fork.clean_user('<user_query>@file:"/x/y.py" 看这个</user_query>')
        self.assertIn("[文件 y.py]", out)
        self.assertIn("看这个", out)


class 长引用标记(unittest.TestCase):
    def test_跟着引文块时删掉预览只留正文(self):
        out = fork.clean_user(
            '<user_query>看 @long-text:"预览" <long_text_quote>完整正文在这里</long_text_quote> 对不对</user_query>')
        self.assertIn("完整正文在这里", out)
        self.assertNotIn("@long-text", out)
        # 预览是正文前 20 字的重复,留着只会让同一个意思出现两次
        self.assertNotIn("预览", out)

    def test_没有引文块时预览是唯一线索要留着(self):
        out = fork.clean_user('<user_query>看 @long-text:"预览" 这个改法对不对</user_query>')
        self.assertIn("[长引用 「预览」]", out)
        self.assertIn("这个改法对不对", out)

    def test_正文里讨论标记本身时不吃成空反引号(self):
        # 用户把标记写在反引号里讨论它。原来会变成一对空反引号,原话消失。
        out = fork.clean_user('<user_query>比如 `@long-text:"预览"` 会被删掉</user_query>')
        self.assertNotIn("``", out)
        self.assertIn("预览", out)
        self.assertIn("会被删掉", out)


class 碎片判据(unittest.TestCase):
    """整段只剩协议碎片才清掉。判据是「整段被一个已知标签包住」。"""

    def test_整段只有一个已知标签算碎片(self):
        self.assertTrue(fork.protocol_only("<system-reminder>x</system-reminder>"))

    def test_同一标签出现两次不算碎片(self):
        # 中间夹着正文,说明有人在说话
        self.assertFalse(fork.protocol_only(
            "<system-reminder>a</system-reminder>正文<system-reminder>b</system-reminder>"))

    def test_用户自己贴的未知标签不算碎片(self):
        self.assertFalse(fork.protocol_only("<div>hello</div>"))

    def test_query标签配不齐不算碎片(self):
        # 配不齐说明被截断了,里面是用户原话
        self.assertFalse(fork.protocol_only("<user_query>说到一半"))

    def test_体外碎片会被清掉(self):
        self.assertEqual(fork.clean_user("<system-reminder>sys</system-reminder>"), "")

    def test_体内有正文时正文保住(self):
        out = fork.clean_user("<user_query>帮我解释 <system-reminder>示例</system-reminder> 是什么</user_query>")
        self.assertIn("帮我解释", out)
        self.assertIn("是什么", out)

    def test_体内只剩一个已知标签时不算用户发言(self):
        # 一条只装着平台注入的 <user_query> 不是用户说的话。原来体内不过碎片判据,
        # 它会变成一条凭空多出来的发言,把后面所有编号顶掉,--from N 从此指错地方。
        self.assertEqual(
            fork.clean_user("<user_query><system-reminder>sys</system-reminder></user_query>"),
            "")

    def test_体内只剩标签但标签出现两次时仍是用户发言(self):
        # 同一个标签出现两次,说明中间夹着正文——那是用户在讨论这个标签。
        out = fork.clean_user(
            "<user_query><system-reminder>a</system-reminder>这里<system-reminder>b</system-reminder></user_query>")
        self.assertIn("这里", out)


class 排版(unittest.TestCase):
    def test_体内缩进原样保留(self):
        # 缩进被压平的话,代码块对齐和 Node 栈的层级就没了
        body = "看这段:\n\n```python\n    def f():\n        return 1\n```"
        out = fork.clean_user(f"<user_query>{body}</user_query>")
        self.assertIn("    def f():", out)
        self.assertIn("        return 1", out)

    def test_摘要块里的假原话不算这一轮说的话(self):
        # <cb_summary> 里嵌着上一轮的 <user_query>,那是历史重放。
        # 必须先整块删再拆体内外,否则上一轮的话会被重复计一次。
        text = ('<cb_summary><user_query>上一轮重复的话</user_query></cb_summary>'
                '<user_query>这一轮真话</user_query>')
        out = fork.clean_user(text)
        self.assertIn("这一轮真话", out)
        self.assertNotIn("上一轮重复的话", out)


if __name__ == "__main__":
    unittest.main()
