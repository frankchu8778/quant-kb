"""V4 端到端檢索測試：agent 只靠「卡片 + 全文 + CLAUDE.md」能不能找到該找的研報。

用法：python eval_retrieval.py --cards cards/poc/sonnet --variant sonnet
  1. 建隔離沙盒 eval/v4/<variant>/kb：cards/*.md、catalog.jsonl、fulltext/*.md、CLAUDE.md
     （agent 看不到 pipeline、其他引擎的卡片或標準答案）
  2. 每題用 claude -p（Sonnet，只開 Read/Grep/Glob）在沙盒裡作答，要求最後輸出 JSON
  3. 計算召回率（must）、誤報（不在 must ∪ ok）、單次呼叫 context 峰值
結果：pipeline/results/v4_<variant>.json；已有答案的題目會跳過（續跑）
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time

import yaml

from common import KB, PIPELINE

PROMPT = """{q}

请按本目录 CLAUDE.md 的检索步骤，在知识库中查找相关研报并回答。
最后单独输出一行 JSON（不要放在代码块里）：{{"relevant_ids": ["卡片id", ...], "answer": "简要回答"}}
relevant_ids 只列出与问题实质相关的研报。"""


def build_sandbox(cards_json_dir, variant):
    box = KB / "eval" / "v4" / variant / "kb"
    if box.exists():
        return box
    rendered = KB / "eval" / "v4" / variant / "_rendered"
    subprocess.run([sys.executable, str(PIPELINE / "render_cards.py"), "--src", str(cards_json_dir.relative_to(KB)),
                    "--out", str(rendered.relative_to(KB)), "--fulltext", "fulltext"], check=True, cwd=PIPELINE)
    (box / "cards").mkdir(parents=True)
    for p in rendered.glob("*.md"):
        shutil.copy(p, box / "cards" / p.name)
    shutil.copy(rendered / "catalog.jsonl", box / "catalog.jsonl")
    shutil.copytree(KB / "fulltext" / "poc" / "clean-v2", box / "fulltext",
                    ignore=shutil.ignore_patterns("_*", "images"))
    shutil.copy(PIPELINE / "kb_CLAUDE.md", box / "CLAUDE.md")
    return box


def ask(box, q, model):
    cmd = ["claude", "-p", "--model", model, "--tools", "Read,Grep,Glob", "--allowedTools", "Read,Grep,Glob",
           "--strict-mcp-config", "--output-format", "json", "--no-session-persistence"]
    t0 = time.time()
    p = subprocess.run(cmd, input=PROMPT.format(q=q), capture_output=True, text=True, cwd=box, timeout=1800)
    res = json.loads(p.stdout)
    text = res.get("result") or ""
    m = re.findall(r"\{[^{}]*\"relevant_ids\"[^{}]*\}", text, flags=re.S)
    parsed = json.loads(m[-1]) if m else {"relevant_ids": [], "answer": "", "parse_error": True}
    iters = (res.get("usage") or {}).get("iterations") or []
    peak = max((i.get("input_tokens", 0) + i.get("cache_read_input_tokens", 0) + i.get("cache_creation_input_tokens", 0)
                for i in iters), default=None)
    return {"relevant_ids": parsed.get("relevant_ids", []), "answer": parsed.get("answer", ""),
            "parse_error": parsed.get("parse_error", False), "num_turns": res.get("num_turns"),
            "peak_context_tokens": peak, "usage": res.get("usage"), "cost_usd_api_equiv": res.get("total_cost_usd"),
            "seconds": round(time.time() - t0, 1), "raw_result": text}


def score(item, ans):
    must, ok = set(item["must"]), set(item["must"]) | set(item.get("ok") or [])
    got = set(ans["relevant_ids"])
    return {"recall": round(len(got & must) / len(must), 3), "missed": sorted(must - got),
            "false_positives": sorted(got - ok)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards", required=True, help="JSON 卡片資料夾（相對 KB）")
    ap.add_argument("--variant", required=True)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    box = build_sandbox(KB / args.cards, args.variant)
    qs = yaml.safe_load((PIPELINE / "v4_questions.yaml").read_text())
    out = PIPELINE / "results" / f"v4_{args.variant}.json"
    out.parent.mkdir(exist_ok=True)
    results = json.loads(out.read_text()) if out.exists() else {}
    for item in qs:
        if (args.only and item["id"] not in args.only) or item["id"] in results:
            continue
        try:
            ans = ask(box, item["q"], args.model)
            ans.update(score(item, ans))
        except Exception as e:
            ans = {"error": f"{type(e).__name__}: {e}"}
        results[item["id"]] = {"lang": item["lang"], "q": item["q"], **ans}
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(item["id"], {k: ans.get(k) for k in ("recall", "missed", "false_positives", "peak_context_tokens", "seconds", "error")}, flush=True)


if __name__ == "__main__":
    main()
