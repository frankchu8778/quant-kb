#!/usr/bin/env bash
# 啟動本地 Qwen（vLLM，OpenAI 相容 API：http://localhost:8000/v1，模型名 qwen-local）
# 用法：bash pipeline/serve_qwen.sh [log 檔路徑] [額外 vllm 參數...]
#
# 環境備註（見 RUNLOG.md）：
# - 用 kb-llm-cu129（vLLM 0.32.0+cu129）；PyPI 版綁 CUDA 13，驅動 577 不支援
# - venv 底層是系統 Python，缺 Python.h；Triton JIT 編譯時改用 uv 管理的 Python 3.12 標頭檔
set -euo pipefail
LOG=${1:-$HOME/quant-kb/pipeline/logs/vllm-qwen.log}
shift || true
export HF_HOME=$HOME/models/hf
export C_INCLUDE_PATH=$(ls -d $HOME/.local/share/uv/python/cpython-3.12*/include/python3.12 | head -1)
# Triton 對 Blackwell 預設用自帶的 ptxas-blackwell（CUDA 13.x），編出的 kernel 驅動 577 載入時會崩潰
# （libcuda cuModuleLoadData segfault / invalid opcode）→ 一律改用 CUDA 12.9 的 ptxas
PTXAS_129=$HOME/venvs/kb-llm-cu129/lib/python3.12/site-packages/nvidia/cuda_nvcc/bin/ptxas
export TRITON_PTXAS_PATH=$PTXAS_129 TRITON_PTXAS_BLACKWELL_PATH=$PTXAS_129
# FlashInfer 需要現場用 nvcc 編譯（CUDA graph 擷取、注意力 prefill 都會觸發），本機沒有完整 CUDA toolkit
#   → --enforce-eager 關掉 CUDA graph；--attention-backend TRITON_ATTN 避開 FlashInfer 注意力
# 獨立的 kernel 快取，避免讀到先前用 CUDA 13 ptxas 編出來的舊 kernel（舊快取保留不刪）
export TRITON_CACHE_DIR=$HOME/.cache/triton-cu129
# FlashInfer 取樣器同樣需要 nvcc 現場編譯 → 改用 PyTorch 原生取樣
export VLLM_USE_FLASHINFER_SAMPLER=0
exec $HOME/venvs/kb-llm-cu129/bin/vllm serve $HOME/models/qwen3.6-35b-a3b-nvfp4 \
  --served-model-name qwen-local --port 8000 \
  --max-model-len 65536 --gpu-memory-utilization 0.90 --kv-cache-dtype fp8 \
  --max-num-seqs 8 --reasoning-parser qwen3 \
  --enforce-eager --attention-backend TRITON_ATTN \
  --limit-mm-per-prompt '{"image":0,"video":0}' "$@" >> "$LOG" 2>&1
