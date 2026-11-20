"""抽樣審計（階段 8）：通過的卡交給和寫卡（Kimi）、檢查（DeepSeek Flash）都不同的模型再審一次。

審計員走和檢查員一樣的兩段：先只看全文寫自己的筆記，再拿筆記逐條核對卡片。
審計員判 critical / major、檢查員卻放行的卡 = 不一致，要人工對照原文。

用法：
  python audit.py calibrate               # 先用已知答案的卡測審計員：7 個埋雷 + 3 個人工確認的真漏寫
  python audit.py sample batch1 --n 20    # 抽 20 張通過的卡審計
  python audit.py review batch1           # 把不一致的條目寫成人工複核表

結果在 eval/<batch>/_audit/；不動 manifest。已存在的結果會跳過，中斷後重跑即可續跑。
"""
import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import batch as B
import judge_fable as jf
import run as R
import trial as T

AUDITOR = "MiniMax-M3.1-Flash"
PIPELINE = Path(__file__).resolve().parent


class Auditor:
    def __init__(self, batch, budget, effort="high"):
        R.EFFORT["MiniMax"] = effort
        self.r = R.Runner(batch, budget)
        self.ctx = self.r.ctx
        self.out = B.ROOT / "eval" / batch / ("_audit" if effort == "high" else f"_audit_{effort}")
        self.out.mkdir(parents=True, exist_ok=True)

    def notes(self, key, fulltext, rec):
        p = self.out / key / "notes.json"
        if p.exists():
            return json.loads(p.read_text())
        out = self.r.call(AUDITOR, jf.fill((PIPELINE / "judge" / "pass1_prompt.md").read_text(),
                                           strategy_types=self.ctx.st_text, families=self.ctx.fams,
                                           methods=self.ctx.meths, report_id=key, fulltext=fulltext),
                          jf.pass1_schema(self.ctx.vocab, self.ctx.st), rec, "audit-notes")
        if out is not None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        return out

    def audit(self, key, name, card, fulltext):
        """key：筆記與結果放的資料夾（同一篇全文共用一份筆記）；name：這次審的是哪張卡。"""
        p = self.out / key / f"{name}.json"
        if p.exists():
            return json.loads(p.read_text())
        rec = {}
        notes = self.notes(key, fulltext, rec)
        if notes is None:
            return None
        n = json.dumps({"reference_card": notes["reference_card"], "must_capture": notes["must_capture"]},
                       ensure_ascii=False, indent=2)
        res = self.r.call(AUDITOR, jf.fill((PIPELINE / "check_prompt.md").read_text(), methods=self.ctx.meths,
                                           report_id=key, fulltext=fulltext, notes=n,
                                           card=json.dumps(card, ensure_ascii=False, indent=2)),
                          T.check_schema(), rec, "audit")
        if res is None:
            return None
        out = {"result": res, "cost_cny": rec.get("cost_cny"), "calls": rec.get("calls")}
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        return out


def serious(res):
    return [i for i in (res or {}).get("issues", []) if i["severity"] != "minor"]


# ---- 校準：已知答案的卡 ----
TRIAL_CHECK = B.KB / "eval" / "trial" / "check-deepseek-flash"
REAL_OMISSIONS = [  # 人工複核認定為真的「重要遗漏」major（pipeline/results/major_review_*.md）
    ("20240506_国金证券_b61ef7", B.KB / "cards/trial/deepseek/20240506_国金证券_b61ef7.json",
     T.SRC / "20240506_国金证券_b61ef7.md", ["微盘", "微盤"]),
    ("20240512_国金证券_35a947", B.KB / "cards/trial/deepseek/20240512_国金证券_35a947.json",
     T.SRC / "20240512_国金证券_35a947.md", ["社融"]),
    ("20231025_招商证券_7d3b43", B.ROOT / "cards/batch1/20231025_招商证券_7d3b43.v1.json",
     B.ROOT / "fulltext/batch1/clean/20231025_招商证券_7d3b43.md", ["10 天", "10天", "相邻", "相鄰", "泄露", "泄漏", "洩露", "洩漏"]),
]


def cmd_calibrate(args):
    a = Auditor("batch1", args.budget, args.effort)
    jobs = []
    for c in json.loads((B.ROOT / "eval/batch1/_canary/_canaries.json").read_text()):
        card = json.loads((B.ROOT / f"eval/batch1/_canary/{c['rid']}.json").read_text())
        jobs.append(("canary", c, c["rid"], card, a.r.fulltext(c["rid"])))
    for rid, cardp, ftp, kw in REAL_OMISSIONS:
        jobs.append(("omission", {"type": "omission", "kw": kw}, rid, json.loads(cardp.read_text()), ftp.read_text()))

    def one(j):
        kind, info, rid, card, ft = j
        return kind, info, rid, a.audit(rid, f"calib_{kind}", card, ft)

    jobs = [j for j in jobs if not getattr(args, "only", None) or j[0] == args.only]
    with ThreadPoolExecutor(args.workers) as ex:
        rows = list(ex.map(one, jobs))
    ok = {"canary": [0, 0], "omission": [0, 0]}
    for kind, info, rid, out in rows:
        res = (out or {}).get("result")
        if kind == "canary":
            key = info["field"].split(".")[0]
            hit = res and res["verdict"] != "pass" and any(key in i["field"] for i in serious(res))
            what = info["desc"]
        else:
            hit = res and res["verdict"] != "pass" and any(
                any(w in json.dumps(i, ensure_ascii=False) for w in info["kw"]) for i in serious(res))
            what = "真漏寫：" + info["kw"][0]
        ok[kind][0] += bool(hit)
        ok[kind][1] += 1
        print(f"  {'抓到' if hit else '沒抓到'}  {rid[:26]}  {info['type']:8} {what[:50]}"
              f"  → {(res or {}).get('verdict', '失敗')}")
    print(f"\n埋雷 {ok['canary'][0]}/{ok['canary'][1]}（檢查員有筆記時 7/7）；"
          f"真漏寫 {ok['omission'][0]}/{ok['omission'][1]}（檢查員有筆記時 5/6 次）")
    print(f"這次花費 ¥{a.r.spent:.2f}")


# ---- 抽樣 ----
def sample(manifest, batch, n):
    passed = [k for k, v in manifest.items() if v["batch"] == batch and (v.get("card") or {}).get("state") == "passed"]
    return sorted(passed, key=lambda k: hashlib.sha1(f"audit:{k}".encode()).hexdigest())[:n]


def cmd_sample(args):
    a = Auditor(args.batch, args.budget, args.effort)
    picks = sample(a.r.manifest, args.batch, args.n)
    print(f"{args.batch}：通過的卡抽 {len(picks)} 張", flush=True)

    def one(rid):
        card = json.loads((B.ROOT / "cards" / args.batch / f"{rid}.json").read_text())
        return rid, a.audit(rid, "audit", card, a.r.fulltext(rid))

    with ThreadPoolExecutor(args.workers) as ex:
        rows = list(ex.map(one, picks))
    bad = 0
    for rid, out in rows:
        res = (out or {}).get("result")
        sev = [i["severity"] for i in (res or {}).get("issues", [])]
        flag = res and res["verdict"] != "pass" and serious(res)
        bad += bool(flag)
        print(f"  {'不一致' if flag else '一致  '}  {rid[:28]}  {(res or {}).get('verdict', '失敗'):7}"
              f" critical {sev.count('critical')} major {sev.count('major')} minor {sev.count('minor')}")
    done = sum(1 for _, o in rows if o)
    print(f"\n審計員判 critical/major、檢查員放行：{bad}/{done}")
    print(f"這次花費 ¥{a.r.spent:.2f}")


def cmd_review(args):
    """不一致的條目 → 人工複核表（格式同 major_review_*.md）。"""
    from validate import _norm, quote_found
    d = B.ROOT / "eval" / args.batch / ("_audit" if args.effort == "high" else f"_audit_{args.effort}")
    out = ["# 抽樣審計：審計員判 critical / major、檢查員放行的條目", "",
           f"審計員 {AUDITOR}。每一條對照原文判斷：**真**（卡片確實錯，檢查員漏抓）、**誤報**（卡片沒錯）、"
           "**吹毛求疵**（不算錯但可改）。", "",
           "「檢查員當時」是 DeepSeek 對同一處有沒有提過（minor 也算）。", ""]
    n = 0
    for p in sorted(d.glob("*/audit.json")):
        rid = p.parent.name
        res = json.loads(p.read_text())["result"]
        ft = _norm((B.ROOT / "fulltext" / args.batch / "clean" / f"{rid}.md").read_text())
        ev = B.ROOT / "eval" / args.batch / rid
        checks = [json.loads(f.read_text()) for f in sorted(ev.glob("check[12].json"))]
        last = checks[-1] if checks else {"issues": []}
        for i in serious(res):
            n += 1
            same = [c for c in last["issues"] if c["field"].split("（")[0].split(".")[0] in i["field"]]
            out += [f"## {n}. {rid}", "",
                    f"- 嚴重度 / 類型：**{i['severity']}** / {i['type']}（審計員整張卡判 {res['verdict']}）",
                    f"- 欄位：`{i['field']}`",
                    f"- 卡片寫：{i['card_says']}",
                    f"- 審計員說原文是：{i['source_says']}",
                    f"- 引文：「{i['quote']}」（{i['section']}）",
                    f"- 引文在全文：{'找得到' if quote_found(i['quote'], ft) else '**找不到**'}",
                    f"- 檢查員當時：" + ("；".join(f"{c['severity']} {c['type']}：{c['source_says'][:60]}" for c in same)
                                       or "同欄位沒有提過"),
                    "- 人工判定：＿＿（真 / 誤報 / 吹毛求疵）", ""]
    dest = PIPELINE / "results" / f"audit_review_{args.batch}.md"
    dest.write_text("\n".join(out))
    print(f"{n} 條 → {dest}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calibrate")
    s = sub.add_parser("sample")
    s.add_argument("batch")
    s.add_argument("--n", type=int, default=20)
    v = sub.add_parser("review")
    v.add_argument("batch")
    v.add_argument("--effort", default="max", choices=["high", "max"])
    for x in (c, s):
        x.add_argument("--budget", type=float, default=10)
        x.add_argument("--workers", type=int, default=8)
        x.add_argument("--effort", default="max", choices=["high", "max"],
                       help="max：high 在 3 個真漏寫上 0/3、max 2/3（2026-11-20 校準）")
        x.add_argument("--only", choices=["canary", "omission"], help="calibrate：只跑其中一種")
    args = ap.parse_args()
    {"calibrate": cmd_calibrate, "sample": cmd_sample, "review": cmd_review}[args.cmd](args)


if __name__ == "__main__":
    main()
