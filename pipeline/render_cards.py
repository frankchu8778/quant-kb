"""JSON 卡片 → 知識庫用的 Markdown 卡片（YAML frontmatter + 正文），並產生 catalog.jsonl 總目錄。

用法：python render_cards.py --src cards/poc/sonnet --out cards/kb-sonnet --fulltext fulltext/poc/clean-v2

frontmatter 的 methods 用「id/中文」雙語形式；keywords 展開中文名稱與同義詞（含英文、繁體），
讓 agent 不論用簡體、繁體或英文 grep 都找得到。
"""
import argparse
import json

import yaml

from common import KB, load_vocab


def render(rid, c, vocab, fulltext_rel, source_pdf):
    fams, meths = vocab["families"], vocab["methods"]
    keywords = []
    for m in c.get("methods") or []:
        v = meths.get(m, {})
        keywords += [v.get("zh", m), *map(str, v.get("aliases", []))]
    for f in c.get("families") or []:
        v = fams.get(f, {})
        keywords += [v.get("zh", f), *map(str, v.get("aliases", []))]
    keywords += c.get("new_tag_suggestions") or []
    fm = {
        "id": rid,
        "title": c.get("title"), "broker": c.get("broker"), "date": c.get("date"), "series": c.get("series"),
        "source_pdf": source_pdf, "fulltext": fulltext_rel,
        "report_type": c.get("report_type"),
        "value_level": c.get("value_level"),
        "reproducibility": c.get("reproducibility"),
        "primary_family": f"{c.get('primary_family')}/{fams.get(c.get('primary_family'), {}).get('zh', '')}",
        "families": [f"{f}/{fams.get(f, {}).get('zh', '')}" for f in c.get("families") or []],
        "methods": [f"{m}/{meths.get(m, {}).get('zh', '')}" for m in c.get("methods") or []],
        "data_needed": c.get("data_needed") or [],
        "keywords": list(dict.fromkeys(keywords)),
    }
    out = ["---", yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=1000).strip(), "---", ""]
    out += [f"# {c.get('title')}", "", "## 核心观点", "", c.get("summary") or "", "",
            "## 创新点", "", c.get("core_idea") or "", "", f"> 方法论价值：{c.get('value_level')} — {c.get('value_reason')}", "",
            "## 方法步骤", ""]
    out += [f"{i}. {s}" for i, s in enumerate(c.get("method_steps") or [], 1)] + [""]
    if c.get("key_formulas"):
        out += ["## 核心公式", ""]
        for f in c["key_formulas"]:
            out += [f"- {f.get('meaning')}", "", f"  $$ {f.get('latex')} $$", ""]
    if c.get("key_results"):
        out += ["## 关键结果", "", "| 指标 | 数值 | 说明 |", "|---|---|---|"]
        out += [f"| {r.get('metric')} | {r.get('value')} | {r.get('context')} |" for r in c["key_results"]] + [""]
    out += ["## 局限与注意", ""] + [f"- {l}" for l in c.get("limitations") or []] + [""]
    out += [f"## 可复现度：{c.get('reproducibility')}", "", c.get("reproducibility_reason") or "", ""]
    if c.get("evidence_quotes"):
        out += ["## 原文依据", ""] + [f"- {q.get('claim')}：「{q.get('quote')}」" for q in c["evidence_quotes"]] + [""]
    return "\n".join(out), fm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fulltext", required=True)
    args = ap.parse_args()
    vocab = load_vocab()
    src, out = KB / args.src, KB / args.out
    out.mkdir(parents=True, exist_ok=True)
    index = json.loads((KB / "fulltext" / "poc" / "basic" / "_index.json").read_text())
    catalog = []
    for p in sorted(src.glob("*.json")):
        if p.name.endswith(".meta.json"):
            continue
        rid = p.stem
        md, fm = render(rid, json.loads(p.read_text()), vocab, f"{args.fulltext}/{rid}.md",
                        f"inbox/poc/{index.get(rid, {}).get('source_pdf', '')}")
        (out / f"{rid}.md").write_text(md)
        catalog.append({k: fm[k] for k in ("id", "title", "broker", "date", "report_type", "value_level",
                                           "reproducibility", "primary_family", "methods")})
    (out / "catalog.jsonl").write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in catalog) + "\n")
    print(f"{len(catalog)} 張卡片 → {args.out}")


if __name__ == "__main__":
    main()
