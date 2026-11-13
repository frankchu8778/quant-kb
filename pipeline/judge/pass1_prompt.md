你是资深量化研究员。下面是一篇券商量化研报的全文（由 PDF 自动转换为 Markdown，可能有少量排版或识别错误）。请仔细阅读，完成三件事。

## 背景：卡片的用途

这篇研报会进入一个研报知识库。日后开发量化策略时，AI agent 会先搜索「研报卡片」，从中找可以借用的思路，再决定要不要读全文。卡片只做忠实的摘要，不评价研报好坏。因此一张好的卡片，要让人不读全文也能准确知道：研究什么问题、核心想法是什么、具体怎么做、用什么数据、结果如何、有什么前提和限制。

## 任务一：写一张参考卡片（reference_card）

按下方字段说明写一张卡片。要求：

- 一切以原文为准。数字只抄原文出现过的，不自己计算或推导；原文没有就不写。
- `quote` 类字段必须从原文逐字复制一段连续文字（10–80 字），不得改写或用省略号拼接。
- 方法标签只能用「方法词汇表」里的 id；词汇表缺少、但你认为重要的方法写进 `new_tag_suggestions`。
- 卡片内容用简体中文（与知识库一致）。

字段说明：

- `title`、`broker`、`date`（YYYY-MM-DD，不明则 null）、`series`（无则 null）
- `report_type`：深度专题 / 系列专题 / 周报点评 / 产品分析 / 其他
- `primary_strategy_type`、`strategy_types`：见「策略大类」；按「这篇的思路最可能被借用到哪类策略」判断
- `frequency`、`asset_class`：见「策略大类」文件的枚举
- `families`、`methods`：见「方法大类」与「方法词汇表」，按重要性排序
- `new_tag_suggestions`：词汇表缺少的方法（中文名称）
- `data_needed`：借用这个思路需要的数据
- `summary`：三句话以内
- `core_idea`：真正的核心想法或洞见，而不是复述摘要
- `method_steps`：可照着实现的步骤
- `key_formulas`：`{latex, meaning}`，只抄原文的核心公式，抄不准就不抄
- `key_results`：`{metric, value, context, quote}`
- `limitations`：原文提到的局限，或能从原文直接看出的限制（后者前面标注「[推断]」）
- `implementation_gaps`：原文**没有交代**、但实现时必须知道的细节（参数、数据来源、样本区间、调仓规则等）
- `evidence_quotes`：`{claim, quote}`，支撑核心想法与方法判断的原文片段

## 任务二：列出不能遗漏的要点（must_capture）

列出 3–5 个要点：任何一张卡片如果漏掉其中一点，借用这篇思路的人就会理解错或做不出来。每个要点附一段逐字原文引用，以及它所在的章节标题。

## 任务三：分类与工具反馈

- `classification_notes`：如果这篇在策略大类或方法大类上模棱两可，说明你在哪几类之间犹豫、最后为什么这样选；不模棱两可就写空字符串。
- `taxonomy_feedback`：策略大类或方法词汇表如果有定义不清、缺少类别的地方，具体指出。
- `conversion_issues`：如果全文有明显的转换问题（乱码、表格错位、公式残缺、段落缺失），指出位置与影响；没有就留空。

`classification_notes`、`taxonomy_feedback`、`conversion_issues`、`must_capture` 的说明文字用繁體中文书写（原文引用保持简体原样）。

## 策略大类

{strategy_types}

## 方法大类

{families}

## 方法词汇表

{methods}

## 研报全文（{report_id}）

{fulltext}
