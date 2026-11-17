# 量產操作手冊

一批研報從 PDF 到審過的卡片，照這份順序跑。設計理由與成本估算見 `../PLAN.md`。

Python 一律用 `~/venvs/kb-parse/bin/python`，在 `pipeline/` 下執行。`<batch>` 是 `inbox/` 下的資料夾名稱。

## 階段總覽

| 階段 | 指令 | 花錢嗎 | 狀態 |
|---|---|---|---|
| — 選檔 | `batch.py sample <batch> --n 100` | 否 | 可用 |
| 0 入庫 | `batch.py ingest <batch>` | 否 | 可用 |
| 1 轉檔 | `batch.py convert <batch>` | 否（本機 GPU） | 可用 |
| 1a 重複性檢查 | `batch.py dedup <batch>` | 否 | 可用 |
| 1b 品質閘 | `batch.py quality <batch>` | 否 | 可用 |
| 2 去固定頁 | `batch.py clean <batch>` | 否 | 可用 |
| — 預算確認 | `batch.py status <batch> --budget N` | 否 | 可用 |
| 4 第一次寫卡（Kimi batch，六折） | `run.py submit <batch> --limit N --budget X` | **是** | 見下方 |
| 5–6b 收回、檢查、退回重寫再複檢 | `run.py collect <batch> --budget X --wait` | **是** | 見下方 |
| — 格式試打 | `run.py probe <batch> --budget 5` | **是**（約 ¥1.3） | 見下方 |
| — 備援：全部走即時 | `run.py cards <batch> --budget X` | **是**（原價） | 用假資料測過 |
| 8 持續埋雷 | `run.py canary <batch> --budget N` | **是** | 同上 |
| — 結果統計 | `run.py report <batch>` | 否 | 可用 |
| — 人工複核表 | `run.py review <batch>` | 否 | 可用 |
| — 分流規則對照 | `run.py triage <batch>` | 否 | 可用 |
| 9 發布 | `render_cards.py` | 否 | 還是舊卡片格式，未更新 |

分流（階段 3）、升級（7）、抽樣審計（8）見 `../PLAN.md`，還沒有腳本。整體關係圖：`../docs/pipeline_overview.png`。待辦：`../TODO.md`。

## 不花錢的部分：照順序跑

### 選檔（可省略）

```bash
python batch.py sample batch1 --n 100
```

從 `.env` 的 `Research_Report_PATH` 隨機選 PDF 複製到 `inbox/batch1/`。已經在 manifest 或其他批次裡的（依檔案雜湊）不會被選到。選了哪些記在 `inbox/batch1/_selection.json`。自己放檔案的話跳過這步。

### 0. 入庫

把 PDF 放進 `inbox/<batch>/`，檔名最好是 `YYYYMMDD-券商-標題.pdf`（不是也能跑，只是 id 會變成 `nodate_unknown_<雜湊>`）。

```bash
python batch.py ingest batch1
```

- 用檔案內容的雜湊去重，**跨批次**都算：同一份 PDF 換檔名再放一次，只會記在原紀錄的 `duplicates`。
- **快篩**：直接抽 PDF 前兩頁的文字算指紋，和全庫比對。相似度 ≥ 90% 就 hold，不會進轉檔。抓的是「同一篇被加了浮水印或重新壓縮」。掃描版抽不到字，這一步會略過。
- 檔名不符合格式的會列出來，id 會變成 `nodate_unknown_<雜湊>`。可以先改檔名再重跑。
- 紀錄寫在 `catalog/manifest.json`，之後每個階段都更新同一筆。

### 1. 轉檔

先啟動 MinerU 的 VLM 服務（佔用 GPU，跑之前關掉其他 vLLM）：

```bash
bash serve_mineru_vlm.sh
```

```bash
python batch.py convert batch1
```

- 預設 `--tier standard`，行內公式比 basic 乾淨，每篇約 33 秒。
- 只處理還沒轉的篇，中斷後重跑即可。MinerU 沒有輸出的篇維持 `ingested`。
- 原始輸出留在 `pipeline/_mineru_raw/<batch>/<tier>/`，不修改。

### 1a. 重複性檢查

```bash
python batch.py dedup batch1
```

用轉檔後的全文和**全庫**（含之前所有批次）比對，相似度 ≥ 60% 就 hold。不看檔名，所以從不同網站抓到的同一篇也擋得住。

- 相似度的算法：把文字只留中英文與數字，切成 5 個字的片段，算兩篇片段集合的重疊比例。浮水印、空白、標點、排版不影響。指紋存在 `catalog/fingerprints.json`。
- 在 POC 的 20 篇上量到的數字（門檻在 `batch.py` 的 `DUP_FULL_THRESHOLD`）：

  | 情況 | 相似度 |
  |---|---|
  | 同一篇，不同轉檔等級 | 97% |
  | 同一篇，每段加浮水印 | 90% |
  | 同一篇，少最後 10% | 84% |
  | 同一篇，只有前半 | 55% |
  | **不同研報裡最像的**（國金同系列的四期週報互相比） | 44–53% |
  | 其餘不同研報 | 三分之二低於 10%，全部低於 40% |

- 60% 離「同系列週報」只有 7 個百分點。模板更固定的週報系列可能會被擋下，這是預期中的：看一眼確認後放行即可。
- 被擋下的是**後進來的那一篇**，先入庫的不動。

### 被 hold 之後

每個指令結束時會列出這次新擋下的篇和原因；`status` 會列出全部。被擋下的篇，PDF 會從 `inbox/<batch>/` 移到 **`hold/<batch>/`**，同一個資料夾裡的「待處理清單.md」寫明每篇的原因。確認不是問題的：

```bash
python batch.py release batch1 <id> [<id> ...]
```

PDF 會移回 `inbox/`。要重新整理 hold 資料夾與清單：

```bash
python batch.py holds batch1
```

會回到被擋下之前的階段繼續往下走，不會再被同一道檢查擋。放行紀錄留在 manifest 的 `released`。確定是重複的就不用處理，維持 hold 即可。

### 1b. 品質閘

```bash
python batch.py quality batch1
```

不合格的標成 `hold`，不會進寫卡。門檻在 `batch.py` 開頭：

| 條件 | 門檻 | 通常代表 |
|---|---|---|
| 亂碼比例 | ≥ 0.5% | 字型解不出來 |
| 全文字數 | < 2,000 | 轉檔失敗、只有封面 |
| 每頁字數 | < 150 | 掃描版，OCR 沒出字 |
| 文字佔比 | < 30% | 幾乎都是表格或符號 |
| 全文字數 | > 120,000 | 超過試過的長度，先人工確認 |

`hold` 的篇請看原 PDF 決定：改用別的轉檔方式、拆開、或放棄。

### 2. 去固定頁

```bash
python batch.py clean batch1
```

- 規則在 `clean.py`，刻意保守。被刪的內容存在 `fulltext/<batch>/clean/_removed/`。
- 單篇刪掉超過 30% 會印出警告（POC 平均 9%），要打開 `_removed` 看有沒有誤刪正文。
- **新券商第一次出現時，抽幾篇 `_removed` 檢查。**

### 預算確認

```bash
python batch.py status batch1 --budget 300
```

列出各階段篇數、每篇 hold 的原因，以及寫卡加檢查的花費估算。估算用試產實測的 token 量推算，不含重寫與複檢（約多一成）。

**送 API 之前一定要看這一步。**

## 花錢的部分

模型分工（試產結果，見 `../PLAN.md`）：寫卡 `kimi-k3.1-pro`，檢查 `deepseek-flash`，不同家。

### 送出前一定會做的餘額檢查

`submit`、`collect`、`probe`、`cards`、`canary` 在送出任何付費請求之前，都會先：

1. 用這批每篇的**實際字數**估這一步要花多少（Kimi：第一次寫卡 ＋ 兩成五的篇數即時重寫；DeepSeek：筆記與檢查以尖峰價計 ＋ 兩成複檢）。
2. 即時查兩個平台的餘額。
3. **任一邊餘額低於估計的 1.3 倍就不送**，並印出還差多少。DeepSeek 不夠時寫卡也不送，免得卡寫好了沒錢檢查。餘額查不到也不送。

```
餘額檢查（送出 46 篇）：
  Kimi     估計花費 ¥ 36.55，要求餘額 ≥ ¥ 47.52，目前 ¥  91.50  夠
  DeepSeek 估計花費 ¥ 13.77，要求餘額 ≥ ¥ 17.90，目前 ¥  45.30  夠
```

加 `--dry-run` 只印這張表和任務清單，不上傳、不建立 batch。

### 流程：Kimi batch

只有**第一次寫卡**走 batch（六折）；程式檢查不過的重寫、檢查員退回的重寫走即時，否則每篇最多要等三輪。

```bash
python run.py submit batch1 --limit 20 --budget 60 --dry-run
```

```bash
python run.py submit batch1 --limit 20 --budget 60
```

- 每篇研報是 JSONL 裡的一行、一個任務，`custom_id` 就是研報 id。每個 batch 最多 20 篇（`--chunk`）。
- 送出的 JSONL 存在 `eval/<batch>/_kimi_batches/`，每個 batch 的 id、任務清單、狀態記在 `catalog/kimi_batches.json`。
- 已經送出、還沒收回的篇不會重複送。

```bash
python run.py collect batch1 --budget 60 --wait
```

- 查每個 batch 的狀態；完成的下載結果、逐篇落檔到 `eval/<batch>/<id>/kimi_write.json`，以 batch 價記帳。
- 接著對收回的篇做：程式檢查（不過就即時重寫 1 次）→ DeepSeek 檢查 → 退回的即時重寫 1 次再複檢 → 仍不過的標成 `escalate`。
- batch 裡失敗的任務（例如輸出被截斷）會即時重試 1 次。
- `--wait` 會每 2 分鐘查一次，最多等 `--max-wait` 分鐘（預設 180）。不加就只查一次。**完成後要盡快收回**，結果檔保留多久官方沒寫。
- 中斷後重跑同一個指令即可，不重複花錢。

等 batch 的時候可以先跑檢查員的獨立筆記。筆記只讀全文、不看卡片，不必等 Kimi：

```bash
python run.py notes batch1 --budget 5
```

- 對象是還在 Kimi batch 裡的篇；加 `--limit N` 會再包含接下來要送出的前 N 篇（順序和 `submit` 相同）。
- 只寫 `eval/<batch>/<id>/notes.json` 與 `notes_meta.json`，不動 manifest，可以和等待中的 `collect` 同時跑。`collect` 處理到該篇時會直接用，並把花費併進紀錄。
- 實測每篇約 ¥0.22、5 篇同時跑約 2 分鐘。

### 格式試打

```bash
python run.py probe batch1 --budget 5
```

用和 batch **完全相同的請求內容**打即時端點（原價，約 ¥1.3）。改了寫卡 prompt 或卡片 schema 之後先跑這個：格式有錯會立刻被拒絕（不扣錢），不用等 batch 跑完才發現。

### 備援：全部走即時

```bash
python run.py cards batch1 --budget 150
```

batch 等太久時用。每篇多約 ¥0.43。

### 看結果

```bash
python run.py report batch1
```

程式檢查通過率、模型檢查退回率、**升級率**、每張通過的卡的成本、待升級清單。

### 埋雷：量檢查員的抓到率

```bash
python run.py canary batch1 --budget 20 --every 4
```

在通過的卡上故意改一處（數字對調、同大類錯標籤、邏輯改反），送給檢查員，看它擋不擋得下。邏輯類的改寫用 Kimi 產生，不用 Claude。`--every 20` 是量產時的抽樣率；驗證批次建議 `--every 4`，樣本才夠。

「抓到」的判定是程式做的：卡片沒有被放行，而且有一個非 minor 的問題落在被改的欄位上。沒抓到的請打開 `eval/<batch>/_canary/` 看是真的漏掉，還是埋雷本身太弱。

### 人工複核：檢查員說的錯是不是真的錯

```bash
python run.py review batch1
```

把檢查員判的 critical / major 列成一張表（`results/major_review_<batch>.md`），每條附卡片原話、檢查員的說法、它引的原文，以及那段原文在全文裡找不找得到。逐條填「真 / 誤報 / 吹毛求疵」，就能算出檢查員的誤報率。

## 已知的坑

- **POC 與試產的卡片用的是 basic 等級的全文**（`fulltext/poc/clean-v2` 是從 `basic` 清出來的），不是 standard。新批次預設用 standard，行內公式的寫法會不同（`$R^{2}$` 對 `$R ^ { 2 }$`）。第一批跑完要確認程式檢查的引文比對通過率沒有掉。
- Kimi 的 batch 只有 OpenAI 格式（`/v1/files` ＋ `/v1/batches`），Anthropic 相容端點沒有 batch；DeepSeek 沒有 batch 介面，離峰半價是依時段自動計算。
- Kimi 的 OpenAI 格式要求工具參數的每個欄位都明寫 `type`，只有 `enum` 的欄位會被拒絕（`kimi_batch.moonshot_schema` 會補上）。
- DeepSeek 偶爾回傳壞掉的 JSON，`trial.py` 會重試 3 次。
- Kimi 每篇約 6 分鐘，5 並行跑 200 篇約 4 小時。
- `trial_canary.py`（試產用）的邏輯類埋雷用 Opus 產生，會用到 Claude 訂閱額度；量產的 `run.py canary` 已改用 Kimi。
- API 金鑰在 `../.env`（不進 git）：`DEEPSEEK_API_KEY`、`KIMI_API_KEY`。

## 檔案對照

| 檔案 | 用途 |
|---|---|
| `batch.py` | 本機階段：入庫、轉檔、重複性檢查、品質閘、去固定頁、狀態、放行 |
| `run.py` | 模型階段：batch 送出與收回、檢查、退回重寫、餘額檢查、埋雷、結果統計、人工複核表 |
| `kimi_batch.py` | Kimi 的 OpenAI 格式介面：batch 任務、即時呼叫、餘額查詢 |
| `clean.py` | 去固定頁的規則 |
| `validate.py` | 程式硬檢查 `gate_v4`：schema、引文在全文、數字在同列引文 |
| `card_prompt_v3.md` | 寫卡 prompt（每個方法步驟附引文與章節） |
| `check_prompt.md` | 單卡檢查 prompt |
| `trial.py` | 試產：寫卡、程式檢查、模型檢查 |
| `trial_canary.py` | 埋雷與抓到率 |
| `judge_fable.py` | 三卡對比的審稿（選模型用）；也放了各家 API 的呼叫函式 |
| `vocab.yaml`、`judge/strategy_types.yaml` | 方法詞彙表、策略大類 |
| `convert.py`、`make_cards.py`、`gold_cards.py`、`eval_*.py` | POC 階段的腳本，量產不再使用 |
