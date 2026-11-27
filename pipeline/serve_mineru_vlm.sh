#!/usr/bin/env bash
# 用本機 vLLM 跑 MinerU2.5-Pro VLM（port 8001），給 MinerU standard 等級使用
# 環境設定同 serve_qwen.sh（CUDA 12.9 ptxas、eager 模式）
# ⚠ 下面的 venv、ptxas、模型路徑是作者機器（RTX 5090、驅動 577）的設定：VLLM_VENV、MINERU_VLM_MODEL 可改；
#   其他機器多半不需要 ptxas 與 C_INCLUDE_PATH 這兩項，或改用 MinerU 的 basic 等級（不用 VLM）。
set -euo pipefail
LOG=${1:-$(dirname "$(readlink -f "$0")")/logs/vllm-mineru-vlm.log}
VLLM_VENV=${VLLM_VENV:-$HOME/venvs/kb-llm-cu129}
export C_INCLUDE_PATH=$(ls -d $HOME/.local/share/uv/python/cpython-3.12*/include/python3.12 | head -1)
PTXAS_129=$VLLM_VENV/lib/python3.12/site-packages/nvidia/cuda_nvcc/bin/ptxas
export TRITON_PTXAS_PATH=$PTXAS_129 TRITON_PTXAS_BLACKWELL_PATH=$PTXAS_129
export TRITON_CACHE_DIR=$HOME/.cache/triton-cu129
exec $VLLM_VENV/bin/vllm serve ${MINERU_VLM_MODEL:-$HOME/.mineru/models/MinerU2.5-Pro-2605-1.2B} \
  --served-model-name mineru-vlm --port 8001 \
  --gpu-memory-utilization 0.5 --enforce-eager >> "$LOG" 2>&1
