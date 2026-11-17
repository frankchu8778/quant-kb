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
| 4–6b 寫卡、檢查、退回重寫再複檢 | `run.py cards <batch> --budget N` | **是** | 程式寫好，用假資料測過，還沒真的送出 |
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

每個指令結束時會列出這次新擋下的篇和原因；`status` 會列出全部。確認不是問題的：

```bash
python batch.py release batch1 <id> [<id> ...]
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

### 先看會花多少

```bash
python run.py cards batch1 --budget 150 --dry-run
```

只列出會處理哪些篇和花費估算，不送出任何請求。

### 送出

```bash
python run.py cards batch1 --budget 150
```

每篇的流程：寫卡 → 程式檢查（不過就帶錯誤清單重寫 1 次）→ 模型檢查 → 退回的帶問題清單重寫 1 次再複檢 → 仍不過的標成 `escalate`（待升級）。

- `--budget` 是這次執行的上限（人民幣，以原價計，偏保守）。達到後不再送新的請求；已經送出、還沒回來的請求仍會算進去，所以實際會超出一點（最多約並行數 × ¥1）。
- API 回餘額不足時整批立刻停下。
- 中斷後重跑同一個指令即可，會從每篇停下的步驟接續，不重複花錢。
- 建議第一次加 `--limit 5` 先跑 5 篇看結果。

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
- DeepSeek 偶爾回傳壞掉的 JSON，`trial.py` 會重試 3 次。
- Kimi 每篇約 6 分鐘，5 並行跑 200 篇約 4 小時。
- `trial_canary.py`（試產用）的邏輯類埋雷用 Opus 產生，會用到 Claude 訂閱額度；量產的 `run.py canary` 已改用 Kimi。
- API 金鑰在 `../.env`（不進 git）：`DEEPSEEK_API_KEY`、`KIMI_API_KEY`。

## 檔案對照

| 檔案 | 用途 |
|---|---|
| `batch.py` | 本機階段：入庫、轉檔、重複性檢查、品質閘、去固定頁、狀態、放行 |
| `run.py` | 模型階段：寫卡、檢查、退回重寫、埋雷、結果統計、人工複核表 |
| `clean.py` | 去固定頁的規則 |
| `validate.py` | 程式硬檢查 `gate_v4`：schema、引文在全文、數字在同列引文 |
| `card_prompt_v3.md` | 寫卡 prompt（每個方法步驟附引文與章節） |
| `check_prompt.md` | 單卡檢查 prompt |
| `trial.py` | 試產：寫卡、程式檢查、模型檢查 |
| `trial_canary.py` | 埋雷與抓到率 |
| `judge_fable.py` | 三卡對比的審稿（選模型用）；也放了各家 API 的呼叫函式 |
| `vocab.yaml`、`judge/strategy_types.yaml` | 方法詞彙表、策略大類 |
| `convert.py`、`make_cards.py`、`gold_cards.py`、`eval_*.py` | POC 階段的腳本，量產不再使用 |
