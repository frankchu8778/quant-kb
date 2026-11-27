#!/usr/bin/env bash
# 監控一個批次的 Kimi batch：每隔 POLL 秒查一次，有完成的就收回並接續後半段
# （程式檢查、DeepSeek 檢查〔尖峰時段會等到離峰〕、重寫、埋雷、審計），有新收回的就重新產生複核表、
# 把已填的人工判定寫回 manifest、重新發布；複核表裡還有沒填的，會印出提醒。
# 這個批次的 batch 全部收回（完成或被拒結案）才結束。
#
# 用法：bash watch.sh <batch> <每次 collect 的預算> [POLL 秒數，預設 600] [其他給 collect 的參數…]
#   例：bash watch.sh batch1 25 600 --ignore-alarms   # 看過監控警報、決定繼續時
# 中途被關掉也沒關係：重跑同一個指令即可，已收回、已檢查過的不會重複花錢。
set -u
cd "$(dirname "$0")"
P=${KB_PYTHON:-$HOME/venvs/kb-parse/bin/python}   # 生產線用的 Python，可用 KB_PYTHON 改
BATCH=$1
BUDGET=$2
POLL=${3:-600}
shift $(( $# < 3 ? $# : 3 ))
EXTRA=("$@")

open_count() {
  $P -c "import json; print(sum(1 for e in json.load(open('../catalog/kimi_batches.json')) if e['batch'] == '$BATCH' and not e['collected']))"
}

remind_unjudged() {
  local f n
  for f in results/major_review_"$BATCH".md results/audit_review_"$BATCH".md; do
    [ -f "$f" ] || continue
    n=$(grep -c "人工判定：＿＿" "$f")
    [ "$n" -gt 0 ] && echo "!! $n 條等你判定：pipeline/$f（填好後下次收回會自動生效，或手動跑 publish.py reviews/publish）"
  done
  return 0
}

while :; do
  echo "== $(date '+%F %T') 查 $BATCH 的 batch =="
  before=$(open_count)
  $P -u run.py collect "$BATCH" --budget "$BUDGET" --wait-offpeak "${EXTRA[@]}"
  after=$(open_count)
  if [ "$after" != "$before" ]; then
    # 複核表：檢查員判的 critical/major、審計員與檢查員不一致的；重新產生時會保留已填的判定
    $P run.py review "$BATCH"
    $P audit.py review "$BATCH"
    $P publish.py reviews "$BATCH" && $P publish.py publish "$BATCH"
    remind_unjudged
  fi
  if [ "$after" = 0 ]; then
    echo "== $(date '+%F %T') $BATCH 的 batch 全部收回，監控結束 =="
    break
  fi
  echo "還有 $after 個 batch 沒完成，$POLL 秒後再查"
  sleep "$POLL"
done
