# 量產操作手冊

一批研報從 PDF 到審過的卡片，照這份順序跑。設計理由與成本估算見 `../PLAN.md`。

Python 一律用 `~/venvs/kb-parse/bin/python`，在 `pipeline/` 下執行。`<batch>` 是 `inbox/` 下的資料夾名稱。

## 階段總覽

| 階段 | 指令 | 花錢嗎 | 狀態 |
|---|---|---|---|
| 0 入庫 | `batch.py ingest <batch>` | 否 | 可用 |
| 1 轉檔 | `batch.py convert <batch>` | 否（本機 GPU） | 可用，新批次還沒實跑過 MinerU |
| 1b 品質閘 | `batch.py quality <batch>` | 否 | 可用 |
| 2 去固定頁 | `batch.py clean <batch>` | 否 | 可用 |
| — 預算確認 | `batch.py status <batch> --budget N` | 否 | 可用 |
| 4 寫卡＋程式檢查 | `trial.py write` | **是** | 只接了 POC 的 20 篇，要改成吃批次 |
| 6 模型檢查 | `trial.py check` | **是** | 同上 |
| 6b 退回重寫再複檢 | — | **是** | 未實作 |
| 9 發布 | `render_cards.py` | 否 | 還是舊卡片格式，未更新 |

分流（階段 3）、升級（7）、抽樣審計（8）見 `../PLAN.md`，還沒有腳本。

## 不花錢的部分：照順序跑

### 0. 入庫

把 PDF 放進 `inbox/<batch>/`，檔名用 `YYYYMMDD-券商-標題.pdf`。

```bash
python batch.py ingest batch1
```

- 用檔案內容的雜湊去重，**跨批次**都算：同一份 PDF 換檔名再放一次，只會記在原紀錄的 `duplicates`。
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

## 花錢的部分：尚未接上批次

目前 `trial.py` 的來源寫死在 `fulltext/poc/clean-v2`。接上批次時要做的事：

1. 來源改成 `fulltext/<batch>/clean/`，只處理 manifest 裡 `cleaned` 的篇，結果寫回 manifest。
2. 檢查員判 `revise` 或 `reject` 時，帶著問題清單重寫 1 次再複檢；仍不過的標成「待升級」。
3. 遇到餘額不足（HTTP 402）整批停下，不要繼續重試。
4. 累計花費超過 `--budget` 就停。

模型分工（試產結果，見 `../PLAN.md`）：寫卡 `kimi-k3.1-pro`，檢查 `deepseek-flash`，不同家。

## 已知的坑

- **POC 與試產的卡片用的是 basic 等級的全文**（`fulltext/poc/clean-v2` 是從 `basic` 清出來的），不是 standard。新批次預設用 standard，行內公式的寫法會不同（`$R^{2}$` 對 `$R ^ { 2 }$`）。第一批跑完要確認程式檢查的引文比對通過率沒有掉。
- DeepSeek 偶爾回傳壞掉的 JSON，`trial.py` 會重試 3 次。
- Kimi 每篇約 6 分鐘，5 並行跑 200 篇約 4 小時。
- `trial_canary.py` 的邏輯類埋雷用 Opus 產生，會用到 Claude 訂閱額度。
- API 金鑰在 `../.env`（不進 git）：`DEEPSEEK_API_KEY`、`KIMI_API_KEY`。

## 檔案對照

| 檔案 | 用途 |
|---|---|
| `batch.py` | 本機階段：入庫、轉檔、品質閘、去固定頁、狀態 |
| `clean.py` | 去固定頁的規則 |
| `validate.py` | 程式硬檢查 `gate_v4`：schema、引文在全文、數字在同列引文 |
| `card_prompt_v3.md` | 寫卡 prompt（每個方法步驟附引文與章節） |
| `check_prompt.md` | 單卡檢查 prompt |
| `trial.py` | 試產：寫卡、程式檢查、模型檢查 |
| `trial_canary.py` | 埋雷與抓到率 |
| `judge_fable.py` | 三卡對比的審稿（選模型用）；也放了各家 API 的呼叫函式 |
| `vocab.yaml`、`judge/strategy_types.yaml` | 方法詞彙表、策略大類 |
| `convert.py`、`make_cards.py`、`gold_cards.py`、`eval_*.py` | POC 階段的腳本，量產不再使用 |
