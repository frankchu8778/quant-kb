# 量化研报知识库

开发策略时，从这里查找研报中的方法与思路。卡片与全文都是**简体中文**。

## 结构

- `catalog.jsonl`：一行一篇（id、标题、券商、日期、value_level、primary_family、methods），用来快速筛选
- `cards/<id>.md`：研报卡片。frontmatter 有 `methods`、`keywords`（中英同义词）、`value_level`、`reproducibility`、`fulltext`（全文路径）；正文是核心观点、创新点、方法步骤、公式、结果、局限
- `fulltext/<id>.md`：研报全文（PDF 转换，可能有少量公式识别噪声）

`value_level: low` 表示市场点评、周报一类，几乎没有方法论。

## 检索步骤

1. 把问题转成简体关键词，并展开同义词与英文，例如 `动量|momentum|特质动量|残差动量`。
2. grep `cards/`（frontmatter 与正文），列出候选卡片；必要时用 `catalog.jsonl` 按 `value_level`、`primary_family` 过滤。
3. 逐张读候选卡片，判断是否相关。
4. 需要公式、参数、实证细节时，先 grep 定位，再只读 `fulltext` 中的相关段落。

完成标准：每个候选都判定过相关或不相关；回答中的每个结论都标注来源卡片 id，引用的数字以全文为准。
