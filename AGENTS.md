# AGENTS.md

给 AI agent 看的仓库说明。给人类看的那份是 `README.md`。

## 这个仓库是什么

给 **WorkBuddy** 和 **dsh(DeepSeek Harness)** 补一个「上下文分叉」技能:把本地会话日志读出来、剥掉平台注入、压成一份能整段粘进新对话的上下文包。

**不做真 fork。** 两个客户端都碰不到会话存储写入口;这里只解决「别丢背景」。两种日志格式差得远,各有一个适配器(`build_turns` / `build_turns_dsh`),产出同一套 turn schema,渲染层只写一份。

| 文件 | 是什么 |
|---|---|
| `scripts/fork.py` | 工具本体。单文件。唯一可选依赖是读 dsh 活日志用的 zstd(Python 3.14+ 自带,否则 `zstandard`);两个都没有时**报错退出**,不会出空包 |
| `SKILL.md` | 技能说明。WorkBuddy / dsh 加载的都是这一份 |
| `README.md` | 用户文档、安装、命令表 |
| `GLOSSARY.md` | 术语。发言 / 轮 / 注入 / 压缩检查点 / 来源 |
| `tests/` | 表征测试,`unittest`,不引第三方库 |

## 跑起来

```bash
python scripts/fork.py --outline                # 列当前会话的用户发言,挑分叉点
python scripts/fork.py --compact --package      # 导出 + 出包骨架
python scripts/fork.py --path x.zip --outline    # dsh 导出包(来源按内容认)
python scripts/fork.py --source dsh --newest -o  # 本机最近的 dsh 会话
python -m unittest discover -s tests -v         # 全部测试
python scripts/corpus_check.py                  # 全库回归:发言数 / 占位数 / 整条丢掉数
python scripts/corpus_check.py --source dsh     # dsh 全库回归:原始 == 计入 / 未认识的 kind
python scripts/corpus_check.py --equiv HEAD~1   # 和旧版逐字节比输出,证明改动等价
```

单测只覆盖想得到的边界;**`corpus_check` 跑的是这台机器上真实发生过的全部会话**。改完剥离规则先跑它,再看数字有没有掉。它也可以 `--save` 存基线、`--compare` 再比,任何一项变差都算回归:吃内容的指标(计入发言、长引用占位)看**变少**,「体外残留」反过来看**变多**——它数的是平台往 `<user_query>` 外面塞、而规则还没认出来的字。

WorkBuddy 的会话日志在 `~/.workbuddy/projects/<项目slug>/<会话Id>.jsonl`,目录可用 `WORKBUDDY_HOME` 覆盖;dsh 的在 `~/.dsh/sessions/<项目slug>/<会话Id>/session.v4.jsonl.zstd`,用 `DSH_HOME` 覆盖。测试全部靠这两个变量指到临时 fixture,**绝不读真实会话**——否则结果取决于这台机器上聊过什么。

dsh 的 `corpus_check` 判的是不变量而不是方向:**计入发言 == 原始发言、幽灵发言 == 0、工具调用 == 原始工具记录、未认识的 `source.kind` == 0、读不了的会话 == 0**(前两条恒等推得出「整条丢掉 == 0」,单独打出来只为看得见)。任何一条破了就非零退出,不带 `--compare` 也查。`--equiv` 只支持 WorkBuddy——老版本 fork.py 根本不认 dsh,没有可比对象。

`pre-commit` 在提交前跑全量测试,不过就拦下(`scripts/sync-skill.sh --install-hooks` 装的)。CI 跑同一份,runner 用 `windows-latest`:中文管道编码那条用例只有在 Windows 上才有意义。

改完 clone 里的代码要同步到实际加载的目录:`scripts/sync-skill.sh` 会同时写 `~/.workbuddy/skills/fork/` 和 `~/.dsh/skills/fork/`(后者不存在时跳过)。`post-commit` / `post-merge` 钩子会自动跑它(装钩子:`scripts/sync-skill.sh --install-hooks`)。**不同步就是跑旧代码,而且没有任何提示。**

## 改动纪律

这几条都是踩出来的,不是风格偏好。

- **静默错数据比崩溃严重。** 这个脚本历史上每一个 P0 都是「照常打印已导出,退出码 0,但内容是错的」。崩溃会被人发现,错数据不会。新增任何剥离规则,先问:它会不会顺手删掉真信息?
- **判据要机械,而且按位置判。** 「剥噪音」和「删真信息」的边界必须落在一个可验证的字符串条件上,条件还要**逐个标记判、不按整段判**:「从这个 `@long-text` 到下一个标记之间有没有 `<long_text_quote>`」,而不是「这一段里有没有」。按整段判,一个真引用会把同一句里讨论用的标记一起带走。
- **被反引号包住的标签不算结构。** 讨论这个工具时必然要把 `<user_query>`、`@image#1:a.png` 写成字面文本,而 `AGENTS.md` 里就写着它们、还会被整篇注进 `user-context`。认了这些假标签,切分就从错误的位置开始:注入被当原话、原话被当注入,还会凭空造出一个挂在不相关消息开头的 `[图片 …]`。注入信封也必须在切分**之前**剥掉,否则信封里的字面标签会先被认走。见 `docs/adr/0004-代码里的标签不算结构.md`。
- **`<user_query>` 体内是用户原话。** 只摘附件、不碰排版。缩进和空行必须原样留着,否则代码块对齐和 Node 栈的层级会被压平。压空行这条加过又拆了,理由见 `docs/adr/0003-体内不压空行.md`。
- **有 `<user_query>` 的发言,体外整段都是注入。** 判据是位置(这一轮有没有 query),不是「像不像注入」——按措辞认,平台换一句就漏(`de650383` 那 420 字就是这么漏的)。没有 query 的老消息体外就是用户原话,一个字不能动。见 `docs/adr/0005-有query时体外整段当注入.md`。
- **标记名的字符集只收 ASCII。** 中文正文常紧贴标记(`@image#1:a.png，这样改对不对`),按「到空白为止」去抓会把整句话当文件名删掉。宁可丢一个中文文件名,也不能吃用户原话。
- **dsh 的工具调用有两种记法,按结构判。** PTC 模式里 `tool/call` 是容器、真工具在 `tool/ptc-dispatch`;直接模式里 `tool/call` 自己就是 `read`/`bash`。分界看 `callId` 在不在某条 `ptc-dispatch` 的 `rootCallId` 里,不是看名字——只认一种约定时,另一种会话导出的工具数是 0,照样退出码 0。见 `docs/adr/0009-工具按结构判不按名字.md`。
- **dsh 的判据是 `source.kind`,不是文本。** 那边注入和原话同结构、正文里也未必有标签,按标签剥等于全程空转。只认 `kind == "user"`,其余按来源挡掉并计数——header 的「忽略 N 条」和 stderr 的「没见过的 kind」是这个判据的唯一可见证据。两种来源的清洗哲学不通用,别把 dsh 的消息喂进 `clean_user`。见 `docs/adr/0007-dsh按source-kind白名单.md`。
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
