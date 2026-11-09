# 執行紀錄（POC 夜間執行）

按時間順序記錄做了什麼、遇到什麼問題、怎麼處理。最終結論見 `POC_REPORT.md`。

## 1. 準備

- 建立 git repo（`.env`、PDF、圖片不進 git）
- POC 研報 20 篇、共 33MB → 推算全庫 1.3GB 約 **800 篇**
- POC 組成：多因子系列 12 篇（財通「拾穗」）、機器學習 3 篇、量化觀市週報 4 篇、CTA（混沌理論）1 篇
  - 偏多因子；缺組合優化、另類數據類 → V3 結論對這兩類的代表性有限
  - 資料夾裡有一個 `*.pdf:Zone.Identifier`（Windows 複製時產生的附加檔），未刪除，pipeline 會忽略

## 2. MiniMax 連線（V0）

- 測試呼叫成功：`MiniMax-M3.1-Flash`，回答正確
- 有 thinking 輸出（88 token），`.env` 的 `MINIMAX_EFFORT=max` 目前沒有傳給 API（找不到對應參數，用預設值）

## 3. 模型選擇（偏離原計畫）

- 原計畫：Qwen3-30B-A3B 4-bit
- 實際：**`nvidia/Qwen3.6-35B-A3B-NVFP4`（約 22GiB）**
  - Qwen3.6 是 2026-04 的新一代，同為 3B 啟用參數的 MoE
  - NVIDIA 官方量化，FP4 是 5090（Blackwell）原生格式
  - 混合注意力（40 層中只有 10 層完整注意力），長 context 的 KV cache 很小
- 備案：`QuantTrio/Qwen3.6-35B-A3B-AWQ`（25.5GB）

## 4. 環境安裝問題

| 問題 | 原因 | 處理 |
|---|---|---|
| `kb-parse` 安裝失敗 | 兩個 uv 安裝同時跑，搶快取鎖 | 改為依序安裝 |
| PyPI 版 vLLM 0.33.0 不能用 GPU | 綁定 torch 2.14+**cu130**，驅動 577 只支援到 CUDA 12.9，實際運算報「driver too old」 | 另建 `~/venvs/kb-llm-cu129`，安裝 vLLM **0.32.0+cu129** 官方 wheel。`~/venvs/kb-llm` 保留未刪，但**不可用** |
| FlagEmbedding 會把 torch 升到 cu13 版 | 同上 | 用 constraints 鎖定 `torch==2.11.0+cu128` |

> 根本解法：Windows 端把 NVIDIA 驅動升級到支援 CUDA 13 的版本（580 以上），之後就能直接用最新版套件。這需要你在 Windows 上操作。
