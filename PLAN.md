# 量化研報知識庫：部署計畫

## 現況（2026-11-10 檢查）

- GPU：RTX 5090 32GB，驅動 577.00，CUDA 12.9（Blackwell，需用 cu128 以上的 wheel）
- 無 `nvcc`：用預編譯 wheel 不需要；若某套件要求 JIT 編譯再裝 CUDA toolkit
- 既有環境：`quant_research`（torch 2.9.1+cu128）、`alphagen`（torch 2.8.0+cu128）→ **不動它們**
- 硬碟：WSL 763G 可用；RAM 125G

## 環境規劃：兩個獨立環境，避免套件互相打架

| 環境 | 用途 | 位置 |
|---|---|---|
| `kb-llm` | vLLM + Qwen | `~/venvs/kb-llm` |
| `kb-parse` | MinerU + bge-m3 + pipeline 腳本 | `~/venvs/kb-parse` |

模型統一放 `~/models`（設 `HF_HOME=~/models/hf`），不放進知識庫目錄。

> GPU 輪流用：vLLM 啟動會吃掉約 90% VRAM，跑 MinerU / bge-m3 前要先關掉 vLLM。

```bash
# 加到 ~/.bashrc
export HF_HOME=~/models/hf
```

---

## 第一部分：Qwen 下載與部署（vLLM）

### 1.1 建環境、裝 vLLM

```bash
uv venv ~/venvs/kb-llm --python 3.12
source ~/venvs/kb-llm/bin/activate
uv pip install vllm            # 會自帶對應的 torch（cu128）
python -c "import vllm, torch; print(vllm.__version__, torch.__version__, torch.cuda.is_available())"
```

裝好後把版本記下來，之後固定版本（`uv pip freeze > ~/quant-kb/pipeline/kb-llm.lock`）。

### 1.2 下載模型（約 17GB）

目標：**Qwen3-30B-A3B-Instruct（非 thinking 版）4-bit 量化**（AWQ 或 GPTQ-Int4）。

```bash
uv pip install -U "huggingface_hub[cli]"
# 先上 Hugging Face 搜尋 "Qwen3-30B-A3B-Instruct AWQ"，確認 repo 名稱後再下載
# 優先順序：Qwen 官方量化版 > 下載量高的社群 AWQ 版
hf download <repo名稱> --local-dir ~/models/qwen3-30b-a3b-awq
```

> 下載慢可改用 ModelScope（`modelscope download --model <repo> --local_dir ...`）。
> 下載時確認是否有比 Qwen3 更新的同級版本（同為 30B MoE、4-bit 大小相近）。

### 1.3 啟動服務（OpenAI 相容 API）

```bash
vllm serve ~/models/qwen3-30b-a3b-awq \
  --served-model-name qwen-local \
  --max-model-len 40960 \
  --gpu-memory-utilization 0.90 \
  --kv-cache-dtype fp8 \
  --port 8000
```

- `max-model-len 40960`：一篇 25 頁研報約 3 萬 token，整篇一次讀完不切段
- 權重約 17GB，剩下 VRAM 放 KV cache，可同時處理約 5–6 篇

### 1.4 驗證

```bash
curl http://localhost:8000/v1/models
curl http://localhost:8000/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"qwen-local","messages":[{"role":"user","content":"用一句話解釋残差动量因子"}]}'
```

Python 調用（之後 pipeline 就是這樣用）：

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="none")
r = client.chat.completions.create(model="qwen-local",
        messages=[{"role": "user", "content": "..."}])
```

建卡時會用 vLLM 的 structured outputs（JSON schema）強制輸出格式與詞彙表。

### 1.5 可能遇到的問題

| 症狀 | 處理 |
|---|---|
| `no kernel image is available` | wheel 不支援 sm_120 → 升級 vLLM 到最新版 |
| 要求 `nvcc` / JIT 編譯失敗 | 安裝 CUDA toolkit 12.8+（WSL 版，不要裝驅動） |
| OOM | 降 `--max-model-len` 或 `--gpu-memory-utilization 0.85` |

---

## 第二部分：MinerU 與 bge-m3 部署

### 2.1 建環境

```bash
uv venv ~/venvs/kb-parse --python 3.12
source ~/venvs/kb-parse/bin/activate
uv pip install torch --index-url https://download.pytorch.org/whl/cu128
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

### 2.2 MinerU（PDF → Markdown）

```bash
uv pip install -U "mineru[core]"
mineru-models-download          # 選 huggingface 或 modelscope，模型約 3–5GB
```

測試一篇：

```bash
mineru -p ~/quant-kb/inbox/poc/某篇.pdf -o ~/quant-kb/pipeline/_test -b pipeline
```

MinerU 有兩種後端，POC 會兩個都試，比較公式和表格的品質：

| 後端 | 特性 |
|---|---|
| `pipeline` | 傳統版面分析 + OCR，穩定、快 |
| `vlm-*` | 用小型視覺語言模型解析，公式、表格通常比較好，但比較慢 |

輸出：每篇一個 `.md` + 圖片資料夾 + 版面 JSON。我們只取 `.md` 進 `fulltext/`。

### 2.3 bge-m3（embedding，第二階段才用，可以先裝好）

```bash
uv pip install -U FlagEmbedding
python -c "
from FlagEmbedding import BGEM3FlagModel
m = BGEM3FlagModel('BAAI/bge-m3', use_fp16=True)   # 首次執行自動下載約 2.3GB
print(m.encode(['残差动量', 'residual momentum'])['dense_vecs'].shape)
"
```

- bge-m3 同時輸出 dense（語意）和 sparse（關鍵字）向量，一個模型就能做 hybrid 搜尋
- 向量庫候選：LanceDB 或 sqlite-vec（都是單檔、免架伺服器）
- 選配：`BAAI/bge-reranker-v2-m3` 做重排序，提升精度

### 2.4 MiniMax（雲端，已設定）

`~/quant-kb/.env`（權限 600，已列入 `.gitignore`）：

| 變數 | 說明 |
|---|---|
| `MINIMAX_API_KEY` | 已填 |
| `MINIMAX_BASE_URL` | `https://api.minimaxi.com/anthropic`（Anthropic 相容格式） |
| `MINIMAX_MODEL` | **待補**：要用的模型名稱（依你的訂閱方案） |

因為是 Anthropic 相容格式，pipeline 直接用 `anthropic` SDK 呼叫，只要換 `base_url`：

```python
import anthropic, os
client = anthropic.Anthropic(base_url=os.environ["MINIMAX_BASE_URL"],
                             api_key=os.environ["MINIMAX_API_KEY"])
```

> 需確認：訂閱方案的額度（每 5 小時 / 每天多少次）、是否允許批次腳本呼叫。

---

## 第三部分：POC 流程（環境裝好、20 篇放進 `inbox/poc/` 之後）

1. MinerU 兩種後端各轉 20 篇 → 比較轉檔品質，選定後端（V1）
2. 規則去除免責聲明 / 目錄 / 券商簡介頁，記錄省下的 token 比例（V2）
3. Sonnet 產生 20 張**標準卡片**，你審核其中 5 張，同時用 `/usage` 實測額度消耗
4. Qwen、MiniMax 各產生 20 張卡片
5. 逐欄比對 → 決定建卡分工（V3）
6. 端到端檢索測試 → 決定是否需要 bge-m3 語意搜尋（V4）
7. 定稿卡片模板與方法論詞彙表 → 寫 `CLAUDE.md` 與檢索 Skill

所有驗證結果記錄在 `pipeline/poc_results.md`。

---

## 第四部分：驗證標準

> 樣本只有 20 篇，「≥ 90%」等於最多錯 2 篇。結果在邊緣時，補 10 篇再測，不要硬下結論。
> 「自動」= 用腳本檢查；「人工」= 你或 Claude 對照原文抽查。

### V0 環境驗證（安裝完成時）

| 項目 | 通過標準 | 方式 |
|---|---|---|
| vLLM 服務 | `/v1/models` 有回應，回傳 `qwen-local` | 自動 |
| vLLM 長文本 | 輸入 3 萬 token 的研報，能完整輸出卡片，不 OOM | 自動 |
| vLLM 並行 | 同時送 4 篇，全部成功 | 自動 |
| vLLM 結構化輸出 | 指定 JSON schema 時，20 次輸出 100% 能 parse | 自動 |
| MinerU GPU | 轉檔時 `nvidia-smi` 看得到 GPU 使用率；30 頁研報 < 2 分鐘 | 自動 |
| bge-m3 跨語言 | 「残差动量」與「residual momentum」的相似度 > 與「波动率」的相似度 | 自動 |
| MiniMax | 測試呼叫成功，記錄單次呼叫的額度消耗 | 自動 |

### V1 轉檔品質（MinerU 兩種後端各跑 20 篇）

| 項目 | 通過標準 | 方式 |
|---|---|---|
| 亂碼 | 每篇亂碼字元（`�`、私用區字元等）比例 < 0.5% | 自動 |
| 正文完整 | 每篇抽 3 頁對照 PDF，段落缺漏 = 0 | 人工 |
| 閱讀順序 | 多欄排版沒有左右欄交錯，抽查 ≥ 95% 正確 | 人工 |
| 公式 | 抽查公式 LaTeX 能正確渲染、符號和原文一致，≥ 90% | 人工 |
| 表格 | 主要結果表的行列結構正確，≥ 80% | 人工 |
| 掃描版 | OCR 後正文可讀，關鍵詞可以 grep 到 | 人工 |

**決定**：選總分較高的後端。如果某類研報兩種都不及格（例如掃描版），把這類標記成「需特別處理」，不影響整體決定。

### V2 去除固定頁

| 項目 | 通過標準 | 方式 |
|---|---|---|
| 誤刪正文 | 20 篇被刪掉的段落全部人工確認，誤刪 = 0 | 人工 |
| 節省比例 | 記錄 token 減少比例（預期 15–30%） | 自動 |

誤刪比少刪嚴重：寧可少刪，規則要保守。

### V3 卡片品質（Qwen、MiniMax 對照 Sonnet 標準卡片）

**前提**：你審核 5 張 Sonnet 標準卡片，確認標準本身沒問題，才拿它來比對。

| 欄位 / 項目 | 通過標準 | 方式 |
|---|---|---|
| 格式 | schema 合法、必填欄位齊全，100% | 自動 |
| 詞彙表 | 方法標籤全部來自詞彙表，詞彙表外標籤 = 0 | 自動 |
| **主要方法分類** | 和標準卡片一致 ≥ 90%（最多錯 2 篇） | 自動 |
| 次要方法標籤 | 標準卡片的標籤被涵蓋 ≥ 80% | 自動 |
| **數字不編造** | 卡片中每個數字（IC、年化等）都能在全文中找到，編造 = 0 | 自動 |
| 引用可驗證 | 卡片附的原文片段在全文中找得到（模糊比對），≥ 95% | 自動 |
| 公式 | 核心公式沒有抄錯；漏抄可以接受，抄錯不行 | 人工 |
| 核心觀點、方法步驟 | 每篇評分：2 = 正確且抓到重點，1 = 正確但表面，0 = 錯誤或遺漏。平均 ≥ 1.5，且 0 分 ≤ 2 篇 | 人工 |
| 低價值研報 | 短評、週報類被正確標記成低價值 | 人工 |
| 速度、成本 | 記錄每篇耗時；MiniMax 記錄額度消耗 | 自動 |

**決定規則**：

| 結果 | 建卡分工 |
|---|---|
| Qwen 及格 | Qwen 主力 + MiniMax 交叉驗證，標籤有分歧的交給 Sonnet 仲裁 |
| Qwen 不及格、MiniMax 及格 | MiniMax 主力，不常駐 vLLM |
| 兩者都不及格 | Sonnet 建卡，根據實測額度決定用 Pro 慢慢跑或改用 API |
| 只有「核心觀點」不及格 | 小模型只做抽取型欄位，觀點欄位留給 Sonnet 在查詢時處理 |

「數字不編造」和「詞彙表外標籤」是硬性條件，只要有一項不過，就算其他欄位都及格也不採用。

### V4 端到端檢索（最終目的：agent 找不找得到）

用 20 篇 POC 研報出 **10 個測試問題**，每題事先標好「應該找到哪幾篇」。例如：

- 哪些研報改進了動量因子？（繁體提問，原文是簡體）
- Which reports use machine learning for stock selection?（英文提問）
- 有沒有處理過因子擁擠度的方法？（概念型，原文用詞可能不同）

| 項目 | 通過標準 |
|---|---|
| 召回率 | 每題應找到的研報，agent 找到 ≥ 90% |
| 繁體 / 英文提問 | 召回率和簡體提問差距 ≤ 10 個百分點 |
| 引用正確 | agent 回答引用的研報和內容 100% 對得上原文 |
| context 用量 | 每題檢索消耗 < 3 萬 token |

**決定**：只用卡片加 grep 就達標 → 暫時不做語意搜尋。英文或概念型問題召回率不足 → 加入 bge-m3 hybrid 搜尋後重測。

## 目錄結構

```
~/quant-kb/
├── PLAN.md          本檔案
├── inbox/poc/       POC 的 20 篇 PDF
├── fulltext/        MinerU 轉出的全文 Markdown
├── cards/           研報卡片
├── topics/          主題綜述
├── catalog/         catalog.jsonl 總目錄、manifest
└── pipeline/        處理腳本、環境 lock 檔
```
