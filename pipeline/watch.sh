#!/usr/bin/env bash
# 監控一個批次的 Kimi batch：每隔 POLL 秒查一次，有完成的就收回並接續後半段
# （程式檢查、DeepSeek 檢查〔尖峰時段會等到離峰〕、重寫、埋雷、審計），有新收回的就重新發布。
# 這個批次的 batch 全部收回（完成或被拒結案）才結束。
#
# 用法：bash watch.sh <batch> <每次 collect 的預算> [POLL 秒數，預設 600]
# 中途被關掉也沒關係：重跑同一個指令即可，已收回、已檢查過的不會重複花錢。
set -u
cd "$(dirname "$0")"
P=/home/newt/venvs/kb-parse/bin/python
BATCH=$1
BUDGET=$2
POLL=${3:-600}

open_count() {
  $P -c "import json; print(sum(1 for e in json.load(open('../catalog/kimi_batches.json')) if e['batch'] == '$BATCH' and not e['collected']))"
}

while :; do
  echo "== $(date '+%F %T') 查 $BATCH 的 batch =="
  before=$(open_count)
  $P -u run.py collect "$BATCH" --budget "$BUDGET" --wait-offpeak
  after=$(open_count)
  if [ "$after" != "$before" ]; then
    $P publish.py reviews "$BATCH" && $P publish.py publish "$BATCH"
  fi
  if [ "$after" = 0 ]; then
    echo "== $(date '+%F %T') $BATCH 的 batch 全部收回，監控結束 =="
    break
  fi
  echo "還有 $after 個 batch 沒完成，$POLL 秒後再查"
  sleep "$POLL"
done
