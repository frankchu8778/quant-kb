"""V3 自動評分：各引擎卡片 vs 標準卡片（Sonnet），並檢查每張卡片的數字與引用是否有原文根據。

用法：python eval_cards.py --gold cards/poc/sonnet --engines cards/poc/minimax cards/poc/qwen --src fulltext/poc/clean-v2
輸出：pipeline/results/v3_auto.json（逐篇明細）、pipeline/results/v3_auto.md（彙總表）
"""
import argparse
import json
from pathlib import Path

from common import KB, PIPELINE, load_vocab
from validate import grounding_report, validate_card


def load_cards(d):
    return {p.stem: json.loads(p.read_text()) for p in sorted(d.glob("*.json")) if not p.name.endswith(".meta.json")}


def load_meta(d):
    return {p.name[:-len(".meta.json")]: json.loads(p.read_text()) for p in sorted(d.glob("*.meta.json"))}


def evaluate(name, cards, metas, gold, src, vocab):
    rows = {}
    for rid in sorted(set(gold) | set(cards) | set(metas)):
        c, g = cards.get(rid), gold.get(rid)
        row = {"has_card": c is not None}
        if c is None:
            row["error"] = (metas.get(rid) or {}).get("error", "no card")
            rows[rid] = row
            continue
        ft = (src / f"{rid}.md").read_text()
        errs = validate_card(c, vocab)
        gr = grounding_report(c, ft)
        row.update({
            "format_ok": not any(e.startswith("缺欄位") or "非法" in e for e in errs),
            "vocab_violations": [e for e in errs if "不在詞彙表" in e],
            "quotes_found": gr["quotes_found"], "quotes_total": gr["quotes_total"],
            "quotes_missing": gr["quotes_missing"], "numbers_missing": gr["numbers_missing"],
            "value_level": c.get("value_level"),
        })
        if g is not None:
            gm, cm = set(g.get("methods") or []), set(c.get("methods") or [])
            row.update({
                "primary_family_match": c.get("primary_family") == g.get("primary_family"),
                "top_method_match": (c.get("methods") or [None])[0] == (g.get("methods") or [None])[0],
                "method_recall_vs_gold": round(len(gm & cm) / len(gm), 3) if gm else None,
                "value_level_match": c.get("value_level") == g.get("value_level"),
                "gold_primary_family": g.get("primary_family"), "primary_family": c.get("primary_family"),
            })
        m = metas.get(rid) or {}
        u = m.get("usage") or {}
        row.update({"seconds": m.get("seconds"), "input_tokens": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0),
                    "output_tokens": u.get("output_tokens")})
        rows[rid] = row
    return rows


def summarize(rows, has_gold):
    ok = [r for r in rows.values() if r.get("has_card")]
    n = len(rows)
    pct = lambda xs: round(100 * sum(xs) / len(xs), 1) if xs else None
    s = {
        "cards": f"{len(ok)}/{n}",
        "format_ok_pct": pct([r["format_ok"] for r in ok]),
        "vocab_violations": sum(len(r["vocab_violations"]) for r in ok),
        "quotes_found_pct": pct([1] * sum(r["quotes_found"] for r in ok) + [0] * sum(r["quotes_total"] - r["quotes_found"] for r in ok)),
        "numbers_missing_total": sum(len(r["numbers_missing"]) for r in ok),
        "cards_with_missing_numbers": sum(1 for r in ok if r["numbers_missing"]),
        "avg_seconds": round(sum(r["seconds"] or 0 for r in ok) / max(len(ok), 1), 1),
        "avg_input_tokens": round(sum(r["input_tokens"] or 0 for r in ok) / max(len(ok), 1)),
        "avg_output_tokens": round(sum(r["output_tokens"] or 0 for r in ok) / max(len(ok), 1)),
    }
    if has_gold:
        g = [r for r in ok if "primary_family_match" in r]
        s.update({
            "primary_family_match_pct": pct([r["primary_family_match"] for r in g]),
            "top_method_match_pct": pct([r["top_method_match"] for r in g]),
            "method_recall_vs_gold_pct": round(100 * sum(r["method_recall_vs_gold"] or 0 for r in g) / max(len(g), 1), 1),
            "value_level_match_pct": pct([r["value_level_match"] for r in g]),
        })
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument("--engines", nargs="+", required=True)
    ap.add_argument("--src", required=True)
    args = ap.parse_args()
    vocab, src = load_vocab(), KB / args.src
    gold_dir = KB / args.gold
    gold = load_cards(gold_dir)
    report = {}
    for d in [args.gold] + args.engines:
        path = KB / d
        rows = evaluate(path.name, load_cards(path), load_meta(path), gold if d != args.gold else {}, src, vocab)
        report[path.name] = {"summary": summarize(rows, d != args.gold), "per_report": rows}
    out = PIPELINE / "results"
    out.mkdir(exist_ok=True)
    (out / "v3_auto.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    keys = list(dict.fromkeys(k for v in report.values() for k in v["summary"]))
    lines = ["| 指標 | " + " | ".join(report) + " |", "|---|" + "---|" * len(report)]
    for k in keys:
        lines.append(f"| {k} | " + " | ".join(str(v["summary"].get(k, "")) for v in report.values()) + " |")
    (out / "v3_auto.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
