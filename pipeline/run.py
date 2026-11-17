"""量產的模型階段（會送 API、會花錢）：寫卡 → 程式檢查 → 模型檢查 → 退回重寫再複檢 → 埋雷監控。

用法（<batch> 要先跑完 batch.py 的本機階段）：
  python run.py cards  <batch> --budget 150 --dry-run     # 只列出會做什麼、估算花費，不送出
  python run.py cards  <batch> --budget 150               # 真的送出
  python run.py canary <batch> --budget 20 [--every 20]   # 在通過的卡上埋雷，量檢查員的抓到率
  python run.py report <batch>                            # 通過率、退回率、升級率、花費（不送 API）
  python run.py review <batch>|--trial                    # 把檢查員判的 critical / major 列成人工複核表（不送 API）
  python run.py triage <batch>|--trial                    # 用檔名規則預測週報類，對照卡片的 report_type（不送 API）

每篇研報的流程（結果寫回 catalog/manifest.json 的 card 欄位）：
  寫卡 → 程式檢查不過：帶錯誤清單重寫 1 次
       → 模型檢查（先獨立讀全文寫筆記，再核對卡片）
          pass                 → passed
          revise / reject      → 帶問題清單重寫 1 次 → 程式檢查 → 複檢
                                   pass → passed（revised=True）
                                   否則 → escalate（待升級，交給更強的模型或人工）
中斷後重跑會從每篇停下的地方接續，已完成的步驟不重做、不重複花錢。

保護措施：
  --budget   這次執行的花費上限（人民幣）。每次呼叫前檢查，超過就不再送新的請求。
  餘額不足   API 回 402 時整批立刻停下。
  重試       暫時性錯誤最多重試 3 次，間隔 10 / 30 / 90 秒。

檔案：
  cards/<batch>/<id>.json            最終卡片；<id>.v1.json 是被退回前的版本
  eval/<batch>/<id>/notes.json       檢查員獨立讀全文的筆記
  eval/<batch>/<id>/check1.json      第一次檢查；check2.json 是重寫後的複檢
  eval/<batch>/_canary/              埋雷版卡片與檢查結果
"""
import argparse
import hashlib
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import batch as B
import judge_fable as jf
import trial as T
import trial_canary as TC
from common import PIPELINE
from validate import gate_v4

WRITER, CHECKER = "kimi-k3.1-pro", "deepseek-flash"
PRICE_CNY = {"kimi": (13, 68), "deepseek": (2, 8)}  # 每百萬 token（輸入, 輸出），原價；離峰與 batch 折扣不計，估算偏保守
RETRY_WAITS = [10, 30, 90]
BILLING_HINTS = ("402", "insufficient balance", "billing")


class Stop(Exception):
    """整批都該停下的情況：預算用完、餘額不足。"""


class Runner:
    def __init__(self, batch, budget, dry_run=False):
        self.batch, self.budget, self.dry_run = batch, budget, dry_run
        self.spent = 0.0
        self.lock = threading.Lock()
        self.stop_reason = None
        self.ctx = T.ctx()
        self.manifest = B.load()

    # ---- 花費與呼叫 ----
    def cost_cny(self, model, usage):
        pin, pout = next(v for k, v in PRICE_CNY.items() if model.startswith(k))
        u = usage or {}
        return round((u.get("input_tokens", 0) * pin + u.get("output_tokens", 0) * pout) / 1e6, 4)

    def call(self, model, prompt, schema, rec, step):
        """呼叫模型並記帳。回傳 structured output；失敗回傳 None。"""
        for attempt in range(len(RETRY_WAITS) + 1):
            with self.lock:
                if self.stop_reason:
                    raise Stop(self.stop_reason)
                if self.budget is not None and self.spent >= self.budget:
                    self.stop_reason = f"花費 ¥{self.spent:.2f} 已達預算 ¥{self.budget:.2f}"
                    raise Stop(self.stop_reason)
            out, meta = call_model(model, prompt, schema)
            cost = self.cost_cny(model, meta.get("usage"))
            with self.lock:
                self.spent += cost
                rec.setdefault("calls", []).append({"step": step, "model": model, "cny": cost,
                                                    "seconds": meta.get("seconds"), "ok": out is not None})
                rec["cost_cny"] = round(rec.get("cost_cny", 0) + cost, 4)
            if out is not None:
                return out
            err = (meta.get("error") or "").lower()
            if any(h in err for h in BILLING_HINTS):
                with self.lock:
                    self.stop_reason = f"{model} 餘額不足，整批停下：{meta.get('error', '')[:120]}"
                raise Stop(self.stop_reason)
            if attempt < len(RETRY_WAITS):
                time.sleep(RETRY_WAITS[attempt] if not FAST_RETRY else 0)
        return None

    # ---- 路徑 ----
    def paths(self, rid):
        cards = B.ROOT / "cards" / self.batch
        ev = B.ROOT / "eval" / self.batch / rid
        cards.mkdir(parents=True, exist_ok=True)
        ev.mkdir(parents=True, exist_ok=True)
        return cards, ev

    def fulltext(self, rid):
        return (B.ROOT / "fulltext" / self.batch / "clean" / f"{rid}.md").read_text()

    def save(self):
        with self.lock:
            B.save(self.manifest)

    # ---- 各步驟 ----
    def write_prompt(self, rid, fulltext):
        return jf.fill((PIPELINE / "card_prompt_v3.md").read_text(), strategy_types=self.ctx.st_text,
                       families=self.ctx.fams, methods=self.ctx.meths, report_id=rid, fulltext=fulltext)

    def write_card(self, rid, fulltext, rec, extra="", step="write"):
        """寫卡並過程式檢查；不過就帶錯誤清單重寫 1 次。回傳 (卡片, 程式檢查問題)。"""
        prompt = self.write_prompt(rid, fulltext) + extra
        card = self.call(WRITER, prompt, self.ctx.card_schema, rec, step)
        if card is None:
            return None, ["模型沒有回傳卡片"]
        problems = gate_v4(card, fulltext, self.ctx.card_schema)
        rec.setdefault("gate", []).append({"step": step, "problems": len(problems)})
        if problems:
            fix = (prompt + "\n\n## 你上一次写的卡片\n\n" + json.dumps(card, ensure_ascii=False, indent=2) +
                   "\n\n## 程序检查发现的问题\n\n" + "\n".join(f"- {p}" for p in problems) +
                   "\n\n请修正这些问题后重新输出完整的卡片。引文必须从原文逐字复制；找不到合适的原文就删掉那一条，不要改写引文。")
            card2 = self.call(WRITER, fix, self.ctx.card_schema, rec, step + "-gatefix")
            if card2 is not None:
                card, problems = card2, gate_v4(card2, fulltext, self.ctx.card_schema)
                rec["gate"].append({"step": step + "-gatefix", "problems": len(problems)})
        return card, problems

    def notes(self, rid, fulltext, ev, rec):
        p = ev / "notes.json"
        if p.exists():
            return json.loads(p.read_text())
        out = self.call(CHECKER, jf.fill((PIPELINE / "judge" / "pass1_prompt.md").read_text(),
                                         strategy_types=self.ctx.st_text, families=self.ctx.fams,
                                         methods=self.ctx.meths, report_id=rid, fulltext=fulltext),
                        jf.pass1_schema(self.ctx.vocab, self.ctx.st), rec, "notes")
        if out is not None:
            p.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        return out

    def check(self, rid, fulltext, card, notes, rec, step):
        n = json.dumps({"reference_card": notes["reference_card"], "must_capture": notes["must_capture"]},
                       ensure_ascii=False, indent=2)
        return self.call(CHECKER, jf.fill((PIPELINE / "check_prompt.md").read_text(), methods=self.ctx.meths,
                                          report_id=rid, fulltext=fulltext, notes=n,
                                          card=json.dumps(card, ensure_ascii=False, indent=2)),
                         T.check_schema(), rec, step)

    def process(self, rid):
        """一篇研報走完整個流程。每一步的產物都落檔，重跑時從缺的那一步接續。"""
        v = self.manifest[rid]
        rec = v.setdefault("card", {"state": "new"})
        if rec["state"] in ("passed", "escalate"):
            return rid, rec["state"] + "（已完成）"
        cards, ev = self.paths(rid)
        fulltext = self.fulltext(rid)
        final, v1 = cards / f"{rid}.json", cards / f"{rid}.v1.json"
        try:
            # 1 寫卡
            if not final.exists():
                card, problems = self.write_card(rid, fulltext, rec)
                if card is None:
                    return self.finish(rid, rec, "error", "寫卡失敗")
                final.write_text(json.dumps(card, ensure_ascii=False, indent=2))
                if problems:
                    return self.finish(rid, rec, "escalate", f"程式檢查重寫後仍有 {len(problems)} 個問題")
            card = json.loads(final.read_text())
            # 2 模型檢查
            notes = self.notes(rid, fulltext, ev, rec)
            if notes is None:
                return self.finish(rid, rec, "error", "檢查員筆記失敗")
            c1p = ev / "check1.json"
            if not c1p.exists():
                c1 = self.check(rid, fulltext, card, notes, rec, "check1")
                if c1 is None:
                    return self.finish(rid, rec, "error", "檢查失敗")
                c1p.write_text(json.dumps(c1, ensure_ascii=False, indent=2))
            c1 = json.loads(c1p.read_text())
            rec["check1"] = c1["verdict"]
            if c1["verdict"] == "pass":
                return self.finish(rid, rec, "passed")
            # 3 退回重寫 1 次再複檢
            c2p = ev / "check2.json"
            if not c2p.exists():
                if not v1.exists():
                    issues = [i for i in c1["issues"] if i["severity"] != "minor"]
                    extra = ("\n\n## 你上一次写的卡片\n\n" + json.dumps(card, ensure_ascii=False, indent=2) +
                             "\n\n## 审核员指出的问题\n\n" +
                             "\n".join(f"- [{i['severity']}] {i['field']}：卡片写「{i['card_says']}」；原文实际是：{i['source_says']}（原文：「{i['quote']}」，{i['section']}）"
                                       for i in issues) +
                             "\n\n请对照原文逐条核实这些问题：属实的就修正，其余内容保持不变。然后重新输出完整的卡片。")
                    new, problems = self.write_card(rid, fulltext, rec, extra, "rewrite")
                    if new is None:
                        return self.finish(rid, rec, "error", "重寫失敗")
                    v1.write_text(json.dumps(card, ensure_ascii=False, indent=2))
                    final.write_text(json.dumps(new, ensure_ascii=False, indent=2))
                    rec["revised"] = True
                    if problems:
                        return self.finish(rid, rec, "escalate", f"重寫後程式檢查仍有 {len(problems)} 個問題")
                card = json.loads(final.read_text())
                c2 = self.check(rid, fulltext, card, notes, rec, "check2")
                if c2 is None:
                    return self.finish(rid, rec, "error", "複檢失敗")
                c2p.write_text(json.dumps(c2, ensure_ascii=False, indent=2))
            c2 = json.loads(c2p.read_text())
            rec["check2"] = c2["verdict"]
            if c2["verdict"] == "pass":
                return self.finish(rid, rec, "passed")
            sev = [i["severity"] for i in c2["issues"]]
            return self.finish(rid, rec, "escalate",
                               f"重寫後複檢仍是 {c2['verdict']}（critical {sev.count('critical')}、major {sev.count('major')}）")
        except Stop as e:
            rec["state"] = "interrupted"
            self.save()
            return rid, f"停下：{e}"

    def finish(self, rid, rec, state, reason=None):
        rec["state"] = state
        if reason:
            rec["reason"] = reason
        else:
            rec.pop("reason", None)
        self.save()
        return rid, state + (f"：{reason}" if reason else "") + f"  ¥{rec.get('cost_cny', 0):.2f}"


FAST_RETRY = False  # 測試時設成 True，重試不等待


def call_model(model, prompt, schema):
    """實際送 API 的唯一入口（測試時替換這個函式）。回傳 (structured output 或 None, meta)。"""
    try:
        res, tail, secs = jf.run_deepseek(prompt, schema, "high", model)  # Kimi 與 DeepSeek 都走 Anthropic 相容端點
    except Exception as e:
        return None, {"error": f"{type(e).__name__}: {str(e)[:300]}", "seconds": 0}
    meta = {"seconds": secs, "usage": (res or {}).get("usage")}
    out = (res or {}).get("structured_output")
    if not out:
        meta["error"] = tail[-600:]
    return out, meta


def ready(manifest, batch):
    return sorted(rid for rid, v in manifest.items() if v["batch"] == batch and v["status"] == "cleaned")


def cmd_cards(args):
    r = Runner(args.batch, args.budget, args.dry_run)
    todo = [rid for rid in ready(r.manifest, args.batch)
            if r.manifest[rid].get("card", {}).get("state") not in ("passed", "escalate")]
    if args.limit:
        todo = todo[:args.limit]
    chars = [r.manifest[rid]["chars_clean"] for rid in todo]
    w, c = B.estimate(chars, "kimi", "deepseek") if chars else (0, 0)
    print(f"批次 {args.batch}：待處理 {len(todo)} 篇；估計寫卡 ¥{w:.0f} ＋ 檢查 ¥{c:.0f} ＝ ¥{w + c:.0f}"
          f"（原價、不含重寫；預算 {'未設' if args.budget is None else f'¥{args.budget:.0f}'}）")
    if args.dry_run:
        for rid in todo[:10]:
            print(f"  會處理 {rid}（{r.manifest[rid]['chars_clean']} 字，目前 {r.manifest[rid].get('card', {}).get('state', 'new')}）")
        print("（--dry-run：沒有送出任何請求）")
        return
    if args.budget is None:
        raise SystemExit("真的送出時必須給 --budget")
    with ThreadPoolExecutor(args.workers) as ex:
        for rid, status in ex.map(r.process, todo):
            print(f"{rid}  {status}", flush=True)
    print(f"\n這次花費 ¥{r.spent:.2f}" + (f"；提前停下：{r.stop_reason}" if r.stop_reason else ""))
    cmd_report(args)


def cmd_report(args):
    m = B.load()
    rows = {rid: v["card"] for rid, v in m.items() if v["batch"] == args.batch and "card" in v}
    if not rows:
        print("這個批次還沒有寫卡紀錄")
        return
    n = len(rows)
    st = lambda s: sum(r["state"] == s for r in rows.values())
    checked = [r for r in rows.values() if "check1" in r]
    back = [r for r in checked if r["check1"] != "pass"]
    gate_first = [r for r in rows.values() if r.get("gate")]
    print(f"\n批次 {args.batch}：有寫卡紀錄 {n} 篇")
    print(f"  通過 {st('passed')}（其中重寫後才過 {sum(r['state'] == 'passed' and r.get('revised', False) for r in rows.values())}）"
          f"｜待升級 {st('escalate')}｜錯誤 {st('error')}｜中斷 {st('interrupted')}")
    if gate_first:
        ok = sum(r["gate"][0]["problems"] == 0 for r in gate_first)
        print(f"  程式檢查第一次通過 {ok}/{len(gate_first)}（{ok / len(gate_first):.0%}）")
    if checked:
        print(f"  模型檢查退回率 {len(back)}/{len(checked)}（{len(back) / len(checked):.0%}）")
        print(f"  升級率 {st('escalate')}/{len(checked)}（{st('escalate') / len(checked):.0%}）")
    total = sum(r.get("cost_cny", 0) for r in rows.values())
    done = st("passed")
    print(f"  累計花費 ¥{total:.2f}；每張通過的卡 ¥{total / done:.2f}" if done else f"  累計花費 ¥{total:.2f}")
    for rid, r in rows.items():
        if r["state"] in ("escalate", "error"):
            print(f"    {r['state']} {rid}: {r.get('reason')}")
    cn = B.ROOT / "eval" / args.batch / "_canary" / "_canaries.json"
    if cn.exists():
        cs = [c for c in json.loads(cn.read_text()) if "caught" in c]
        for typ in sorted({c["type"] for c in cs}):
            sub = [c for c in cs if c["type"] == typ]
            print(f"  埋雷 {typ}：抓到 {sum(c['caught'] for c in sub)}/{len(sub)}")


# ---- 埋雷（持續監控檢查員）----
LOGIC_SCHEMA = jf.obj({"new_text": jf.s, "what_changed": jf.s})


def make_canary(r, rid, card, rec):
    """在卡片上改一處，回傳埋雷資訊。類型依 id 輪流；做不出來就換下一類。"""
    rng = random.Random(f"canary-{rid}")
    order = TC.TYPES[int(hashlib.sha1(rid.encode()).hexdigest(), 16) % 3:] + TC.TYPES
    for typ in order:
        if typ == "number":
            info = TC.canary_number(card, rng)
        elif typ == "tag":
            info = TC.canary_tag(card, rng, r.ctx.vocab)
        else:
            # 改寫用寫卡模型（Kimi），和檢查員（DeepSeek）不同家；不使用 Claude
            steps = card["method_steps"]
            use_core = rng.random() < 0.5 or not steps
            k = rng.randrange(min(len(steps), 4)) if steps else 0
            path, target = ("core_idea", card["core_idea"]) if use_core else (f"method_steps.{k}.step", steps[k]["step"])
            context = json.dumps({"core_idea": card["core_idea"], "method_steps": [x["step"] for x in steps]},
                                 ensure_ascii=False, indent=2)
            out = r.call(WRITER, jf.fill(TC.LOGIC_PROMPT, context=context, path=path, target=target),
                         LOGIC_SCHEMA, rec, "canary-make")
            info = None
            if out and out["new_text"].strip() != target.strip():
                if use_core:
                    card["core_idea"] = out["new_text"]
                else:
                    steps[k]["step"] = out["new_text"]
                info = {"field": path, "old": target, "new": out["new_text"], "desc": out["what_changed"]}
        if info:
            return {"type": typ, **info}
    return None


def cmd_canary(args):
    if args.budget is None:
        raise SystemExit("必須給 --budget")
    r = Runner(args.batch, args.budget)
    d = B.ROOT / "eval" / args.batch / "_canary"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "_canaries.json"
    canaries = json.loads(path.read_text()) if path.exists() else []
    done = {c["rid"] for c in canaries}
    passed = sorted(rid for rid, v in r.manifest.items()
                    if v["batch"] == args.batch and v.get("card", {}).get("state") == "passed")
    # 依 id 的雜湊固定抽樣：每 every 張抽 1 張，重跑會抽到同一批
    picks = [rid for rid in passed if int(hashlib.sha1(rid.encode()).hexdigest(), 16) % args.every == 0 and rid not in done]
    print(f"通過的卡 {len(passed)} 張，這次埋雷 {len(picks)} 張（每 {args.every} 張 1 張；已做過 {len(done)} 張）")
    for rid in picks:
        rec = {}
        try:
            fulltext = r.fulltext(rid)
            card = json.loads((B.ROOT / "cards" / args.batch / f"{rid}.json").read_text())
            info = make_canary(r, rid, card, rec)
            if not info:
                print(f"{rid}: 做不出埋雷")
                continue
            if gate_v4(card, fulltext, r.ctx.card_schema):
                print(f"{rid}: 埋雷會被程式檢查擋下，不算（{info['type']}）")
                continue
            (d / f"{rid}.json").write_text(json.dumps(card, ensure_ascii=False, indent=2))
            notes = json.loads((B.ROOT / "eval" / args.batch / rid / "notes.json").read_text())
            res = r.check(rid, fulltext, card, notes, rec, "canary-check")
            if res is None:
                print(f"{rid}: 檢查失敗")
                continue
            (d / f"{rid}.check.json").write_text(json.dumps(res, ensure_ascii=False, indent=2))
            key = info["field"].split(".")[0]
            hits = [i for i in res["issues"] if key in i["field"] and i["severity"] != "minor"]
            # 抓到 = 卡片沒有被放行，而且有一個非 minor 的問題落在被改的欄位上
            caught = res["verdict"] != "pass" and bool(hits)
            canaries.append({"rid": rid, **info, "verdict": res["verdict"], "caught": caught,
                             "cost_cny": rec.get("cost_cny", 0)})
            path.write_text(json.dumps(canaries, ensure_ascii=False, indent=2))
            print(f"{rid} [{info['type']}] {'抓到' if caught else '沒抓到'}（{res['verdict']}）: {info['desc'][:80]}")
        except Stop as e:
            print(f"停下：{e}")
            break
    print(f"這次花費 ¥{r.spent:.2f}")


# ---- 人工複核表：檢查員判的 major / critical 是真是假 ----
def review_items(args):
    """回傳 [(標籤, 檢查結果路徑, 全文路徑)]。"""
    if args.trial:
        d = B.KB / "eval" / "trial" / "check-deepseek-flash"
        return [(f"{p.parent.name} / {p.name.split('.')[0]} 寫的卡", p, T.SRC / f"{p.parent.name}.md")
                for p in sorted(d.glob("*/*.clean.json"))]
    d = B.ROOT / "eval" / args.batch
    ft = B.ROOT / "fulltext" / args.batch / "clean"
    return [(f"{p.parent.name} / {'第一次檢查' if p.name == 'check1.json' else '重寫後複檢'}", p, ft / f"{p.parent.name}.md")
            for p in sorted(d.glob("*/check[12].json"))]


def cmd_review(args):
    from validate import _norm, quote_found
    out = ["# 檢查員判的 critical / major：人工複核表", "",
           "每一條對照原文判斷：**真**（卡片確實錯）、**誤報**（卡片沒錯）、**吹毛求疵**（不算錯但可改）。",
           "「引文」是檢查員自己附的原文證據；「引文在全文」是程式比對的結果，找不到的優先懷疑是誤報。", ""]
    n = 0
    for label, p, ftp in review_items(args):
        res = json.loads(p.read_text())
        ft = _norm(ftp.read_text())
        for i in res["issues"]:
            if i["severity"] == "minor":
                continue
            n += 1
            out += [f"## {n}. {label}", "",
                    f"- 嚴重度 / 類型：**{i['severity']}** / {i['type']}（整張卡判 {res['verdict']}）",
                    f"- 欄位：`{i['field']}`",
                    f"- 卡片寫：{i['card_says']}",
                    f"- 檢查員說原文是：{i['source_says']}",
                    f"- 引文：「{i['quote']}」（{i['section']}）",
                    f"- 引文在全文：{'找得到' if quote_found(i['quote'], ft) else '**找不到**'}",
                    "- 人工判定：＿＿（真 / 誤報 / 吹毛求疵）", ""]
    dest = PIPELINE / "results" / f"major_review_{'trial' if args.trial else args.batch}.md"
    dest.write_text("\n".join(out))
    print(f"{n} 條 → {dest}")


# ---- 分流規則：只看檔名能不能認出週報類 ----
WEEKLY_RE = __import__("re").compile(r"(周报|週報|周度|月报|月度|日报|晨报|观市|觀市|点评|點評|快评|速递|跟踪周|市场回顾|一周)")


def cmd_triage(args):
    if args.trial:
        idx = json.loads((B.KB / "fulltext" / "poc" / "basic" / "_index.json").read_text())
        rows = [(idx[p.stem]["source_pdf"], json.loads(p.read_text())["report_type"])
                for p in sorted((T.CARDS / "kimi").glob("*.json")) if ".meta" not in p.name and ".attempt" not in p.name]
    else:
        m = B.load()
        rows = [(m[p.stem]["source_pdf"], json.loads(p.read_text())["report_type"])
                for p in sorted((B.ROOT / "cards" / args.batch).glob("*.json")) if ".v1" not in p.name]
    tp = fp = fn = tn = 0
    for name, rtype in rows:
        pred, actual = bool(WEEKLY_RE.search(name)), rtype == "周报点评"
        tp += pred and actual
        fp += pred and not actual
        fn += actual and not pred
        tn += not pred and not actual
        if pred != actual:
            print(f"  不一致：規則說{'是' if pred else '不是'}週報類，卡片是「{rtype}」：{name[:60]}")
    print(f"{len(rows)} 篇：卡片標成週報點評 {tp + fn} 篇；規則認出 {tp} 篇，漏掉 {fn} 篇，"
          f"把非週報誤判成週報 {fp} 篇（這種最傷，會讓有內容的研報只拿到簡卡）")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ["cards", "canary", "report", "review", "triage"]:
        p = sub.add_parser(name)
        p.add_argument("batch", nargs="?" if name in ("review", "triage") else None)
        if name in ("review", "triage"):
            p.add_argument("--trial", action="store_true", help="用試產 20 篇的資料")
        if name in ("cards", "canary"):
            p.add_argument("--budget", type=float, help="這次執行的花費上限（人民幣）")
        if name == "cards":
            p.add_argument("--workers", type=int, default=5)
            p.add_argument("--limit", type=int, help="只處理前 N 篇")
            p.add_argument("--dry-run", action="store_true", help="只列出會做什麼，不送出")
        if name == "canary":
            p.add_argument("--every", type=int, default=20)
    args = ap.parse_args()
    globals()[f"cmd_{args.cmd}"](args)


if __name__ == "__main__":
    main()
