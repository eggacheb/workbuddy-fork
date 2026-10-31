# 0007 dsh 按 `source.kind` 白名单,不做文本剥离

- 状态:已决定
- 日期:2026-10-31

## 背景

WorkBuddy 的日志里,用户原话和平台注入混在同一个 `content` 字符串里,只能靠
`<user_query>` 这个标签把「体内」和「体外」分开,再按标签名逐块剥(见
[0003](0003-体内不压空行.md)、[0005](0005-有query时体外整段当注入.md))。

dsh 的日志不是这样。注入和原话都是 `user/message`,结构一模一样,正文里也
不一定有标签:

```
{"type":"user/message","data":{"content":[{"type":"text","text":"按推荐的来"}],
 "source":{"kind":"user","rpcId":"…","clientTimeZone":"Asia/Shanghai"}, …}}
{"type":"user/message","data":{"content":[{"type":"text","text":"<system-reminder>…"}],
 "source":{"kind":"agent-instructions","form":"instructions"}, …}}
```

`samples` 里两种发言在结构上唯一的差别是 `data.source.kind`。实测两份导出包
(43 + 45 条 `user/message`):

| `kind` | 条数 | 是什么 |
|---|---|---|
| `user` | 20 | 用户真说的话,全部带 `rpcId` + `clientTimeZone` |
| `ptc-mode` | 8 | `read_image` 的工具回执(图片路径 + 尺寸),不是人话 |
| `agent-instructions` / `skill-catalog` / `skill-invocation` / `runtime-context` / `plugin:*` / `repeat-tool-reminder` | 14 | 平台注入 |
| `compact-checkpoint` | 1 | 压缩摘要 |

真实发言的正文里 `<标签>` 命中数是 **0**。

本机全库(118 个会话、353 条真发言)又跑出五种当时没见过的,逐条看过内容后一并归为非人话:

| `kind` | 条数 | 是什么 |
|---|---|---|
| `agent-message` | 67 | 子代理发回主代理的报告。是执行细节,主代理的 `text` 里已有结论 |
| `subagent-settled` | 70 | 后台子代理结束通知 |
| `model-selection` | 7 | 换了模型的环境通知 |
| `time-context` | 2 | 时间采样 |
| `user-approval` | 2 | 审批策略变更通知 |

这五种正是「白名单之外」的样本:内容一眼能认出来,但**判据不是「一眼能认出来」**,而是「它是不是 `kind=user`」。写进常量是为了让 `corpus_check` 的「未认识的 kind」归零——那个计数一旦非零,说明平台又加了一种,得回原文看,而不是自动放行。

## 决定

dsh 来源的清洗只认一条判据:`type == "user/message"` 且 `data.source.kind == "user"`。
其余 `user/message` 一律不进正文,只计数;计数打印在 header 的「忽略」行里。

## 理由

- 判据是**位置/来源**,不是措辞。WorkBuddy 那边只能按文本猜,是因为日志没给
  别的信号;dsh 给了,再用正则去猜就是主动丢掉更可靠的判据。
- 两套清洗哲学必须落在不同函数里。dsh 的注入里也有 `<system-reminder>`,但它
  不是「体外」——没有 `<user_query>` 可切。硬塞进 `clean_user`,等于让一套
  「剥标签」的规则去处理一段「已经标注好来源」的文本:剥对了是运气,剥错了
  没人发现。
- 反过来,`clean_user` 对 dsh 几乎空转,这一点必须显式承认。它不是 bug,
  是「这个来源不需要猜」。

## 后果

- 新增 `build_turns_dsh`,与 `build_turns` 并列,产出的 turn dict 与原来同一套
  schema。渲染、`--from`/`--last`、包骨架全部不改。
- 判据依赖 `kind` 的取值集合。dsh 以后新增一种 kind 时,工具会**静默当成注入
  丢掉**——正是这个仓库最怕的失败模式。所以:
  - 认识的 kind 列成常量;
  - 不认识的 kind 在 stderr 打一行「遇到没见过的 source.kind: X(N 条)」;
  - header 的「忽略 N 条」与实测对得上,`corpus_check --source dsh` 用
    「计入发言 == 原始发言」当硬指标。
- `corpus_check` 那套「体外残留」指标在 dsh 下没有意义(没有体内外),换成
  「整条丢掉 == 0」「幽灵发言 == 0」「计入发言 == 原始发言」三条不变量。
