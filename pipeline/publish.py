"""發布（階段 9）：通過的卡 → 知識庫（kb/）。人工複核的判定先寫回 manifest，發布時照判定處理。

用法：
  python publish.py reviews <batch>     # 讀填好的複核表與審計結果，寫回 manifest（不送 API）
  python publish.py accept <batch> <id> [<id> ...]   # 使用者同意：待升級的卡帶著已知問題發布
  python publish.py publish <batch>     # 渲染卡片、複製全文、重建 kb/catalog.jsonl 與 kb/CLAUDE.md

人工判定的處理（只看「真」；誤報、吹毛求疵只記錄）：
  - 判定對象是第一次檢查、而那張卡之後重寫過 → 已由重寫處理，不再算問題
  - 其餘「真」= 仍在卡上的已知問題：critical 不發布；major / minor 發布，卡片最後加「已知问题」一節，
    frontmatter 的 known_issues 記條數，讓檢索時看得到
複核表是人工填寫的唯一來源；重新填或改判之後再跑一次 `reviews` 即可。

待升級（重寫後複檢仍不過）的卡預設不發布。使用者看過、同意帶著已知問題發布的，用 `accept` 登記（2026-11-24 起）：
  - 已知問題＝最後一次檢查（對目前這版卡片）判的 critical／major；複核表把同一條判成誤報或吹毛求疵的不列
  - 有 critical 的不能 accept；更早版本卡片的複核條目不再適用（卡片已改過）
"""
import argparse
import json
import re
import shutil
import time

import yaml

import batch as B
from common import PIPELINE, load_vocab
from run import catalog_lock

KB_OUT = B.ROOT / "kb"
RESULTS = PIPELINE / "results"
VERDICTS = ("誤報", "吹毛求疵", "真")


# ---- 複核表 → 判定 ----
def parse_review(path, source):
    """回傳 [{rid, stage, severity, type, field, verdict, note, source, item}]。

    條目標題：`## 3. <rid> / 第一次檢查`（檢查員的複核表）或 `## 3. <rid>`（審計的複核表）。
    人工判定一行：`- 人工判定：＿真＿（真 / 誤報 / 吹毛求疵）`。判定詞要放在最前面，後面可以接說明；說明裡寫「降為 minor」之類會改掉嚴重度。
    """
    items = []
    for block in re.split(r"\n(?=## \d+\. )", path.read_text())[1:]:
        head = block.split("\n", 1)[0]
        m = re.match(r"## (\d+)\. (\S+)(?: / (.+))?", head)
        no, rid, label = m.group(1), m.group(2), (m.group(3) or "").strip()
        stage = "audit" if source == "audit" else ("check2" if "複檢" in label else "check1")
        sev = re.search(r"嚴重度 / 類型：\*\*(\w+)\*\* / ([^（\n]+)", block)
        field = re.search(r"欄位：`([^`]*)`", block)
        says = re.search(r"(?:檢查員|審計員)說原文是：(.+)", block)
        j = re.search(r"人工判定：(.*?)（真 / 誤報 / 吹毛求疵）", block)
        raw = (j.group(1) if j else "").replace("＿", " ").replace("*", "").strip()
        verdict = next((v for v in VERDICTS if raw.startswith(v)), None)   # 只認開頭；開頭認不出就當沒填，請人確認
        if verdict is None and re.match(r"部分(?:真|成立)", raw):   # 「部分真」：卡片確實有錯的部分，當真處理
            verdict = "真"
            raw = "真，" + raw
        skip = bool(re.search(r"要不要看：可略過", block))   # 抽查制下標「可略過」的，沒填不算漏
        severity = sev.group(1) if sev else None
        regrade = re.search(r"(?:降為|降为|改為|改为|升為|升为)\s*(critical|major|minor)", raw)   # 說明裡寫「降為 minor」就照人工改嚴重度
        if verdict == "真" and regrade:
            severity = regrade.group(1)
        items.append({"rid": rid, "stage": stage, "source": f"{path.name}#{no}",
                      "severity": severity, "type": sev.group(2).strip() if sev else None,
                      "field": field.group(1) if field else None, "verdict": verdict,
                      "note": raw[len(verdict):].strip(" ，,：:") if verdict else raw,
                      "source_says": says.group(1).strip() if says else "", "skip": skip})
    return items


JUDGE_RE = re.compile(r"^- 人工判定：.*$", re.M)


def _block_key(block):
    head = block.split("\n", 1)[0]
    head = re.sub(r"^## \d+\. ", "", head)
    pick = lambda pat: (re.search(pat, block) or [None, ""])[1] if re.search(pat, block) else ""
    return (head, pick(r"嚴重度 / 類型：(.+)"), pick(r"欄位：(.+)"), pick(r"卡片寫：(.{0,80})"))


def keep_judgments(dest, text):
    """重新產生複核表時，把舊檔裡已經填好的「人工判定」搬到同一條目上（以標題、嚴重度、欄位、卡片原話認條目）。"""
    if not dest.exists():
        return text
    old = {}
    for b in re.split(r"\n(?=## \d+\. )", dest.read_text())[1:]:
        m = JUDGE_RE.search(b)
        if m and "＿＿（" not in m.group(0):
            old[_block_key(b)] = m.group(0)
    parts = re.split(r"\n(?=## \d+\. )", text)
    kept = 0
    for i, b in enumerate(parts[1:], 1):
        line = old.get(_block_key(b))
        if line:
            parts[i] = JUDGE_RE.sub(lambda _: line, b, count=1)
            kept += 1
    if kept:
        print(f"（保留舊檔已填的判定 {kept} 條）")
    return "\n".join(parts)


def sync_audit(batch):
    """把審計結論寫進 manifest 的 audit 欄位。"""
    with catalog_lock():
        m = B.load()
        for rid, v in m.items():
            if v["batch"] == batch:
                a = audit_summary(batch, rid)
                if a:
                    v["audit"] = a
        B.save(m)


def audit_summary(batch, rid):
    for d in ("_audit_max", "_audit"):
        p = B.ROOT / "eval" / batch / d / rid / "audit.json"
        if p.exists():
            res = json.loads(p.read_text())["result"]
            sev = [i["severity"] for i in res["issues"]]
            return {"model": "MiniMax-M3.1-Flash", "effort": d.removeprefix("_audit_") if d != "_audit" else "high",
                    "verdict": res["verdict"], "critical": sev.count("critical"), "major": sev.count("major"),
                    "minor": sev.count("minor")}
    return None


def cmd_reviews(args):
    files = [(RESULTS / f"major_review_{args.batch}.md", "check"), (RESULTS / f"audit_review_{args.batch}.md", "audit")]
    items = [i for p, src in files if p.exists() for i in parse_review(p, src)]
    blank = [i for i in items if i["verdict"] is None]
    with catalog_lock():
        m = B.load()
        unknown = sorted({i["rid"] for i in items} - set(m))
        for rid, v in m.items():
            if v["batch"] != args.batch:
                continue
            mine = [i for i in items if i["rid"] == rid]
            revised = (v.get("card") or {}).get("revised", False)
            for i in mine:
                i["resolved"] = i["verdict"] == "真" and i["stage"] == "check1" and revised
                i["open"] = i["verdict"] == "真" and not i["resolved"]
            if mine:
                v["review"] = {"items": [{k: x for k, x in i.items() if k != "rid"} for i in mine],
                               "applied_at": time.strftime("%Y-%m-%d %H:%M")}
            else:
                v.pop("review", None)
            a = audit_summary(args.batch, rid)
            if a:
                v["audit"] = a
        B.save(m)
    print(f"{len(items)} 條判定（{', '.join(f'{v} {sum(i['verdict'] == v for i in items)}' for v in VERDICTS)}"
          f"，空白 {len(blank)}）")
    for i in items:
        if i.get("open"):
            print(f"  仍在卡上：{i['rid']}  {i['severity']} {i['type']}  `{i['field']}`")
        elif i.get("resolved"):
            print(f"  已由重寫處理：{i['rid']}  {i['severity']} {i['type']}")
    skipped = [i for i in blank if i.get("skip")]
    if skipped:
        print(f"  標「可略過」沒填的 {len(skipped)} 條不用判")
    for i in (i for i in blank if not i.get("skip")):
        print(f"  還沒填或看不懂（判定要以 真／誤報／吹毛求疵 開頭）：{i['source']}" + (f"「{i['note'][:30]}」" if i["note"] else ""))
    if unknown:
        print(f"  manifest 裡沒有這些篇（可能是試產的卡，略過）：{unknown}")


# ---- 渲染 ----
def tag(vocab_part, k):
    return f"{k}/{vocab_part.get(k, {}).get('zh', '')}"


def known_issues(v):
    """發布時要列在卡片最後的已知問題。"""
    acc = (v.get("card") or {}).get("accepted")
    items = (v.get("review") or {}).get("items", [])
    if not acc:
        return [i for i in items if i.get("open")]
    dismissed = {(i["stage"], i["field"], i["type"]) for i in items if i["verdict"] in ("誤報", "吹毛求疵")}
    return [i for i in acc["known"] if (acc["check"], i["field"], i["type"]) not in dismissed]


def cmd_accept(args):
    with catalog_lock():
        m = B.load()
        for rid in args.ids:
            v = m.get(rid)
            rec = (v or {}).get("card") or {}
            if not v or v["batch"] != args.batch or rec.get("state") != "escalate":
                print(f"略過 {rid}：不在批次 {args.batch} 或不是待升級")
                continue
            ev = B.ROOT / "eval" / args.batch / rid
            stage = "check2" if (ev / "check2.json").exists() else "check1"
            res = json.loads((ev / f"{stage}.json").read_text())
            serious = [i for i in res["issues"] if i["severity"] != "minor"]
            if any(i["severity"] == "critical" for i in serious):
                print(f"略過 {rid}：最後一次檢查有 critical，不能帶著已知問題發布")
                continue
            rec["accepted"] = {"at": time.strftime("%Y-%m-%d %H:%M"), "by": "使用者", "check": stage,
                               "known": [{"stage": stage, "source": "檢查員（待升級，使用者同意帶著已知問題發布）",
                                          **{k: i.get(k, "") for k in ("severity", "type", "field", "source_says",
                                                                       "quote", "section")}} for i in serious]}
            print(f"登記 {rid}：帶 {len(serious)} 個已知問題發布（{stage}）")
        B.save(m)


def render(rid, v, c, vocab, st):
    fams, meths = vocab["families"], vocab["methods"]
    keywords = []
    for m in c["methods"]:
        keywords += [meths.get(m, {}).get("zh", m), *map(str, meths.get(m, {}).get("aliases", []))]
    for f in c["families"]:
        keywords += [fams.get(f, {}).get("zh", f), *map(str, fams.get(f, {}).get("aliases", []))]
    keywords += c.get("new_tag_suggestions") or []
    known = known_issues(v)
    audit = v.get("audit")
    fm = {
        "id": rid, "title": c["title"], "broker": c["broker"], "date": c["date"], "series": c["series"],
        "report_type": c["report_type"],
        "primary_strategy_type": tag(st, c["primary_strategy_type"]),
        "strategy_types": [tag(st, s) for s in c["strategy_types"]],
        "asset_class": c["asset_class"], "frequency": c["frequency"],
        "families": [tag(fams, f) for f in c["families"]],
        "methods": [tag(meths, m) for m in c["methods"]],
        "keywords": list(dict.fromkeys(keywords)),
        "fulltext": f"fulltext/{rid}.md",
        "source_pdf": v["source_pdf"],
        "checked": ("escalated-accepted" if (v.get("card") or {}).get("accepted")
                    else "revised" if (v.get("card") or {}).get("revised") else "pass"),
        "audited": (audit or {}).get("verdict"),
        "known_issues": len(known),
    }
    out = ["---", yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=1000).strip(), "---", "",
           f"# {c['title']}", "", f"{c['broker']}｜{c['date']}｜{c['series'] or '非系列'}｜{c['report_type']}", ""]
    if known:
        out += ["> **注意：本卡有已知问题（人工复核确认，或检查员指出、使用者同意带着发布），见文末「已知问题」。**", ""]
    out += ["## 摘要", "", c["summary"], "", "## 核心思路", "", c["core_idea"], "", "## 方法步骤", ""]
    for n, s in enumerate(c["method_steps"], 1):
        out += [f"{n}. {s['step']}", f"   - 原文（{s['section']}）：「{s['quote']}」"]
    out += [""]
    if c["key_formulas"]:
        out += ["## 核心公式", ""]
        for f in c["key_formulas"]:
            out += [f"- {f.get('meaning', '')}", "", f"  $$ {f.get('latex', '')} $$", ""]
    if c["key_results"]:
        out += ["## 关键结果", "", "| 指标 | 数值 | 条件 |", "|---|---|---|"]
        out += [f"| {r['metric']} | {r['value']} | {r['context']} |".replace("\n", " ") for r in c["key_results"]]
        out += ["", "原文依据：", ""] + [f"- {r['metric']}：「{r['quote']}」".replace("\n", " ") for r in c["key_results"]] + [""]
    if c.get("market_conditions"):   # 2026-11-23 之後寫的卡才有
        out += ["## 市场条件", "", "结论依赖的中国大陆市场条件（原文事实）。用到其他市场前，先看这些条件在目标市场是否成立。", ""]
        for m in c["market_conditions"]:
            out += [f"- {m['condition']}", f"  - 原文（{m['section']}）：「{m['quote']}」"]
        out += [""]
    out += ["## 局限", ""] + [f"- {x}" for x in c["limitations"]] + [""]
    if c["implementation_gaps"]:
        out += ["## 复现缺口（原文没交代的）", ""] + [f"- {x}" for x in c["implementation_gaps"]] + [""]
    if c["data_needed"]:
        out += ["## 所需数据", ""] + [f"- {x}" for x in c["data_needed"]] + [""]
    if c["evidence_quotes"]:
        out += ["## 其他原文依据", ""] + [f"- {q['claim']}：「{q['quote']}」" for q in c["evidence_quotes"]] + [""]
    if known:
        out += ["## 已知问题", "", "卡片尚未修正的问题（人工对照原文确认，或检查员指出、使用者同意带着发布）。使用这张卡时以原文为准。", ""]
        out += [f"- **{i['severity']} / {i['type']}**（`{i['field']}`）：{i['source_says']}"
                + (f"（复核备注：{i['note']}）" if i.get("note") else "") for i in known] + [""]
    return "\n".join(out), fm


def vocab_md(vocab, st, catalog):
    """標籤對照表：把問題對到標籤 id 用。附每個標籤目前有幾張卡。"""
    from collections import Counter
    cnt = Counter()
    for c in catalog:
        for k in ("strategy_types", "families", "methods"):
            cnt.update(x.split("/")[0] for x in c[k])
    out = ["# 标签对照", "", "由 `publish.py` 产生。「卡片数」是目前知识库里标了这个标签的卡。", "",
           "## 策略大类（strategy_types）", "", "| id | 中文 | 定义 | 卡片数 |", "|---|---|---|---|"]
    out += [f"| {k} | {v['zh']} | {v.get('definition', '')} | {cnt[k]} |" for k, v in st.items()]
    out += ["", "## 方法家族（families）", "", "| id | 中文 | 别名 | 卡片数 |", "|---|---|---|---|"]
    out += [f"| {k} | {v['zh']} | {'、'.join(map(str, v.get('aliases', [])))} | {cnt[k]} |" for k, v in vocab["families"].items()]
    out += ["", "## 方法（methods）", "", "| id | 中文 | 别名 | 家族 | 卡片数 |", "|---|---|---|---|---|"]
    out += [f"| {k} | {v['zh']} | {'、'.join(map(str, v.get('aliases', [])))} | {v.get('family', '')} | {cnt[k]} |"
            for k, v in vocab["methods"].items()]
    return "\n".join(out) + "\n"


def cmd_publish(args):
    vocab = load_vocab()
    st = yaml.safe_load((PIPELINE / "judge" / "strategy_types.yaml").read_text())["strategy_types"]
    m = B.load()
    cards_dir, ft_dir = KB_OUT / "cards", KB_OUT / "fulltext"
    cards_dir.mkdir(parents=True, exist_ok=True)
    ft_dir.mkdir(parents=True, exist_ok=True)
    done, skipped = [], []
    for rid, v in sorted(m.items()):
        if v["batch"] != args.batch:
            continue
        state = (v.get("card") or {}).get("state")
        known = known_issues(v)
        if state != "passed" and not (state == "escalate" and v["card"].get("accepted")):
            skipped.append((rid, {None: "還沒寫卡", "escalate": "待升級", "error": "寫卡失敗"}.get(state, f"狀態 {state}")
                                 if v["status"] != "hold" else "hold（暫緩處理）"))
        elif any(i["severity"] == "critical" for i in known):
            skipped.append((rid, "有人工確認的 critical"))
        else:
            c = json.loads((B.ROOT / "cards" / args.batch / f"{rid}.json").read_text())
            md, fm = render(rid, v, c, vocab, st)
            (cards_dir / f"{rid}.md").write_text(md)
            shutil.copy(B.ROOT / "fulltext" / args.batch / "clean" / f"{rid}.md", ft_dir / f"{rid}.md")
            done.append(rid)
            continue
        for d in (cards_dir, ft_dir):   # 之前發布過、現在不該發布的要拿掉
            (d / f"{rid}.md").unlink(missing_ok=True)
    with catalog_lock():
        disk = B.load()
        for rid in done:
            disk[rid]["published"] = {"at": time.strftime("%Y-%m-%d %H:%M"), "path": f"kb/cards/{rid}.md"}
        for rid, _ in skipped:
            disk[rid].pop("published", None)
        B.save(disk)
    # 目錄涵蓋 kb/cards 下所有卡（可能來自不同批次）
    catalog = []
    for p in sorted(cards_dir.glob("*.md")):
        fm = yaml.safe_load(p.read_text().split("---", 2)[1])
        catalog.append({k: fm[k] for k in ("id", "title", "broker", "date", "series", "report_type",
                                           "primary_strategy_type", "strategy_types", "asset_class", "frequency",
                                           "families", "methods", "known_issues")})
    (KB_OUT / "catalog.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in catalog))
    (KB_OUT / "VOCAB.md").write_text(vocab_md(vocab, st, catalog))
    shutil.copy(PIPELINE / "kb_CLAUDE.md", KB_OUT / "CLAUDE.md")
    print(f"{args.batch}：發布 {len(done)} 張（其中帶已知問題 "
          f"{sum(1 for r in done if known_issues(m[r]))} 張）"
          f"；沒發布 {len(skipped)} 篇；知識庫共 {len(catalog)} 張 → {KB_OUT}")
    from collections import Counter
    for why, n in Counter(w for _, w in skipped).items():
        print(f"  沒發布：{why} × {n}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("reviews", "publish", "accept"):
        sp = sub.add_parser(name)
        sp.add_argument("batch")
        if name == "accept":
            sp.add_argument("ids", nargs="+")
    args = ap.parse_args()
    {"reviews": cmd_reviews, "publish": cmd_publish, "accept": cmd_accept}[args.cmd](args)


if __name__ == "__main__":
    main()
