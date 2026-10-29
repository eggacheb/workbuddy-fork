"""剥噪音 / 删真信息的边界。

这个文件里的每一条都对应一次真实踩坑:**剥掉了平台噪音,顺手把真信息也带走了。**
照常输出、退出码 0,只有人肉比对原文才看得出来——所以必须钉成用例。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_文件标记留最后两段目录(self):
        # 只留 y.py 的话,新对话不知道是哪个 y.py。图片那边留的是完整路径,
        # 这里留两段是同一个方向:够认出是哪个文件,又不像全路径那样占篇幅。
        out = fork.clean_user('<user_query>@file:"C:/code/proj/src/y.py" 看这个</user_query>')
        self.assertIn("[文件 src/y.py]", out)
        self.assertIn("看这个", out)

    def test_文件标记没有目录时原样(self):
        out = fork.clean_user('<user_query>@file:"y.py" 看这个</user_query>')
        self.assertIn("[文件 y.py]", out)

    def test_文件标记的反斜杠路径也认(self):
        out = fork.clean_user(r'<user_query>@file:"D:\code\proj\src\y.py" 看这个</user_query>')
        self.assertIn("[文件 src/y.py]", out)


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

    def test_同一体内既有引文块又有讨论时逐个标记判断(self):
        # 判据不能是「这个体内有没有引文块」——那样一个真的引用会把同一句里
        # 讨论用的标记一起带走。得看每个标记自己后面跟没跟引文块。
        out = fork.clean_user(
            '<user_query>看 @long-text:"预览" <long_text_quote>完整正文</long_text_quote> '
            '但 `@long-text:"例子"` 这种也会被删</user_query>')
        self.assertIn("完整正文", out)
        self.assertNotIn("预览", out)              # 跟着引文块,预览是重复
        self.assertIn("[长引用 「例子」]", out)     # 没跟引文块,是唯一线索
        self.assertNotIn("``", out)


class 引号形式的文件引用(unittest.TestCase):
    """平台真实的文件引用是 `@"…"`,不是 `@file:"…"`。

    体内那句 `@"…"` 和 C# 的 verbatim string 长得一模一样,所以只能靠
    「这条消息的 <user_references> 里列过这个路径」来认——那是平台盖的章。
    """

    def _msg(self, body, refs):
        lines = "".join(f'"{p}"\n' for p in refs)
        block = ('<system-reminder data-role="additional-data">\n<user_references>\n'
                 + lines + "</user_references>\n</system-reminder>\n")
        return f"{block}<user_query>{body}</user_query>"

    def test_平台列过的路径换成文件占位(self):
        out = fork.clean_user(self._msg(
            r'@"scripts\\fork.py" 我发一个文件引用，你测一下看看吧',
            [r"D:\\code\\workbuddy-fork\\scripts\\fork.py"]))
        self.assertIn("[文件 scripts/fork.py]", out)
        self.assertIn("我发一个文件引用", out)
        # 双反斜杠要收掉,否则看着像故障
        self.assertNotIn("\\\\", out)

    def test_没被平台确认的引号原样不动(self):
        # C# 的 @"verbatim string" 也长这样,不能当文件引用
        out = fork.clean_user(r'<user_query>var s = @"C:\\temp\\x"; 这行什么意思</user_query>')
        self.assertIn(r'@"C:\\temp\\x"', out)
        self.assertNotIn("[文件", out)

    def test_平台列的是别的文件时不误伤(self):
        out = fork.clean_user(self._msg(r'@"other.py" 看这个',
                                        [r"D:\\code\\proj\\real.py"]))
        self.assertIn('@"other.py"', out)
        self.assertNotIn("[文件", out)

    def test_引用块本身不留在正文里(self):
        out = fork.clean_user(self._msg(r'@"a\\b.py" 看看',
                                        [r"D:\\code\\proj\\a\\b.py"]))
        self.assertNotIn("<user_references>", out)
        self.assertNotIn("Note: These references", out)


class 包的识别(unittest.TestCase):
    """按开头认包,但导出文件的第一行固定是 `# 会话记录:`——先排除它。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_读不出来的包仍认作包(self):
        # 被占用、没权限时读不到开头。判成「不是包」正好会让正文盖掉
        # 用户手填的那一份,而「已导出」还会照常打印。
        p = self.dir / "abc-package.md"
        p.write_text("x", encoding="utf-8")
        with mock.patch.object(Path, "read_text", side_effect=OSError("被占用")):
            self.assertTrue(fork.looks_like_package(p))

    def test_开头是会话记录的一律不是包(self):
        p = self.dir / "x-full.md"
        p.write_text("# 会话记录:某条线\n\n## 之前那条线到哪了\n人话\n", encoding="utf-8")
        self.assertFalse(fork.looks_like_package(p))


class 临时文件判据(unittest.TestCase):
    """这个仓库自己就叫 workbuddy-fork,按名字判会把它的文件全滤掉。"""

    def test_仓库里改过的文件不算临时文件(self):
        self.assertFalse(fork.is_temp_artifact(r"D:\code\workbuddy-fork\scripts\fork.py"))
        self.assertFalse(fork.is_temp_artifact(r"D:\code\workbuddy-fork\AGENTS.md"))

    def test_导出目录里的不算改过的文件(self):
        self.assertTrue(fork.is_temp_artifact(str(fork.TEMP_ROOT / "abc-from1.md")))

    def test_上次留下的包不算改过的文件(self):
        self.assertTrue(fork.is_temp_artifact(r"C:\somewhere\abc-package.md"))

    def test_改过的文件那一节列得出仓库里的改动(self):
        target = r"D:\code\workbuddy-fork\scripts\fork.py"
        turns = [{
            "user": "x", "blocks": [], "errors": [], "ts": 1,
            "calls": [
                {"name": "Edit", "args": {"file_path": target}},
                {"name": "Edit", "args": {"file_path": target}},
                {"name": "Write", "args": {"file_path": str(fork.TEMP_ROOT / "p.md")}},
            ],
        }]
        facts = fork.collect_facts(turns)
        self.assertEqual(facts["edited"], [(target, 2)])


class 体内外切分(unittest.TestCase):
    def test_注入区里的字面标签不吃掉原话(self):
        # AGENTS.md 被整篇注进 user-context,里面就写着「`<user_query>` 体内是
        # 用户原话」这条纪律。非贪婪正则从那个字面标签开始匹配到真闭合标签,
        # 前半段注入就被当成体内、真原话被当成体外——照常输出,看不出来。
        text = (
            '<system-reminder data-role="user-context">\n'
            '## 改动纪律\n'
            '- **`<user_query>` 体内是用户原话。** 只摘附件、不碰排版。\n'
            '</system-reminder>\n'
            '<user_query>帮我看下这个正则</user_query>'
        )
        self.assertEqual(fork.clean_user(text), "帮我看下这个正则")

    def test_正文里反引号包住的标签不算发言起点(self):
        # 用户在体内讨论这个标签本身,把它写进行内代码。非贪婪正则从反引号
        # 里那个开始匹配,把「…」当成一条发言——凭空多一条,后面所有编号错位,
        # --from N 从此指到别的地方。
        text = (
            '<user_query>真实用户输入优先取 `<user_query>…</user_query>` 里的内容。'
            '帮我看下这个正则</user_query>'
        )
        self.assertEqual(
            fork.clean_user(text),
            "真实用户输入优先取 `<user_query>…</user_query>` 里的内容。帮我看下这个正则",
        )

    def test_正文里讨论的图片标记不当成真附件(self):
        # 用户在正文里举例:`@image#1:a.png，这样改对不对`。体内没有
        # <image_local_path>,所以那不是附件。原来的处理把它当真图:原话被删成
        # 空反引号,消息开头还凭空多出一个 [图片 a.png]。
        text = ('<user_query>中文常紧贴标记（`@image#1:a.png，这样改对不对`），'
                '按空白抓会吃掉原话</user_query>')
        out = fork.clean_user(text)
        self.assertIn("@image#1:a.png", out)
        self.assertNotIn("[图片", out)

    def test_没有路径的图片标记仍然按附件渲染(self):
        # 平台偶尔漏记 <image_local_path>,但用户确实贴了图。不能因为查不到
        # 路径就当成正文里的讨论,把 @image#1:xxx.png 原样丢给新对话。
        text = '<user_query>@image#1:Clipboard_Screenshot.png 是这个的问题吗</user_query>'
        out = fork.clean_user(text)
        self.assertIn("[图片 Clipboard_Screenshot.png]", out)
        self.assertIn("是这个的问题吗", out)
        self.assertNotIn("@image", out)


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

    def test_连续空行原样保留(self):
        # 用户贴进来的终端输出、markdown 原文里,空行就是内容。压空行不压缩进,
        # 所以压不平代码块——但它仍然在改用户的原话,而这一类改动不会报错。
        body = "先说结论:\n\n\n\n```python\nx = 1\n```"
        out = fork.clean_user(f"<user_query>{body}</user_query>")
        self.assertIn("先说结论:\n\n\n\n```python", out)

    def test_代码块里的空行不被压缩(self):
        body = "```\n调一下这里\n\n\n\n再看这里\n```"
        out = fork.clean_user(f"<user_query>{body}</user_query>")
        self.assertIn("调一下这里\n\n\n\n再看这里", out)

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
