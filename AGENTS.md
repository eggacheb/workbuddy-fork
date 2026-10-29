# AGENTS.md

给 AI agent 看的仓库说明。给人类看的那份是 `README.md`。

## 这个仓库是什么

给 **WorkBuddy 桌面端**补一个「上下文分叉」技能:把本地会话日志读出来、剥掉平台注入、压成一份能整段粘进新对话的上下文包。

**不做真 fork。** 桌面端闭源,碰不到会话存储;这里只解决「别丢背景」。

| 文件 | 是什么 |
|---|---|
| `scripts/fork.py` | 工具本体。单文件,无第三方依赖 |
| `SKILL.md` | 技能说明。WorkBuddy 加载的是这一份 |
| `README.md` | 用户文档、安装、命令表 |
| `tests/` | 表征测试,`unittest`,不引第三方库 |

## 跑起来

```bash
python scripts/fork.py --outline                # 列当前会话的用户发言,挑分叉点
python scripts/fork.py --compact --package      # 导出 + 出包骨架
python -m unittest discover -s tests -v         # 全部测试
python scripts/corpus_check.py                  # 全库回归:发言数 / 占位数 / 整条丢掉数
python scripts/corpus_check.py --equiv HEAD~1   # 和旧版逐字节比输出,证明改动等价
```

单测只覆盖想得到的边界;**`corpus_check` 跑的是这台机器上真实发生过的全部会话**。改完剥离规则先跑它,再看数字有没有掉。它也可以 `--save` 存基线、`--compare` 再比,任何一项变少都算回归。

会话日志在 `~/.workbuddy/projects/<项目slug>/<会话Id>.jsonl`,目录可用 `WORKBUDDY_HOME` 覆盖。测试全部靠这个变量指到临时 fixture,**绝不读真实会话**——否则结果取决于这台机器上聊过什么。

`pre-commit` 在提交前跑全量测试,不过就拦下(`scripts/sync-skill.sh --install-hooks` 装的)。CI 跑同一份,runner 用 `windows-latest`:中文管道编码那条用例只有在 Windows 上才有意义。

改完 clone 里的代码要同步到 WorkBuddy 实际加载的 `~/.workbuddy/skills/fork/`:`scripts/sync-skill.sh`。`post-commit` / `post-merge` 钩子会自动跑它(装钩子:`scripts/sync-skill.sh --install-hooks`)。**不同步就是跑旧代码,而且没有任何提示。**

## 改动纪律

这几条都是踩出来的,不是风格偏好。

- **静默错数据比崩溃严重。** 这个脚本历史上每一个 P0 都是「照常打印已导出,退出码 0,但内容是错的」。崩溃会被人发现,错数据不会。新增任何剥离规则,先问:它会不会顺手删掉真信息?
- **判据要机械,而且按位置判。** 「剥噪音」和「删真信息」的边界必须落在一个可验证的字符串条件上,条件还要**逐个标记判、不按整段判**:「从这个 `@long-text` 到下一个标记之间有没有 `<long_text_quote>`」,而不是「这一段里有没有」。按整段判,一个真引用会把同一句里讨论用的标记一起带走。
- **`<user_query>` 体内是用户原话。** 只摘附件、不碰排版。缩进和空行必须原样留着,否则代码块对齐和 Node 栈的层级会被压平。压空行这条加过又拆了,理由见 `docs/adr/0003-体内不压空行.md`。
- **标记名的字符集只收 ASCII。** 中文正文常紧贴标记(`@image#1:a.png，这样改对不对`),按「到空白为止」去抓会把整句话当文件名删掉。宁可丢一个中文文件名,也不能吃用户原话。
- **用户点过的东西不能删。** `@skill:` / `@"…"` / `@long-text:` 记的是「这句话是怎么来的」,新对话在仓库里查不回来,所以渲染成 `[技能 X]` / `[文件 目录/y.py]` / `[长引用 「X」]` 而不是清掉——代码块里也一样,理由见 `docs/adr/0001-标记在代码块里照常渲染.md`。`@"…"` 只认平台在 `<user_references>` 里列过的路径,见 `docs/adr/0002-只认平台盖章的文件引用.md`。
- **会话定位不猜。** ID 对不上就报错并列出最近的会话,`--newest` 才显式降级。悄悄换成别的会话,用户从输出里看不出来。
- **会话 ID 以日志里的 `sessionId` 为准。** 文件名可以改,接续会话的文件名和 ID 本来就不一致,所以 header 和包里的「来源会话」都不抄文件名。取不到才退回 `path.stem`,而退回必须在交付物里标出来——「日志说的」和「我们猜的」在纸面上不能长得一样。
- **动一大段之前先补表征测试。** 把要改的行为先钉成用例再动手,这只为「改的时候立刻知道撞到哪」;提交前那道测试闸是另一回事,别拿它当理由跳过这一步。

## Agent skills

### Issue tracker

Issue 走 GitHub Issues(本仓库 remote 指向 `eggacheb/workbuddy-fork`),用 `gh` CLI 操作。见 `docs/agents/issue-tracker.md`。

### Triage labels

五个规范分诊角色,标签字符串与角色同名。见 `docs/agents/triage-labels.md`。

### Domain docs

单上下文布局:根目录 `GLOSSARY.md` + `docs/adr/`。目前都还没建,按需懒创建。见 `docs/agents/domain.md`。
