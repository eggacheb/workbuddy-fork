# workbuddy-fork

给 **WorkBuddy** 和 **dsh（DeepSeek Harness）** 补一个「上下文分叉」技能。

两个客户端都没有原生 fork（WorkBuddy 只有 CLI 侧有 `/branch`）。这个技能把当前对话从磁盘读出来、剥掉平台注入、压成一份**能直接粘进新对话的 prompt**——背景全带走，新任务开局不用重新交代。

默认读 WorkBuddy 的会话；在 dsh 里跑要带 `--source dsh`，或者用 `--path` 直接指导出包（来源按内容自动认）。

```
旧对话 → 一条命令导出 → 自动出骨架 → 填成人话 → 整段粘进新任务
```

## 它解决什么

聊了 50 轮，你想换个方向试，但又舍不得前面聊出来的东西。

桌面端没有「从这里另开一条线」，只能新开任务然后手动复述一遍：定过哪些方案、试过什么不通、卡在哪。复述一遍等于重新想一遍，而且一定会漏。

这个技能把这件事变成一句话。

## 安装

### WorkBuddy（主要目标）

把整个 `fork/` 文件夹放进用户级技能目录：

| 系统 | 路径 |
|---|---|
| Windows | `%USERPROFILE%\.workbuddy\skills\fork\` |
| macOS / Linux | `~/.workbuddy/skills/fork/` |

```bash
git clone https://github.com/eggacheb/workbuddy-fork.git /tmp/workbuddy-fork
# macOS / Linux
cp -r /tmp/workbuddy-fork ~/.workbuddy/skills/fork
# Windows (PowerShell)
Copy-Item -Recurse .\workbuddy-fork "$env:USERPROFILE\.workbuddy\skills\fork"
```

重启 WorkBuddy。左侧技能面板里能看到 `fork` 就装好了。

### dsh（DeepSeek Harness）

同一个技能目录，换个位置：

| 系统 | 路径 |
|---|---|
| Windows | `%USERPROFILE%\.dsh\skills\fork\` |
| macOS / Linux | `~/.dsh/skills/fork/` |

`scripts/sync-skill.sh` 会一起同步两个目标——`~/.dsh/skills/` 不存在时自动跳过，没装 dsh 的机器上不会凭空多出目录。

在 dsh 里读 `.zstd` 压缩的活日志需要 **Python 3.14+** 或 `pip install zstandard`。都没有时脚本报错退出，不会给你一份空包。不想装依赖就用应用里的「导出会话」拿 zip，再用 `--path` 指过去。

### 开发时：让 clone 的改动生效

如果你有一份 clone、又在 `skills/` 下放了一份副本，**两处不会自动同步**——push 完忘了复制，跑的还是旧代码，而且没有任何提示。装一次钩子就解决：

```bash
cd <你的 clone>
scripts/sync-skill.sh --install-hooks   # 装 post-commit / post-merge 钩子
scripts/sync-skill.sh                   # 立刻同步一次
```

之后每次提交或合并，都会自动把 `SKILL.md` / `README.md` / `LICENSE` / `scripts/` 复制到 `~/.workbuddy/skills/fork/`（目标可用 `WORKBUDDY_SKILLS_DIR` 覆盖，改名/删掉的旧脚本也会一起清掉）。

脚本改动立即生效；**`SKILL.md` 的 `description` 要重启 WorkBuddy 才会重新注入**。钩子不进版本库，换机器要重跑一次 `--install-hooks`。

### 测试

```bash
python -m unittest discover -s tests -v
```

全是 `unittest`，不引第三方依赖。夹具用 `WORKBUDDY_HOME` 指到临时目录自己造 jsonl，**不读真实会话**——否则用例结果取决于这台机器上聊过什么。

提交时 `pre-commit` 会跑一遍，过不了就拦下（`scripts/sync-skill.sh --install-hooks` 装的）；CI 用 `windows-latest` 跑同一份。

还有一个只对开发者有意义的：

```bash
python scripts/corpus_check.py                  # 扫本机全部真实会话,报各项数字
python scripts/corpus_check.py --save base.json # 存基线
python scripts/corpus_check.py --compare base.json  # 改完之后比,任何一项变少就是回归
python scripts/corpus_check.py --equiv HEAD~1   # 和旧版逐字节比输出,证明重构等价

python scripts/corpus_check.py --source dsh     # 扫本机全部 dsh 会话(默认 DSH_HOME)
```

单测覆盖不到「真实语料里有没有误伤」这件事——它只能靠真实会话。改完剥离规则跑一次，看 `计入发言` 有没有掉。

dsh 那边的指标不一样：没有「体外残留」这种概念，换成一个更硬的不变量——**计入发言 == 原始发言、整条丢掉 == 0、幽灵发言 == 0、工具调用 == 原始工具记录**，外加「未认识的 `source.kind` == 0」。任何一项破了就直接非零退出（不带 `--compare` 也查）。

### 怎么让 AI 用上它

装好之后**不需要额外配置**。WorkBuddy 启动时会把 `skills/` 下每个 `SKILL.md` 的 `description` 注入给模型，模型看到「分叉」「开分支」「把上下文带过去」这类说法就会自己去调用。

三种触发方式，从自然到明确：

1. 「把这个对话分叉出去」——自动触发
2. 「开个分支，接着做 XX」——自动触发
3. `/fork` 或「用 fork 技能」——手动指定，最稳

如果你的 AI 没有主动用，直接说「用 fork 技能分叉」就行。技能是**手动可用**的，不依赖它自己判断。

### 其他 agent 框架

**支持 WorkBuddy 和 dsh 两种日志。**

dsh 的会话格式和 WorkBuddy 完全不同，所以走的是另一条适配分支（`build_turns_dsh`），不是同一套字段映射：

| | WorkBuddy | dsh |
|---|---|---|
| 日志位置 | `~/.workbuddy/projects/<slug>/<id>.jsonl` | `~/.dsh/sessions/<slug>/<id>/session.v4.jsonl.zstd` |
| 压缩 | 明文 | zstd（导出包是明文 zip） |
| 用户消息 | `{type:"message", role:"user", content:[…]}` | `{type:"user/message", data:{content:[…], source:{kind}}}` |
| 区分「谁在说话」 | 靠 `<user_query>` 切体内外，再按标签剥 | 靠 `data.source.kind`：只有 `user` 是人话 |
| 工具调用 | `type:"function_call"` | `type:"tool/ptc-dispatch"`（PTC 模式）；直接模式下是 `type:"tool/call"` 里的真工具 |

Claude Code、Cursor 等其它框架仍然不支持：装到 `~/.claude/skills/` 里能加载，但读不到会话。要加一种来源，照着 `build_turns_dsh` 再写一个适配器——它产出与 `build_turns` 同一套 turn schema，渲染、`--from`、包骨架都不用动。

## 用起来什么样

```
你：把这个对话分叉出去，新线改成做 B 方案

AI：（读日志、剥注入、出包）
    新建任务，把下面这段整段粘进去就能开工：

    ┌─────────────────────────────────────┐
    │ 接着做 XX 的 B 方案。                │
    │ ## 之前那条线到哪了                  │
    │ ## 已经定下的                        │
    │ ## 试过但不通的                      │
    │ ## 关键事实                          │
    │ ## 这次要做                          │
    └─────────────────────────────────────┘

你：新建任务 → 整段粘贴 → 发送
```

新对话一上来就知道全部背景，不用读任何文件。

**第 3 步必须你自己点。** 桌面端没有创建会话的接口，技能不会自动建任务——它只负责把背景递过去。

## 它怎么工作

### 1. 找到日志

当前会话 ID 直接取环境变量 `CODEBUDDY_SESSION_ID`，不用猜。日志路径按会话 ID 全局匹配，不依赖项目目录名。

**对不上就是错误，不会替你猜。** 后台 / 子代理会话的日志常常还没落盘，这时脚本报错并列出最近的几个会话。要最近那个得显式加 `--newest`——悄悄换成别的会话，你从输出里看不出来。

>`~/.workbuddy/sessions/*.json` **不是对话**，那是进程心跳（pid / heartbeat），别搞混。

### 2. 剥掉平台注入

会话日志是「你说的话」和「平台每轮塞给模型的上下文」混在一起存的。不剥的话，一份 29 MB 的日志里有 95% 是噪音。

实测效果：

| | 剥前 | 剥后 |
|---|---|---|
| 一条压缩摘要 | 294,814 字符 | 0 —— 整块丢，里面的"原话"是上一轮的重复 |
| 某会话的用户发言 | 192 条 | 117 条 |

剥的是这些（全部有明确标签边界，不靠猜语义）：

- `<system-reminder>` —— 每轮注入的系统上下文
- `<task-notification>` —— 后台任务完成通知
- `<cb_summary>` / `<conversation_history_summary>` —— 上下文压缩摘要
- `<previous_*>` —— 历史重放
- `<teammate-message>` / `<additional-data>` / `image_local_path`
- 标签外的 harness 指令（`Use the TaskOutput tool with…`）

**留在 `<user_query>` 里的会保留**，所以你真正的输入一句不丢。`<long_text_quote>`（你 `@` 引用粘进来的正文）只脱壳、内容留着。用户自己贴的 `<div>`、`<config>` 之类代码也不会被误伤——只剥白名单里的标签。

**写在反引号里的标签不算结构。** 你讨论这个工具时会把 `<user_query>`、`@image#1:a.png` 写成行内代码，那不是真标签——按真标签处理会把切分点错位（注入被当原话、原话被当注入），还会凭空造出一个挂在不相关消息开头的 `[图片 …]`。见 `docs/adr/0004`。

唯一的例外：整个 `<user_query>` 里除了一个已知协议标签什么都没有，说明那一轮你其实没说话，这条会被整条丢掉。留着它会凭空多出一条发言，把后面所有编号往后顶一位，`--from N` 从此指错地方。夹着正文的、或者同一个标签出现两次的（说明你在讨论这个标签本身）都照常保留。

**`@skill:` / `@"…"` 会换成 `[技能 code-review]` / `[文件 src/y.py]`，不是删掉。** 它记的是「这句话当时是用哪个技能触发的」，删了之后新对话就看不出该按 `/code-review` 还是 `/to-spec` 的流程办事——而这件事仓库里查不到。文件只留最后两段路径：够认出是哪个 `y.py`，又不像完整路径那样占篇幅。

**`@"…"` 只认平台盖过章的。** 用户引用文件时，平台会在 `<user_references>` 里列出完整路径；只有对得上那里的 `@"…"` 才换成 `[文件 …]`，对不上的原样留着。因为 `@"…"` 和 C# 的 verbatim string 语法完全相同，认错就会改坏代码——实测全库 21 处转换、11 处有意保留。

**`@long-text:"预览"` 按「后面跟没跟引文块」分两种处理。** 跟着 `<long_text_quote>` 时，预览串就是正文前 20 字的重复，删掉；没跟引文块时，这串预览是这个引用留下的唯一痕迹，换成 `[长引用 「预览」]` 留着。判据是机械的（同一个 `<user_query>` 体内有没有引文块），不猜语义——否则用户在正文里讨论这个标记本身时，原话会被吃成一对空反引号。

### 2.5 dsh 走的是另一套：按来源，不按标签

dsh 的日志里，注入和原话是同一个结构（都是 `user/message`），正文里也未必有标签——按上面那套正则会**全部空转**。多出来的信号是 `data.source.kind`：

| `kind` | 是什么 |
|---|---|
| `user` | 用户真说的话（还带 `rpcId` + `clientTimeZone`） |
| `ptc-mode` | `read_image` 的工具回执，不是人话 |
| `agent-instructions` / `skill-catalog` / `skill-invocation` / `runtime-context` / `plugin:*` | 平台注入 |
| `agent-message` / `subagent-settled` | 子代理的往来，不是用户说的 |
| `compact-checkpoint` | 压缩摘要 |

只认 `kind == "user"`，其余一律不进正文，**但会被数出来**：header 的 `- 忽略:非用户消息 N 条` 就是它们；有压缩检查点时那一行后面补 `,其中压缩检查点 K 处(原文完整保留)`。实测本机 118 个 dsh 会话：353 条真发言全部计入，挡掉 1007 条注入，0 条误判。

两条容易误会的地方：

- **压缩检查点不导出，原文全留。** dsh 上下文满时会生成一份摘要并用 `surfaceOp` 标记「这段被替换了」。我们只读原始事件，不重放替换——摘要是原文的机器摘要，信息量严格更小。见 `docs/adr/0008`。
- **工具行来自两种记录，判据是结构不是名字。** PTC 模式下 `tool/call` 只是 `run_code` 容器，真工具挂在它的 `tool/ptc-dispatch` 下；直接模式下 `tool/call` 自己就是 `read`/`bash`。分法是「有没有派发挂在它下面」（`callId` 是否出现在某条 `ptc-dispatch` 的 `rootCallId` 里），容器不显示、真工具显示。按名字认 `run_code` 会在平台改名时静默漏掉整类工具——实测有 6 个会话是纯直接模式，只认 `ptc-dispatch` 时它们的工具数是 0。

新出现一种 `kind` 时，脚本会把它当注入丢掉，同时在 stderr 打一行「遇到没见过的 source.kind」——**不会静默**。`corpus_check --source dsh` 会把这类情况直接判成失败。

### 3. 出骨架

机械部分自动填好：改过的文件（带次数）、跑过的命令、报错原文、查过的链接、工作目录、时间跨度。

这几项是模型猜不出来的真事实。人话部分留空给模型填。

### 4. 按节自检

`--check` **只报每一节占多少篇幅，不判合不合格**：

```
共 2965 字符

  (开头)                62 字符   2%  █
  之前那条线到哪了       408 字符  13%  ████
  已经定下的             525 字符  17%  █████
  试过但不通的           340 字符  11%  ███
  关键事实              1179 字符  39%  █████████████
  这次要做               375 字符  12%  ████
```

这是刻意设计。之前试过设「1500~3000 字」的达标线，结果观察到：模型为了凑够数字去**合并两条本该独立的决策**，牺牲内容换指标。现在只指病灶——「关键事实占大头，里面多半是仓库查得到的东西，换成路径」。

## 命令

```bash
python scripts/fork.py [选项]
```

| 选项 | 作用 |
|---|---|
| （无） | 导出当前会话全量 |
| `--outline` / `-o` | 只列用户发言，挑分叉点用 |
| `--from N` | 从第 N 个用户发言开始（**含**这一句） |
| `--last N` | 配 `--from` 时是「从第 N 个往后取 M 个」；单独用时取最后 N 个 |
| `--compact` | 压缩版：单条助手 200 字、单轮 500 字、用户 400 字，工具只留次数 |
| `--package` | 同时生成 `<会话>[-fromN]-package.md` 骨架 |
| `--force` | 允许覆盖已存在的包（默认不覆盖，防止把填好的清掉） |
| `--check FILE` | 按节报篇幅占比 |
| `--session <id>` / `-s` | 指定会话（默认当前会话） |
| `--path PATH` | 直接给文件：dsh 导出 zip、解压目录，或 `session.v4.jsonl(.zstd)`。来源按内容自动认 |
| `--source {auto,workbuddy,dsh}` | 日志来源。给了 `--path` 时按内容认，不给时默认按 workbuddy 找 |
| `--newest` | 会话 ID 对不上时，显式改用最近活跃的会话 |
| `--list` / `-l` | 列出各项目最近的会话（配 `--source dsh` 列 dsh 的） |
| `--out PATH` | 指定输出路径（默认系统临时目录 `workbuddy-fork/`） |
| `--stdout` | 打到标准输出。文件是完整的，只是 Bash 工具的回显有上限，长会话看起来会像被截断 |

**跨平台**：macOS/Linux 上 Python 常叫 `python3`；`cmd.exe` 里 `$HOME` 不展开，改用 `%USERPROFILE%`。

## 判断该带多少

轮数只是粗筛，**更准的信号是「线之间什么关系」**：

- **并列的线**（装技能 / 改 bug / 调研）→ 一条线一个包，混在一起互相稀释
- **递进的线**（部署 → 手机访问 → 语音 → 调研，每步吃上一步的结论）→ **别拆**，拆完新对话还得回头补背景

拿不准先 `--outline` 扫标题：后一条在引用前一条的成果，就是递进。

## 为什么不是「读完整文件」

导出的原文是**证据**，不是正文。

485 KB 的文件，新对话读到哪儿算够？它没有完成标准——只会读到「感觉懂了」就开工，而那正是最容易漏掉关键约束的时候。包的价值是**它有边界**：读完，背景就齐了。

同理，包末尾那条指向原文的路径必须**带窄条件**：

```
原对话完整导出：`<路径>`（45 KB，第 18~52 轮）。只在我怀疑某条结论被概括得不准时
翻它核对原话；核对完回来，不要通读。
```

写「需要时可查阅」等于没条件，新对话会为保险整份读完。写「你自己决定要不要读」更糟，等于取消准入线。

## 限制

- **不是真分叉。** 不会自动建任务、不会自动切过去，旧线也不会因为分叉而改变。两边不同步，是两条独立的时间线。
- **只支持 WorkBuddy 和 dsh。** 其它框架见上面「其他 agent 框架」。
- **dsh 读活日志要 zstd。** Python 3.14+ 或 `pip install zstandard`，缺了会报错退出（不会给空包）。导出包走 `--path` 不需要依赖。
- **临时文件会被清。** 要长期留档就 `--out` 到固定目录。
- **后台 / 子代理会话可能不落盘。** 这种会话拿不到日志。脚本会报错并列出最近的几个会话，确认是哪个之后用 `--session` 指定；要最近那个就用 `--newest`。

## 隐私

导出的文件是**本地明文会话记录**，可能含路径、密钥、私人内容。分享给任何人之前自己扫一遍。别把它提交进仓库。

`.gitignore` 已经排除 `__pycache__`，但没有也不会排除你 `--out` 到仓库里的导出文件——那是你自己的责任。

## License

MIT
