# Domain Docs

工程技能在探索这个代码库时,该怎么消费它的领域文档。

## 探索之前先读

- 根目录的 **`GLOSSARY.md`**,或
- 根目录的 **`GLOSSARY-MAP.md`**(如果存在):它指向每个上下文各自的 `GLOSSARY.md`,读和当前话题相关的那些。
- **`docs/adr/`**:读涉及你即将改动区域的 ADR。多上下文仓库还要看 `src/<context>/docs/adr/` 里的上下文级决策。

这些文件不存在就**静默跳过**。不要提示缺失,也不要建议先建——`/domain-modeling` 技能(经 `/grill-with-docs` 和 `/improve-codebase-architecture` 到达)会在术语或决策真正敲定时懒创建。

## 布局

本仓库是**单上下文**:一个 `GLOSSARY.md` + 根目录 `docs/adr/`。

```
/
├── GLOSSARY.md
├── docs/
│   ├── adr/
│   │   ├── 0001-不做真-fork.md
│   │   └── 0002-上下文包不给字数上限.md
│   └── agents/          ← 就是这三个文件
├── scripts/fork.py
└── tests/
```

出现 `GLOSSARY-MAP.md` 才算多上下文;本仓库没有,也没有 monorepo 信号。

## 用术语表里的词

当你的产出要命名一个领域概念(issue 标题、重构提案、假设、测试名),用 `GLOSSARY.md` 里定义的写法,别漂到术语表明确避开的同义词上。

如果你要的概念还不在表里,这本身是个信号:要么你在发明这个项目不用的说法(再想想),要么确实有缺口(留给 `/domain-modeling`)。

## ADR 冲突要摆出来

如果你的产出和已有 ADR 矛盾,明确指出来,别默默覆盖:

> _与 ADR-0001(不做真 fork)冲突,但值得重开,因为…_
