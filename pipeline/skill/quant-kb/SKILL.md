---
name: quant-kb
description: 查询量化研报知识库（券商研报的方法卡片与全文）。用于：问某个因子、择时、选股、组合或风险方法在研报里怎么做、效果如何；开发或改进策略时找前人研究、参数与回测数字；需要引用研报作为依据时。
---

# 量化研报知识库

知识库在 `/home/newt/quant-kb/kb/`，只读：由 quant-kb 项目的 `pipeline/publish.py` 生成，改动会在下次发布时被覆盖。卡片与全文都是**简体中文**，搜索词一律用简体。

| 文件 | 内容 |
|---|---|
| `catalog.jsonl` | 一行一篇：id、title、broker、date、series、report_type、primary_strategy_type、strategy_types、asset_class、frequency、families、methods、known_issues |
| `cards/<id>.md` | 卡片。frontmatter 同上，另有 `keywords`（标签的中英同义词）、`fulltext`、`checked`、`audited`。正文：摘要、核心思路、方法步骤（每步附原文引文与章节）、核心公式、关键结果（附原文依据）、局限、复现缺口、所需数据、已知问题 |
| `fulltext/<id>.md` | 研报全文（PDF 转换；表格是 HTML） |
| `VOCAB.md` | 标签对照：策略大类、方法家族、方法的 id、中文、别名、卡片数 |

标签都写成「英文 id/中文名」（如 `index-enhancement/指数增强`），中文或英文都 grep 得到。

## 步骤

1. **对到标签**：读 `VOCAB.md`，把问题对到相关的标签 id；卡片数为 0 的标签说明库里还没有这类研报。再把问题写成简体关键词正则，展开同义词与英文，例如 `动量|momentum|特质动量|残差动量`。完成：一组标签 id ＋ 一个关键词正则。
2. **找候选**：在 `catalog.jsonl` grep 标签 id，在 `cards/` grep 关键词，合并去重。问方法时先排除 `report_type: 周报点评`（几乎没有方法论），问市场观点时再纳入。完成：候选清单，或确定一张都没有。
3. **逐张判定**：读每张候选卡片，判定相关或不相关。完成：每张候选都有判定。
4. **回全文**：要公式、参数、样本期、回测细节时，在该卡的 `fulltext` 里 grep 定位，只读相关段落。卡片和全文不一致时以全文为准。
5. **回答**，格式见下。

## 可信度

- 「」里的引文是逐字原文，程序核对过。卡片自己的叙述是摘要，可能有遗漏。
- 数字以全文为准；引用时带上条件（样本期、股票池、是否含交易成本）。
- `known_issues` > 0：先读卡末「已知问题」，涉及的部分以原文为准，并在回答里说明。
- `checked: revise` 表示核对时被退回、重写后才通过；`audited` 是抽样审计的结论（空白 = 没被抽到）。

## 回答格式

1. 直接回答问题，每个结论标来源卡片 id，例如 `[20190610_国信证券_1818e6]`。
2. 关键数字附条件。
3. 列出「看过但不相关」的候选 id，各附一句理由。
4. 只陈述卡片或全文里有的内容。知识库里没有的，明说「知识库里没有」，再给最接近的卡；要补充一般知识时，标明那是一般知识、不是研报结论。

## 更新

知识库随 quant-kb 项目的发布更新（`python pipeline/publish.py publish <batch>`）。这份 skill 的源文件在 `/home/newt/quant-kb/pipeline/skill/quant-kb/`，`~/.claude/skills/quant-kb` 是指向它的链接。
