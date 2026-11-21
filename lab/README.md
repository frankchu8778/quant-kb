# lab：試產與實驗的封存

2026-11-21 從正式流程搬出來。這裡是選模型、定 prompt、驗證防呆時留下的過程紀錄，**正式流程不讀寫這裡**，只有兩個例外會讀：

- `audit.py calibrate`（審計員校準）讀 `cards/trial/`、`eval/trial/`、`fulltext/poc/clean-v2/` 裡的已知答案。
- `run.py review --trial`、`run.py triage --trial` 回頭看試產的卡。

路徑由 `pipeline/common.py` 的 `LAB` 與 `pipeline/trial.py` 的 `SRC`、`CARDS`、`EVAL` 指定。

`pipeline/` 裡的腳本是封存，不保證能直接執行（它們假設自己在 `pipeline/` 下）。要重跑時複製回 `pipeline/`。

| 位置 | 是什麼 |
|---|---|
| `docs/PLAN_POC.md`、`docs/POC_REPORT.md` | 試產計畫與報告 |
| `pipeline/RUNLOG.md` | 試產夜間執行紀錄（含排錯） |
| `cards/trial/` | 試產 20 篇，各寫卡模型的卡（kimi、deepseek、opus…）與 `_canary/` |
| `cards/poc-models/` | 更早的 POC：minimax、qwen、sonnet 各版本的卡、失敗紀錄 |
| `cards/_render_test/` | 卡片轉 Markdown 的測試 |
| `eval/trial/` | 試產的檢查結果（`check-deepseek-flash/` 等） |
| `eval/v4/` | v4 卡片格式的檢索評估 |
| `eval/fable/`、`eval/opus-high/`、`eval/deepseek-flash-high/` | 選檢查模型時的比較 |
| `eval/_notes_ab/` | 獨立筆記有沒有用的對照（結論：有筆記 5/6、沒筆記 0/6） |
| `eval/_stability/` | 檢查員判定穩定度（同卡檢查 3 次） |
| `eval/_kimi_batch_probe/` | Kimi batch 格式試打 |
| `fulltext/poc/basic`、`standard`、`clean-v2`、`clean-v2-standard` | 試產的轉檔方式比較；`clean-v2` 是試產卡片當初對照的全文 |
| `pipeline/*.py`、`*.sh` | 試產用的評估與比較腳本：`compare_judges`、`eval_cards`、`eval_retrieval`、`gold_cards`、`make_cards`、`render_cards`、`check_conversion`、`convert`、`serve_qwen`、`try_serve` |
| `pipeline/card_prompt.md`、`card_prompt_v2.md` | 舊版寫卡 prompt（現行是 `pipeline/card_prompt_v3.md`） |
| `pipeline/v4_questions.yaml` | 檢索評估的題目 |
| `pipeline/_mineru_test/` | 轉檔測試 |
| `pipeline/results/` | 試產的評分與人工複核結果 |
