"""試產的埋雷：在寫好的卡片上各改一處，量檢查員抓不抓得到。

  python trial_canary.py make     # 產生 cards/trial/_canary/<writer>/<id>.json 與 _canaries.json
  python trial_canary.py score --checker deepseek-flash   # 列出每個埋雷對應的檢查結果

三類（都通過程式硬檢查，只有讀原文才看得出來）：
  number  把兩條 key_results 的 value 與 quote 對調：數字和引文都是原文的，但套到別的對象上
  tag     把首要方法標籤換成同一大類裡、這張卡沒用到的另一個方法
  logic   由 Opus 改寫 core_idea 或某個方法步驟，把條件、方向或對象改反（引文不動）
"""
import argparse
import copy
import json
import random
import re

import judge_fable as jf
from common import KB, load_vocab
from trial import CARDS, EVAL, SRC, ctx
from validate import gate_v4

OUT = CARDS / "_canary"
WRITERS = ["opus", "deepseek"]
TYPES = ["number", "tag", "logic"]
s, obj = jf.s, jf.obj


def shape(v):
    return (len(re.findall(r"\d+(?:\.\d+)?", v)), "%" in v)


def canary_number(card, rng):
    rows = card["key_results"]
    pairs = [(i, j) for i in range(len(rows)) for j in range(i + 1, len(rows))
             if rows[i]["value"] != rows[j]["value"] and shape(rows[i]["value"]) == shape(rows[j]["value"])
             and shape(rows[i]["value"])[0] == 1]
    if not pairs:
        return None
    i, j = rng.choice(pairs)
    a, b = rows[i], rows[j]
    desc = (f"key_results.{i}（{a['metric'][:30]}）與 key_results.{j}（{b['metric'][:30]}）的 value、quote 對調："
            f"{a['value']} ↔ {b['value']}")
    a["value"], b["value"] = b["value"], a["value"]
    a["quote"], b["quote"] = b["quote"], a["quote"]
    return {"field": "key_results", "rows": [i, j], "desc": desc}


def canary_tag(card, rng, vocab):
    if not card["methods"]:
        return None
    old = card["methods"][0]
    fam = vocab["methods"][old]["family"]
    cands = [m for m, v in vocab["methods"].items() if v["family"] == fam and m not in card["methods"]]
    if not cands:
        return None
    new = rng.choice(cands)
    card["methods"][0] = new
    return {"field": "methods", "old": old, "new": new, "desc": f"methods[0] {old} → {new}（同屬 {fam}）"}


LOGIC_PROMPT = """下面是一张量化研报卡片的一部分。为了测试审核员，请你把指定的那一段文字改写成**有原则性错误**的版本：
把一个关键的条件、方向、先后顺序或对象改反或换掉（例如：时序换成横截面、做多换成做空、剔除换成保留、低换成高、主方法换成对照组的做法）。

要求：
- 只改一处关键内容，其余文字、长度、语气尽量保持原样，读起来仍然通顺自然、像是认真写的。
- 不要改数字，不要加任何提示性的字眼。
- 改完之后，照着做的人会做出和原文不同的东西，或得到相反的理解。

## 卡片的核心想法与方法步骤（供你理解上下文）

{context}

## 要改写的那一段（{path}）

{target}
"""


def canary_logic(card, rng, idx):
    steps = card["method_steps"]
    use_core = idx % 2 == 0 or not steps
    if use_core:
        path, target = "core_idea", card["core_idea"]
    else:
        k = rng.randrange(min(len(steps), 4))  # 前幾步通常是方法的核心
        path, target = f"method_steps.{k}.step", steps[k]["step"]
    context = json.dumps({"core_idea": card["core_idea"], "method_steps": [x["step"] for x in steps]},
                         ensure_ascii=False, indent=2)
    schema = obj({"new_text": s, "what_changed": s})
    res, tail, secs = jf.run_fable(jf.fill(LOGIC_PROMPT, context=context, path=path, target=target),
                                   schema, "medium", 900, 24, "claude-opus-5-5", no_cache=True)
    out = (res or {}).get("structured_output")
    if not out or out["new_text"].strip() == target.strip():
        return None
    if use_core:
        card["core_idea"] = out["new_text"]
    else:
        steps[k]["step"] = out["new_text"]
    return {"field": path, "old": target, "new": out["new_text"], "desc": out["what_changed"]}


def cmd_make(args):
    c = ctx()
    path = OUT / "_canaries.json"
    canaries = json.loads(path.read_text()) if path.exists() else []
    done = {(x["rid"], x["writer"]) for x in canaries}
    rids = sorted(m.stem for m in SRC.glob("*.md"))
    slots = []
    for i, rid in enumerate(rids):
        slots.append((rid, WRITERS[i % 2], TYPES[i % 3]))
        if i % 2 == 0:
            slots.append((rid, WRITERS[(i + 1) % 2], TYPES[(i + 1) % 3]))
    for n, (rid, w, typ) in enumerate(slots):
        src = CARDS / w / f"{rid}.json"
        if (rid, w) in done or not src.exists():
            continue
        fulltext = (SRC / f"{rid}.md").read_text()
        rng = random.Random(f"trial-canary-{rid}-{w}")
        info = None
        for t in [typ] + [x for x in TYPES if x != typ]:  # 這類做不出來就換下一類
            card = json.loads(src.read_text())
            info = {"number": lambda: canary_number(card, rng), "tag": lambda: canary_tag(card, rng, c.vocab),
                    "logic": lambda: canary_logic(card, rng, n)}[t]()
            if info:
                typ = t
                break
        if not info:
            print(f"{rid} {w}: 做不出埋雷", flush=True)
            continue
        gate = gate_v4(card, fulltext, c.card_schema)
        (OUT / w).mkdir(parents=True, exist_ok=True)
        (OUT / w / f"{rid}.json").write_text(json.dumps(card, ensure_ascii=False, indent=2))
        canaries.append({"rid": rid, "writer": w, "type": typ, "passes_code_gate": not gate, **info})
        path.write_text(json.dumps(canaries, ensure_ascii=False, indent=2))
        print(f"{rid} {w} {typ} gate={'過' if not gate else '不過'}: {info['desc'][:100]}", flush=True)


def cmd_score(args):
    canaries = json.loads((OUT / "_canaries.json").read_text())
    for cn in canaries:
        d = EVAL / f"check-{args.checker}" / cn["rid"]
        p = d / f"{cn['writer']}.canary.json"
        if not p.exists():
            print(f"\n### {cn['rid']} {cn['writer']} {cn['type']}：（未跑）")
            continue
        res = json.loads(p.read_text())
        clean_p = d / f"{cn['writer']}.clean.json"
        clean = json.loads(clean_p.read_text()) if clean_p.exists() else None
        key = cn["field"].split(".")[0]
        rel = [i for i in res["issues"] if key in i["field"]]
        print(f"\n### {cn['rid']} {cn['writer']} [{cn['type']}] {cn['desc'][:160]}")
        print(f"  verdict: 乾淨版 {clean['verdict'] if clean else '?'} → 埋雷版 {res['verdict']}；{key} 相關問題 {len(rel)} 個")
        for i in rel:
            print(f"   [{i['severity']}/{i['type']}] {i['field']}: {i['card_says'][:70]} ‖ {i['source_says'][:130]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("make")
    sc = sub.add_parser("score")
    sc.add_argument("--checker", default="deepseek-flash")
    args = ap.parse_args()
    {"make": cmd_make, "score": cmd_score}[args.cmd](args)
