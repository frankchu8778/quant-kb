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

## 5. Qwen（vLLM）部署排錯

| # | 症狀 | 根本原因 | 處理 |
|---|---|---|---|
| 1 | `libnvrtc.so.13` 找不到，vLLM 啟動失敗 | `torchcodec` 從 PyPI 裝成 CUDA 13 版 | 換成 CPU 版 `torchcodec 0.17.0+cpu`（純文字用不到音訊/影片解碼） |
| 2 | Triton 編譯失敗：`Python.h` 不存在 | venv 底層是系統 Python，沒裝 `python3.12-dev` | `uv python install 3.12` 取得標頭檔，設 `C_INCLUDE_PATH`（不需 sudo） |
| 3 | EngineCore 當掉（SIGILL / SIGSEGV，位置在 `libcuda.so` 的 `cuModuleLoadData`） | Triton 對 Blackwell 用自帶的 `ptxas-blackwell`（**CUDA 13.1**），`tokenspeed_triton` 帶 13.4；編出的 kernel 驅動 577 載入時崩潰 | `TRITON_PTXAS_PATH` / `TRITON_PTXAS_BLACKWELL_PATH` 指向 venv 內 CUDA 12.9 的 ptxas；另設新的 Triton 快取目錄（舊快取保留） |
| 4 | CUDA graph 擷取失敗：找不到 `nvcc` | FlashInfer 需要現場編譯 | `--enforce-eager` |
| 5 | 第一個請求就讓引擎掛掉：找不到 `nvcc` | FlashInfer 注意力 prefill 需要現場編譯 | `--attention-backend TRITON_ATTN` |
| 6 | 同上 | FlashInfer 取樣器（top-k）需要現場編譯 | `VLLM_USE_FLASHINFER_SAMPLER=0` |

結果：可以正常服務，但**速度打折**（eager 模式、無 CUDA graph、Marlin W4A16 kernel）。單篇建卡 139 秒。
vLLM 也提示「GPU 不支援原生 FP4」，改用 Marlin 做 4-bit 權重運算（影響速度、不影響正確性）。

**根本解法（未執行，需你決定）**：
- A. 用 conda 在使用者目錄裝 CUDA 12.9 toolkit（`nvcc`），不需 sudo → FlashInfer、CUDA graph 都能用，預期快數倍
- B. Windows 端升級 NVIDIA 驅動到 580 以上（支援 CUDA 13）→ 直接用最新 PyPI 套件，上面 6 個問題大多消失

## 6. MinerU 4

- MinerU 已是 4.x，CLI 與 PLAN.md 寫的 2.x 完全不同：用 `mineru-kit parse --tier basic|standard`
- 預設本機解析，`--remote` 才會上傳（我們沒用）
- standard 等級的 VLM 預設走 llama.cpp，但 llama.cpp 看不到 GPU → 改用 vLLM 在本機 port 8001 服務 MinerU2.5-Pro VLM（`serve_mineru_vlm.sh` + `mineru-standard.yaml`）
- 輸出的 Markdown 把圖片以 base64 內嵌（單篇 2MB）→ 整理時另存成圖片檔，正文只留連結
- 加入 NFKC 正規化：`𝑹𝟐` → `R2`，否則 grep 搜不到

## 7. 其他失誤與處理（照實記錄）

- 誤用 `python -I` 跑自己的腳本 → import 失敗，重跑
- 試跑 Sonnet 時漏加篇數限制，整批 20 篇被丟到背景且未追蹤 → 發現後終止該程序（未產生任何檔案），補上 `--only` 參數後重跑
- 一條指令誤夾 `rm -rf /dev/null`（本身無作用）→ 被權限系統擋下，未執行；之後所有指令不含刪除
- MiniMax 第一次試跑：thinking 吃光 16000 輸出 token，沒產出 JSON → 提高 `max_tokens` 到 24000、加 thinking 上限（MiniMax 似乎不理會上限，但總量夠用）。失敗結果移到 `cards/poc/_failed_attempts/`
