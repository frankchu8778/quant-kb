"""用 Claude Sonnet（透過 claude CLI，使用 Pro 訂閱額度）產生 POC 標準卡片。

用法：python gold_cards.py --src fulltext/poc/clean --out cards/poc/sonnet

每篇開一個全新 headless session（不帶工具、精簡 system prompt），
記錄 token 用量與耗時到 <id>.meta.json。已存在的卡片會跳過。
遇到額度限制會等待後重試，不會中斷整批。
"""
import argparse
import json
import subprocess
import time
from pathlib import Path

from common import KB, build_prompt, extract_json, load_vocab
from validate import validate_card

SYSTEM = "你是严谨的量化金融研究员。严格按用户要求只输出一个 JSON 对象，不要输出任何其他文字。"
RATE_LIMIT_HINTS = ("rate limit", "usage limit", "limit reached", "429", "overloaded")


def run_claude(prompt, model):
    cmd = ["claude", "-p", "--model", model, "--tools", "", "--system-prompt", SYSTEM,
           "--strict-mcp-config", "--output-format", "json", "--no-session-persistence"]
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=1800)
    try:
        return json.loads(p.stdout), p.stderr
    except json.JSONDecodeError:
        return None, (p.stdout + "\n" + p.stderr)[-2000:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--wait", type=int, default=900, help="撞到額度限制時等待秒數")
    ap.add_argument("--max-waits", type=int, default=24)
    ap.add_argument("--prompt", default="card_prompt.md", help="prompt 範本檔（pipeline/ 下）")
    ap.add_argument("--only", nargs="*", help="只處理這些 id")
    args = ap.parse_args()

    vocab = load_vocab()
    src, out = KB / args.src, KB / args.out
    out.mkdir(parents=True, exist_ok=True)
    waits = 0
    mds = sorted(src.glob("*.md"))
    if args.only:
        mds = [m for m in mds if m.stem in args.only]
    for md in mds:
        rid = md.stem
        if (out / f"{rid}.json").exists():
            print(f"skip  {rid}", flush=True)
            continue
        prompt = build_prompt(vocab, rid, md.read_text(), args.prompt)
        while True:
            t0 = time.time()
            res, err = run_claude(prompt, args.model)
            text = (res or {}).get("result") or ""
            limited = (res is None or res.get("is_error")) and any(
                h in (err + text).lower() for h in RATE_LIMIT_HINTS)
            if limited and waits < args.max_waits:
                waits += 1
                print(f"rate-limited, wait {args.wait}s ({waits}/{args.max_waits})  {rid}", flush=True)
                time.sleep(args.wait)
                continue
            break
        meta = {"engine": f"claude-{args.model}", "src": str(md.relative_to(KB)),
                "prompt_chars": len(prompt), "seconds": round(time.time() - t0, 1)}
        if res:
            meta.update(usage=res.get("usage"), total_cost_usd_api_equiv=res.get("total_cost_usd"),
                        model_usage=res.get("modelUsage"), is_error=res.get("is_error"))
        (out / f"{rid}.raw.txt").write_text(text or err)
        try:
            card = extract_json(text)
            meta["validation_errors"] = validate_card(card, vocab)
            (out / f"{rid}.json").write_text(json.dumps(card, ensure_ascii=False, indent=2))
            status = "ok" if not meta["validation_errors"] else f"ok-with-{len(meta['validation_errors'])}-issues"
        except Exception as e:
            meta["error"] = f"{type(e).__name__}: {e}"
            status = "error"
        (out / f"{rid}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        print(f"{status:>20}  {rid}  {meta['seconds']}s", flush=True)


if __name__ == "__main__":
    main()
