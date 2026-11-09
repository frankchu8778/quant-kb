#!/usr/bin/env bash
# 用本機 vLLM 跑 MinerU2.5-Pro VLM（port 8001），給 MinerU standard 等級使用
# 環境設定同 serve_qwen.sh（CUDA 12.9 ptxas、eager 模式）
set -euo pipefail
LOG=${1:-$HOME/quant-kb/pipeline/logs/vllm-mineru-vlm.log}
export C_INCLUDE_PATH=$(ls -d $HOME/.local/share/uv/python/cpython-3.12*/include/python3.12 | head -1)
PTXAS_129=$HOME/venvs/kb-llm-cu129/lib/python3.12/site-packages/nvidia/cuda_nvcc/bin/ptxas
export TRITON_PTXAS_PATH=$PTXAS_129 TRITON_PTXAS_BLACKWELL_PATH=$PTXAS_129
export TRITON_CACHE_DIR=$HOME/.cache/triton-cu129
exec $HOME/venvs/kb-llm-cu129/bin/vllm serve $HOME/.mineru/models/MinerU2.5-Pro-2605-1.2B \
  --served-model-name mineru-vlm --port 8001 \
  --gpu-memory-utilization 0.5 --enforce-eager >> "$LOG" 2>&1
