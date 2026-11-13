# Fable 審稿評估（草稿）

用 Claude Fable 5.2 對照研報全文審核卡片，取代人工逐篇審閱。你只需要抽查 Fable 列出的問題（用它附的原文引用在 PDF 裡 Ctrl+F）。

## 流程

```
每篇研報
  第 1 次呼叫（pass1_prompt.md）
    輸入：全文 clean-v2 + 策略大類 + 方法詞彙表
    輸出：Fable 參考卡片（新 schema）+ 必須涵蓋的要點 + 分類/詞彙/轉換回饋
  第 2 次呼叫（pass2_prompt.md）
    輸入：全文 + 第 1 次的筆記 + 匿名卡片 A/B/C（隨機順序）
    輸出：每張卡片的問題清單、六維評分、借用者測試；整篇排名
```

- 執行：`claude -p --model claude-fable-5-2 --tools "" --system-prompt "" --json-schema <schema> --output-format json`，effort 先用 high。
- 全文用 `fulltext/poc/clean-v2/`（三種卡片產生時用的都是這份）。不放 PDF。
- 輸出存到 `eval/fable/`，每篇的 prompt、原始回覆、用量都保留。

## 送進第 2 次呼叫前，卡片的處理

- 三種卡片：`sonnet-v2`、`minimax-v2`、`qwen-v2-think`
- 移除評分類欄位：`value_level`、`value_reason`、`reproducibility`、`reproducibility_reason`
- 統一欄位順序、統一 JSON 縮排；移除 meta 與 raw 輸出
- A/B/C 對應哪個模型：每篇用固定種子隨機打亂，對照表另存在 `eval/fable/_mapping.json`，不送進 prompt

## 埋雷（一次）

在 3 張卡片各改一處，Fable 不知道哪些被改過：

| # | 改動類型 | 例子 |
|---|---|---|
| 1 | 數字 | 把某個 `key_results.value` 改成原文沒有的數字 |
| 2 | 方法標籤 | 把一個主要方法換成詞彙表裡無關的方法 |
| 3 | 核心觀點 | 把 `core_idea` 的結論方向改反 |

改動內容記在 `eval/fable/_canaries.json`，最後統計 Fable 抓到幾個。

## 輸出 schema 重點

- 第 1 次：`reference_card`（新卡片 schema：去掉評分欄位，新增 `primary_strategy_type`、`strategy_types`、`frequency`、`asset_class`、`implementation_gaps`）、`must_capture[]`、`classification_notes`、`taxonomy_feedback`、`conversion_issues`
- 第 2 次：`cards.{A,B,C}.issues[]`（欄位、類型、嚴重度、卡片原話、原文實際、引用、章節）、`scores`（六維 0/1/2）、`borrower_test`；`ranking`、`must_capture_coverage`、`reference_corrections`、`conversion_issues`

## 試算與分段

1. 先跑 1 篇（兩次呼叫），你在前後看 `/usage`
2. 依實際用量決定 20 篇要分幾段跑
