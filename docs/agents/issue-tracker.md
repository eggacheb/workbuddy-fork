# Issue tracker: GitHub

本仓库的 issue 和 spec 都放在 GitHub Issues 里,一律用 `gh` CLI 操作。

仓库从 `git remote -v` 推断;在 clone 里跑 `gh` 会自动认。

## 约定

- **建 issue**:`gh issue create --title "..." --body "..."`。多行正文用 heredoc。
- **读 issue**:`gh issue view <number> --json number,title,body,labels,comments`。
- **列 issue**:`gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`,按需加 `--label` / `--state` 过滤。
- **挂子 issue**:`gh issue create --parent <parent> ...`,或事后 `gh issue edit <parent> --add-sub-issue <child>`(`gh` 2.94+)。老版本 `gh`:`gh api --method POST repos/<owner>/<repo>/issues/<parent>/sub_issues -F sub_issue_id=<child-db-id>`(要**数据库 id**,不是 `#number`)。不支持子 issue 时,在子 issue 正文顶部写 `Part of #<parent>`。
- **评论**:`gh issue comment <number> --body "..."`
- **加/删标签**:`gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **关闭**:`gh issue close <number> --comment "..."`

## PR 作为分诊入口

**PR 不作为请求入口(`no`)。** _(这个仓库只接 issue。若以后要收外部 PR 当功能请求,把这里改成 `yes`,`/triage` 会读这个开关。)_

改成 `yes` 之后,PR 走和 issue 同一套标签与状态,命令换成 `gh pr` 系列:

- **读 PR**:`gh pr view <number> --comments`、`gh pr diff <number>`。
- **列外部 PR**:`gh api --paginate 'repos/{owner}/{repo}/pulls?state=open' --jq '.[] | select(.author_association | IN("OWNER","MEMBER","COLLABORATOR") | not) | {number, title, author: .user.login, author_association, labels: [.labels[].name]}'`。
- **评论 / 打标签 / 关闭**:`gh pr comment`、`gh pr edit --add-label` / `--remove-label`、`gh pr close`。

GitHub 的 issue 和 PR 共用一个编号空间,所以裸的 `#42` 可能两者都不是:先用 `gh pr view 42`,失败再退到 `gh issue view 42`。

## 技能说「publish to the issue tracker」时

建一条 GitHub issue。

## 技能说「fetch the relevant ticket」时

按上面 **读 issue** 读。

## Wayfinding 操作

供 `/wayfinder` 使用。**地图**是一个 issue,**子 issue** 是票。

- **地图**:一条打 `wayfinder:map` 标签的 issue,正文装 Notes / Decisions-so-far / Fog。`gh issue create --label wayfinder:map`。
- **子票**:作为地图的子 issue 关联(见上面「挂子 issue」)。不支持子 issue 时,把子票加进地图正文的 task list,并在子票正文顶部写 `Part of #<map>`。标签 `wayfinder:<type>`(`research` / `prototype` / `grilling` / `task`)。领取后 assign 给驱动的 dev。
- **阻塞**:用 GitHub 原生 issue 依赖,这是唯一在 UI 里看得见的表示。加边:`gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`,`<blocker-db-id>` 是阻塞方的数字**数据库 id**(`gh api repos/<owner>/<repo>/issues/<n> --jq .id`,**不是** `#number`,也不是 `node_id`)。GitHub 用 `issue_dependencies_summary.blocked_by` 报未关闭的阻塞数,这是实际的闸门。依赖功能不可用时,退回在子票正文顶部写 `Blocked by: #<n>, #<n>`。所有阻塞都关闭,票才算解锁。
- **前沿查询**:列出地图下未关闭的子票(`gh issue list --state open`,限定在地图的子 issue / task list 内),丢掉有未关闭阻塞(`issue_dependencies_summary.blocked_by > 0`,或 `Blocked by` 行里还有未关闭的)或已有 assignee 的;按地图顺序取第一个。
- **领取**:`gh issue edit <n> --add-assignee @me`——这是本会话的第一次写入。
- **了结**:`gh issue comment <n> --body "<答案>"`,然后 `gh issue close <n>`,再把上下文指针(gist + 链接)追加到地图的 Decisions-so-far。
