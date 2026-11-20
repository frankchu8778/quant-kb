# 量化研报知识库

开发策略时，从这里查找研报中的方法与思路。卡片与全文都是**简体中文**。

## 结构

- `catalog.jsonl`：一行一篇（id、标题、券商、日期、系列、report_type、primary_strategy_type、strategy_types、asset_class、frequency、families、methods、known_issues），用来快速筛选
- `cards/<id>.md`：研报卡片。frontmatter 有上面这些字段，加上 `keywords`（标签的中英同义词与卡片建议的新标签）、`fulltext`（全文路径）、`checked`、`audited`、`known_issues`。正文依次是摘要、核心思路、方法步骤（每步附原文引文与章节）、核心公式、关键结果（附原文依据）、局限、复现缺口、所需数据
- `fulltext/<id>.md`：研报全文（PDF 转换，表格是 HTML，可能有少量公式识别噪声）

标签都写成「英文 id/中文名」，例如 `methods: [index-enhancement/指数增强]`，中文或英文都能 grep 到。

`report_type: 周报点评` 的研报几乎没有方法论，找方法时可以先排除。

## 卡片可信度

每张卡都经过：程序检查（引文逐字在原文、数字在同行引文里）、另一个模型对照原文核对、部分卡片再被第三个模型抽样审计。

- `checked: revise` 表示核对时被退回、重写后才通过
- `audited` 是抽样审计的结论（`pass` 或 `revise`；空白表示没被抽到）
- `known_issues` > 0：人工确认卡片有错但尚未修正，**先读卡片末尾的「已知问题」**，涉及的部分以原文为准

卡片是摘要，不是原文。方法步骤里的引文是逐字原文，可以信；卡片自己的叙述可能有遗漏。

## 检索步骤

1. 把问题转成简体关键词，并展开同义词与英文，例如 `动量|momentum|特质动量|残差动量`。
2. grep `cards/`（frontmatter 与正文），列出候选卡片；必要时用 `catalog.jsonl` 按 `report_type`、`primary_strategy_type`、`methods` 过滤。
3. 逐张读候选卡片，判断是否相关。
4. 需要公式、参数、实证细节时，先 grep 定位，再只读 `fulltext` 中的相关段落。

完成标准：每个候选都判定过相关或不相关；回答中的每个结论都标注来源卡片 id，引用的数字以全文为准。
