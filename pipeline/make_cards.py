"""用 Qwen（本地 vLLM）或 MiniMax（雲端）產生研報卡片。

用法：
  python make_cards.py --engine qwen    --src fulltext/poc/clean --out cards/poc/qwen
  python make_cards.py --engine minimax --src fulltext/poc/clean --out cards/poc/minimax

已存在的卡片會跳過（不覆蓋），中斷後重跑即可續跑。
每篇輸出：<id>.json（卡片）、<id>.raw.txt（模型原始輸出）、<id>.meta.json（耗時、token 用量、錯誤）
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import KB, build_prompt, card_schema, extract_json, load_env, load_vocab
from validate import validate_card


def call_qwen(prompt, schema, args):
    from openai import OpenAI
    client = OpenAI(base_url="http://localhost:8000/v1", api_key="none", timeout=1800)
    r = client.chat.completions.create(
        model="qwen-local",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2, max_tokens=16000 if args.thinking else 6000,  # thinking 也算在輸出 token 內
        response_format={"type": "json_schema",
                         "json_schema": {"name": "card", "schema": schema, "strict": True}},
        extra_body={"chat_template_kwargs": {"enable_thinking": args.thinking}},
    )
    u = r.usage
    return r.choices[0].message.content, {"input_tokens": u.prompt_tokens, "output_tokens": u.completion_tokens}


def call_minimax(prompt, schema, args):
    import anthropic
    env = load_env()
    client = anthropic.Anthropic(base_url=env["MINIMAX_BASE_URL"], api_key=env["MINIMAX_API_KEY"],
                                 timeout=1800, max_retries=3)
    # thinking 不設上限時會吃光全部輸出 token、來不及輸出 JSON（POC 實測 15999/16000）
    r = client.messages.create(model=env["MINIMAX_MODEL"], max_tokens=24000,
                               thinking={"type": "enabled", "budget_tokens": args.thinking_budget},
                               messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in r.content if b.type == "text")
    u = r.usage.model_dump() if hasattr(r.usage, "model_dump") else dict(r.usage)
    return text, u


ENGINES = {"qwen": call_qwen, "minimax": call_minimax}


def process(md: Path, out: Path, vocab, schema, args):
    rid = md.stem
    card_path = out / f"{rid}.json"
    if card_path.exists():
        return rid, "skip"
    prompt = build_prompt(vocab, rid, md.read_text(), args.prompt)
    meta = {"engine": args.engine, "src": str(md.relative_to(KB)), "prompt_chars": len(prompt)}
    t0 = time.time()
    try:
        text, usage = ENGINES[args.engine](prompt, schema, args)
        meta.update(usage=usage, seconds=round(time.time() - t0, 1))
        (out / f"{rid}.raw.txt").write_text(text)
        card = extract_json(text)
        meta["validation_errors"] = validate_card(card, vocab)
        card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2))
        status = "ok" if not meta["validation_errors"] else f"ok-with-{len(meta['validation_errors'])}-issues"
    except Exception as e:  # 單篇失敗不中斷整批，錯誤記錄在 meta
        meta.update(error=f"{type(e).__name__}: {e}", seconds=round(time.time() - t0, 1))
        status = "error"
    (out / f"{rid}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return rid, status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=list(ENGINES), required=True)
    ap.add_argument("--src", required=True, help="全文 Markdown 資料夾（相對 KB）")
    ap.add_argument("--out", required=True, help="卡片輸出資料夾（相對 KB）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--thinking", action="store_true", help="Qwen 開啟 thinking 模式")
    ap.add_argument("--thinking-budget", type=int, default=8000, help="MiniMax thinking token 上限")
    ap.add_argument("--prompt", default="card_prompt.md", help="prompt 範本檔（pipeline/ 下）")
    ap.add_argument("--only", nargs="*", help="只處理這些 id")
    args = ap.parse_args()

    vocab, schema = load_vocab(), card_schema(load_vocab())
    src, out = KB / args.src, KB / args.out
    out.mkdir(parents=True, exist_ok=True)
    mds = sorted(src.glob("*.md"))
    if args.only:
        mds = [m for m in mds if m.stem in args.only]
    with ThreadPoolExecutor(args.workers) as ex:
        for rid, status in ex.map(lambda m: process(m, out, vocab, schema, args), mds):
            print(f"{status:>20}  {rid}", flush=True)


if __name__ == "__main__":
    main()
