"""量產試產（PLAN.md 第一步）：寫卡 → 程式硬檢查 → 單卡模型檢查。

用法：
  python trial.py write --writer opus     [--only <id> ...]
  python trial.py write --writer deepseek [--only <id> ...]
  python trial.py gate                     # 各寫卡模型的程式檢查結果
  python trial.py check --checker deepseek-flash [--writers opus deepseek] [--variant clean|canary]

輸出：
  lab/cards/trial/<writer>/<id>.json、.meta.json（含程式檢查結果與重寫紀錄）
  lab/eval/trial/check-<checker>/<id>/notes.json（檢查員獨立讀全文的筆記，每篇一份）
  lab/eval/trial/check-<checker>/<id>/<writer>.<variant>.json（檢查結果）
已存在的結果會跳過，中斷後重跑即可續跑。
"""
import argparse
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import yaml

import judge_fable as jf
import offpeak
from common import KB, LAB, PIPELINE, load_vocab, vocab_blocks
from validate import gate_v4

# 試產資料已封存到 lab/；量產只用這個模組的 ctx()、check_schema()、items()
SRC = LAB / "fulltext" / "poc" / "clean-v2"
CARDS = LAB / "cards" / "trial"
EVAL = LAB / "eval" / "trial"
WRITERS = {"opus": "claude-opus-5-5", "deepseek": "deepseek-flash", "kimi": "kimi-k3.1-pro"}
CHECK_TYPES = ["方法对象错误", "数字张冠李戴", "引用不支持", "标签错误", "重要遗漏", "其他"]
s, arr, obj, enum = jf.s, jf.arr, jf.obj, jf.enum


def card_v4_schema(vocab, st):
    """v3 加上：每個方法步驟附引文與章節；市場條件（結論依賴的大陸市場事實，附引文，2026-11-23 起）。"""
    sc = copy.deepcopy(jf.card_v3_schema(vocab, st))
    sc["properties"]["method_steps"] = arr(obj({"step": s, "quote": s, "section": s}))
    sc["properties"]["market_conditions"] = arr(obj({"condition": s, "quote": s, "section": s}))
    sc["required"] = list(sc["properties"])
    return sc


def check_schema():
    return obj({
        "issues": arr(obj({"field": s, "type": enum(CHECK_TYPES), "severity": enum(jf.SEVERITIES),
                           "card_says": s, "source_says": s, "quote": s, "section": s})),
        "verdict": enum(["pass", "revise", "reject"]),
        "borrower_test": obj({"verdict": enum(["correct", "partial", "wrong"]), "reason": s}),
        # 筆記的必須涵蓋要點逐條對照（2026-11-24 起）：missing 且引文在原文裡，run.py 一律當 major 退回
        "must_capture_check": arr(obj({"point": s, "status": enum(["covered", "missing"]),
                                       "missing_part": s, "quote": s, "section": s})),
    })


def call(model, prompt, schema, effort):
    """回傳 (structured_output 或 None, meta)。"""
    if model.startswith(("deepseek", "kimi")):
        for attempt in range(3):  # DeepSeek 偶爾回傳壞掉的 JSON，SDK 會直接丟例外
            try:
                res, tail, secs = jf.run_deepseek(prompt, schema, effort, model)
                break
            except offpeak.PeakHours:
                raise   # DeepSeek 尖峰時段：不重試，直接停
            except Exception as e:
                res, tail, secs = None, f"{type(e).__name__}: {str(e)[:300]}", 0.0
    else:
        res, tail, secs = jf.run_fable(prompt, schema, effort, 900, 24, model, no_cache=True)
    meta = {"model": model, "effort": effort, "seconds": secs, "prompt_chars": len(prompt)}
    if res:
        meta.update(cost_usd=res.get("total_cost_usd"), usage=res.get("usage"),
                    num_turns=res.get("num_turns"), is_error=res.get("is_error"))
    out = (res or {}).get("structured_output")
    if not out:
        meta["error"] = tail[-1500:]
    return out, meta


def ctx():
    vocab = load_vocab()
    st_text = (PIPELINE / "judge" / "strategy_types.yaml").read_text()
    st = yaml.safe_load(st_text)
    fams, meths = vocab_blocks(vocab)
    return SimpleNamespace(vocab=vocab, st=st, st_text=st_text, fams=fams, meths=meths,
                           card_schema=card_v4_schema(vocab, st))


def rids(args):
    ids = sorted(m.stem for m in SRC.glob("*.md"))
    return [r for r in ids if r in args.only] if args.only else ids


def write_one(rid, writer, c, effort, src=SRC, out_name=None):
    out = CARDS / (out_name or writer)
    if (out / f"{rid}.json").exists():
        return rid, "skip"
    fulltext = (src / f"{rid}.md").read_text()
    prompt = jf.fill((PIPELINE / "card_prompt_v3.md").read_text(), strategy_types=c.st_text,
                     families=c.fams, methods=c.meths, report_id=rid, fulltext=fulltext)
    card, meta = call(WRITERS[writer], prompt, c.card_schema, effort)
    meta = {"attempts": [meta]}
    if card:
        meta["gate_first"] = gate_v4(card, fulltext, c.card_schema)
        if meta["gate_first"]:  # 程式檢查不過：帶錯誤清單重寫 1 次（上限 1 次）
            (out / f"{rid}.attempt1.json").write_text(json.dumps(card, ensure_ascii=False, indent=2))
            fix = (prompt + "\n\n## 你上一次写的卡片\n\n" + json.dumps(card, ensure_ascii=False, indent=2) +
                   "\n\n## 程序检查发现的问题\n\n" + "\n".join(f"- {p}" for p in meta["gate_first"]) +
                   "\n\n请修正这些问题后重新输出完整的卡片。引文必须从原文逐字复制；找不到合适的原文就删掉那一条，不要改写引文。")
            card2, m2 = call(WRITERS[writer], fix, c.card_schema, effort)
            meta["attempts"].append(m2)
            if card2:
                card = card2
                meta["gate_final"] = gate_v4(card, fulltext, c.card_schema)
        else:
            meta["gate_final"] = []
        (out / f"{rid}.json").write_text(json.dumps(card, ensure_ascii=False, indent=2))
    (out / f"{rid}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    if not card:
        return rid, "ERROR"
    return rid, (f"gate1={len(meta['gate_first'])} final={len(meta.get('gate_final', []))} "
                 f"{sum(a['seconds'] for a in meta['attempts']):.0f}s "
                 f"${sum(a.get('cost_usd') or 0 for a in meta['attempts']):.3f}")


def cmd_write(args):
    c = ctx()
    src = KB / args.src if args.src else SRC
    (CARDS / (args.out_name or args.writer)).mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(args.workers) as ex:
        for rid, status in ex.map(lambda r: write_one(r, args.writer, c, args.effort, src, args.out_name), rids(args)):
            print(f"{rid}  {status}", flush=True)


def cmd_gate(args):
    c = ctx()
    for d in sorted(p for p in CARDS.iterdir() if p.is_dir() and not p.name.startswith("_")):
        metas = [json.loads(p.read_text()) for p in sorted(d.glob("*.meta.json"))]
        ok = [m for m in metas if "gate_first" in m]
        if not ok:
            continue
        first = sum(not m["gate_first"] for m in ok)
        final = sum(not m.get("gate_final", m["gate_first"]) for m in ok)
        att = [a for m in metas for a in m["attempts"]]
        out_tok = [a["usage"]["output_tokens"] for m in ok for a in m["attempts"][:1] if a.get("usage")]
        print(f"{d.name:<10} 卡片 {len(ok)}/{len(metas)}  第一次通過 {first}  重寫後通過 {final}  "
              f"每篇 ${sum(a.get('cost_usd') or 0 for a in att) / len(metas):.3f}  "
              f"{sum(a['seconds'] for a in att) / len(metas):.0f} 秒  首次輸出 {sum(out_tok) / max(len(out_tok), 1):.0f} token")
        if args.verbose:
            for p, m in zip(sorted(d.glob("*.meta.json")), metas):
                for g in m.get("gate_final") or []:
                    print(f"    {p.name[:-10]}: {g}")


def check_one(rid, c, args):
    d = EVAL / f"check-{args.checker}" / rid
    d.mkdir(parents=True, exist_ok=True)
    fulltext = (SRC / f"{rid}.md").read_text()
    lines = []
    todo = []
    for w in args.writers:
        src = (CARDS / w / f"{rid}.json") if args.variant == "clean" else (CARDS / "_canary" / w / f"{rid}.json")
        if src.exists() and not (d / f"{w}.{args.variant}.json").exists():
            todo.append((w, src))
    if not todo:
        return rid, ["skip"]
    notes_p = d / "notes.json"
    if not notes_p.exists():  # 檢查員獨立讀全文（沿用審稿的 pass1）
        p1, meta = call(args.checker, jf.fill((PIPELINE / "judge" / "pass1_prompt.md").read_text(),
                                             strategy_types=c.st_text, families=c.fams, methods=c.meths,
                                             report_id=rid, fulltext=fulltext),
                        jf.pass1_schema(c.vocab, c.st), args.effort)
        (d / "notes.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        if not p1:
            return rid, ["ERROR notes"]
        notes_p.write_text(json.dumps(p1, ensure_ascii=False, indent=2))
        lines.append(f"notes {meta['seconds']:.0f}s ${meta.get('cost_usd') or 0:.3f}")
    p1 = json.loads(notes_p.read_text())
    notes = json.dumps({"reference_card": p1["reference_card"], "must_capture": p1["must_capture"]},
                       ensure_ascii=False, indent=2)
    tpl = (PIPELINE / "check_prompt.md").read_text()
    for w, src in todo:
        res, meta = call(args.checker, jf.fill(tpl, methods=c.meths, report_id=rid, fulltext=fulltext,
                                               notes=notes, card=src.read_text()), check_schema(), args.effort)
        (d / f"{w}.{args.variant}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        if not res:
            lines.append(f"{w}: ERROR")
            continue
        (d / f"{w}.{args.variant}.json").write_text(json.dumps(res, ensure_ascii=False, indent=2))
        sev = [i["severity"] for i in res["issues"]]
        lines.append(f"{w}: {res['verdict']} c{sev.count('critical')}/M{sev.count('major')}/m{sev.count('minor')} "
                     f"{meta['seconds']:.0f}s ${meta.get('cost_usd') or 0:.3f}")
    return rid, lines


def safe(fn, rid, *a):
    try:
        return fn(rid, *a)
    except Exception as e:  # 單篇失敗不中斷整批
        return rid, [f"ERROR {type(e).__name__}: {str(e)[:200]}"]


def cmd_check(args):
    c = ctx()
    with ThreadPoolExecutor(args.workers) as ex:
        for rid, lines in ex.map(lambda r: safe(check_one, r, c, args), rids(args)):
            print(f"{rid}  " + " | ".join(lines), flush=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write")
    w.add_argument("--writer", choices=list(WRITERS), required=True)
    w.add_argument("--src", help="全文資料夾（相對 KB）；預設 lab/fulltext/poc/clean-v2")
    w.add_argument("--out-name", help="輸出到 lab/cards/trial/<out-name>/；預設同 writer")
    g = sub.add_parser("gate")
    g.add_argument("--verbose", action="store_true")
    k = sub.add_parser("check")
    k.add_argument("--checker", default="deepseek-flash")
    k.add_argument("--writers", nargs="+", default=["opus", "deepseek"])
    k.add_argument("--variant", choices=["clean", "canary"], default="clean")
    for p in (w, k):
        p.add_argument("--only", nargs="*")
        p.add_argument("--effort", default="high")
        p.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    {"write": cmd_write, "gate": cmd_gate, "check": cmd_check}[args.cmd](args)


if __name__ == "__main__":
    main()
