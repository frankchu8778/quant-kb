#!/usr/bin/env bash
# 排錯用：用指定參數啟動 vLLM，等到 ready 或失敗，回報結果後關閉服務
# 用法：bash pipeline/try_serve.sh <標籤> [環境變數=值 ...] -- [vllm 參數...]
LABEL=$1; shift
ENVS=(); while [[ $# -gt 0 && $1 != "--" ]]; do ENVS+=("$1"); shift; done; shift || true
LOG=$HOME/quant-kb/pipeline/logs/try-$LABEL.log; : > "$LOG"
env "${ENVS[@]}" bash $HOME/quant-kb/pipeline/serve_qwen.sh "$LOG" "$@" &
PID=$!
for i in $(seq 1 240); do
  if curl -s -m 2 localhost:8000/v1/models >/dev/null; then echo "[$LABEL] READY"; grep -E "KV cache size|Maximum concurrency" "$LOG" | tail -2; exit 0; fi
  if ! kill -0 $PID 2>/dev/null; then echo "[$LABEL] DIED"; dmesg 2>/dev/null | grep -E "invalid opcode|fatal signal" | tail -1; grep -E "rror" "$LOG" | grep -v "DeepGEMM PDL" | tail -4; exit 1; fi
  sleep 2
done
echo "[$LABEL] TIMEOUT"; exit 2
