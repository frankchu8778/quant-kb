"""量產的模型階段（會送 API、會花錢）：寫卡 → 程式檢查 → 模型檢查 → 退回重寫再複檢 → 埋雷監控。

用法（<batch> 要先跑完 batch.py 的本機階段）：
  python run.py probe   <batch> --budget 5                # 用 batch 的請求格式即時試打 1 篇，驗證格式
  python run.py submit  <batch> --limit 5 --budget 60 [--dry-run]   # 第一次寫卡送進 Kimi batch（六折），每 20 篇一個 batch
  python run.py collect <batch> --budget 60 [--wait]      # 收回完成的 batch，接著做程式檢查、模型檢查、重寫
  python run.py cards  <batch> --budget 150 [--dry-run]   # 備援：全部走即時 API（原價）
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

batch 模式：只有第一次寫卡走 batch（每篇一個任務，custom_id 就是研報 id）；
程式檢查不過的重寫、檢查員退回的重寫走即時，免得每篇要等好幾輪。
送出前會查兩個平台的餘額，任一邊低於這一步估計花費的 1.3 倍就不送。

監控（monitor.py）：每做完一篇重算程式檢查通過率、退回率、升級率、失敗率、埋雷漏抓率；
  超過警戒就不再開始新的篇（已在跑的做完），之後重跑也會先擋下，處理過再加 --ignore-alarms。
  cards / collect 跑完會自動在新通過的卡上埋雷（每 20 張 1 個）、抽 5% 給 MiniMax 審計；
  不要的話加 --no-canary / --no-audit。

保護措施：
  --budget   這次執行的花費上限（人民幣）。每次呼叫前檢查，超過就不再送新的請求。
  餘額不足   API 回 402 時整批立刻停下。
  重試       暫時性錯誤最多重試 3 次，間隔 10 / 30 / 90 秒。

檔案：
  cards/<batch>/<id>.json            最終卡片；<id>.v1.json 是被退回前的版本
  eval/<batch>/<id>/kimi_write.json  第一次寫卡的結果（batch 或即時）
  eval/<batch>/_kimi_batches/        送出的 JSONL 與收回的原始結果
  catalog/kimi_batches.json          每個 batch 的 id、任務清單、狀態、等待時間
  eval/<batch>/<id>/notes.json       檢查員獨立讀全文的筆記
  eval/<batch>/<id>/check1.json      第一次檢查；check2.json 是重寫後的複檢
  eval/<batch>/_canary/              埋雷版卡片與檢查結果
"""
import argparse
import copy
import fcntl
import hashlib
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import batch as B
import kimi_batch as KB
import judge_fable as jf
import monitor
import offpeak
import trial as T
import trial_canary as TC
from common import PIPELINE
from validate import gate_v4

WRITER, CHECKER = "kimi-k3.1-pro", "deepseek-flash"
CANARY_EVERY = 20   # 量產時每 20 張通過的卡埋 1 個雷
AUDIT_PCT = 5       # 量產時抽 5% 通過的卡審計（PLAN：前 200 篇 5%，漏抓率穩定後降到 2%）
PRICE_CNY = {"kimi-batch": (7.8, 40.8), "kimi": (13, 68), "deepseek": (2, 8), "MiniMax": (1.1, 4.3)}  # 每百萬 token（輸入, 輸出），原價；離峰與 batch 折扣不計，估算偏保守
RETRY_WAITS = [10, 30, 90]
BILLING_HINTS = ("402", "insufficient balance", "billing")
SAFETY = 1.3              # 餘額要有估計花費的幾倍才送
BATCH_ACTUAL_RATIO = 1.66  # batch 實際花費 / 送出時的估計（batch1：¥18.11/¥10.90、¥5.21/¥3.20）
REWRITE_SHARE = 0.25      # 估計有多少比例的篇會需要即時重寫（試產約一到兩成，取保守值）
RECHECK_SHARE = 0.20      # 估計有多少比例的篇會需要複檢
REGISTRY = B.ROOT / "catalog" / "kimi_batches.json"


class Stop(Exception):
    """整批都該停下的情況：預算用完、餘額不足。"""


class Waiting(Exception):
    """第一次寫卡還在 Kimi batch 裡，這篇先停在這裡。"""


class OffPeakWait(Exception):
    """輪到 DeepSeek 的步驟，但現在是尖峰時段。這篇先停在這裡，離峰再接續。"""


class Runner:
    def __init__(self, batch, budget, dry_run=False, mode="realtime"):
        self.batch, self.budget, self.dry_run, self.mode = batch, budget, dry_run, mode
        self.spent = 0.0
        self.lock = threading.Lock()
        self.stop_reason = None
        self.alarm = None            # 監控警報（monitor.py）：有值就不再開始新的篇
        self.ignore_alarms = False
        self.ctx = T.ctx()
        self.manifest = B.load()
        self._saved = {rid: copy.deepcopy(v.get("card")) for rid, v in self.manifest.items()}

    # ---- 花費與呼叫 ----
    def cost_cny(self, model, usage):
        pin, pout = PRICE_CNY[model] if model in PRICE_CNY else next(v for k, v in PRICE_CNY.items() if model.startswith(k))
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
            if offpeak.blocked(model):
                raise OffPeakWait()
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
        """只把這個程式改過的卡片紀錄寫回 manifest。

        `cards`（即時）和 `collect`（等 batch）可能同時在跑，各自手上有一份 manifest。
        整份覆蓋會把對方剛寫的紀錄蓋掉，所以存檔時重新讀檔、只換掉自己改過的篇。
        """
        with self.lock, catalog_lock():
            disk = B.load()
            for rid, v in self.manifest.items():
                if v.get("card") != self._saved.get(rid) and rid in disk:
                    disk[rid]["card"] = copy.deepcopy(v["card"])
                    self._saved[rid] = copy.deepcopy(v["card"])
            B.save(disk)

    # ---- 各步驟 ----
    def write_prompt(self, rid, fulltext):
        return jf.fill((PIPELINE / "card_prompt_v3.md").read_text(), strategy_types=self.ctx.st_text,
                       families=self.ctx.fams, methods=self.ctx.meths, report_id=rid, fulltext=fulltext)

    def write_card(self, rid, fulltext, rec, extra="", step="write"):
        """寫卡並過程式檢查；不過就帶錯誤清單重寫 1 次。回傳 (卡片, 程式檢查問題)。"""
        prompt = self.write_prompt(rid, fulltext) + extra
        card = self.first_write(rid, prompt, rec) if step == "write" else \
            self.call(WRITER, prompt, self.ctx.card_schema, rec, step)
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

    def first_write(self, rid, prompt, rec):
        """第一次寫卡：結果已落檔就用；batch 模式下還沒回來就等；即時模式直接呼叫。"""
        p = self.paths(rid)[1] / "kimi_write.json"
        if p.exists():
            data = json.loads(p.read_text())
            if data.get("card") is not None or data.get("retried"):
                return data.get("card")
            # batch 任務失敗（模型沒回傳合法卡片）：即時重試 1 次
            card = self.call(WRITER, prompt, self.ctx.card_schema, rec, "write-retry")
            p.write_text(json.dumps({**data, "card": card, "retried": True}, ensure_ascii=False, indent=2))
            return card
        if self.mode == "batch":
            raise Waiting()
        card = self.call(WRITER, prompt, self.ctx.card_schema, rec, "write")
        p.write_text(json.dumps({"card": card, "via": "realtime"}, ensure_ascii=False, indent=2))
        return card

    def notes(self, rid, fulltext, ev, rec):
        p = ev / "notes.json"
        if p.exists():
            # 筆記可能是 `notes` 指令先跑好的：把那次的花費併進這篇的紀錄（只併一次）
            meta = ev / "notes_meta.json"
            if meta.exists() and not any(c["step"] == "notes" for c in rec.get("calls", [])):
                m = json.loads(meta.read_text())
                rec.setdefault("calls", []).extend(m["calls"])
                rec["cost_cny"] = round(rec.get("cost_cny", 0) + m["cost_cny"], 4)
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
        if self.alarm:
            return rid, "沒開始：監控警報"
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
        except Waiting:
            rec["state"] = "waiting"
            self.save()
            return rid, "等待 Kimi batch"
        except OffPeakWait:
            rec["state"] = "waiting_offpeak"
            self.save()
            return rid, f"等離峰（{offpeak.next_offpeak():%H:%M} 起）再做 DeepSeek 檢查"
        except Stop as e:
            rec["state"] = "interrupted"
            self.save()
            return rid, f"停下：{e}"

    def check_alarms(self):
        """重算監控指標；超標就設 self.alarm，之後不再開始新的篇。回傳警報清單。"""
        if self.ignore_alarms:
            return []
        with self.lock:
            found = monitor.alarms(self.manifest, self.batch)
            if found and not self.alarm:
                self.alarm = "；".join(f"{a[0]} {a[1]}" for a in found)
                print(f"\n!! 監控警報：{self.alarm}\n!! 不再開始新的篇，已經在跑的會做完", flush=True)
        return found

    def finish(self, rid, rec, state, reason=None):
        rec["state"] = state
        if reason:
            rec["reason"] = reason
        else:
            rec.pop("reason", None)
        self.save()
        self.check_alarms()
        return rid, state + (f"：{reason}" if reason else "") + f"  ¥{rec.get('cost_cny', 0):.2f}"


FAST_RETRY = False  # 測試時設成 True，重試不等待
EFFORT = {}  # 模型名稱前綴 → 推理強度；沒列的用 high（audit.py 會設 MiniMax）


def call_model(model, prompt, schema):
    """實際送 API 的唯一入口（測試時替換這個函式）。回傳 (structured output 或 None, meta)。"""
    try:
        effort = next((v for k, v in EFFORT.items() if model.startswith(k)), "high")
        res, tail, secs = jf.run_deepseek(prompt, schema, effort, model)  # Kimi、DeepSeek、MiniMax 都走 Anthropic 相容端點
    except offpeak.PeakHours:
        raise OffPeakWait()   # 跑到一半進入尖峰：不算失敗、不重試，這篇等離峰
    except Exception as e:
        return None, {"error": f"{type(e).__name__}: {str(e)[:300]}", "seconds": 0}
    meta = {"seconds": secs, "usage": (res or {}).get("usage")}
    out = (res or {}).get("structured_output")
    if not out:
        meta["error"] = tail[-600:]
    return out, meta


def ready(manifest, batch):
    return sorted(rid for rid, v in manifest.items() if v["batch"] == batch and v["status"] == "cleaned")


# ---- Kimi batch：登記簿、餘額檢查、送出、收回 ----
def load_reg():
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else []


def catalog_lock():
    """跨程式的檔案鎖：manifest 與 batch 登記簿的「讀檔—合併—寫檔」不會和另一個程式交錯。"""
    class _Lock:
        def __enter__(self):
            REGISTRY.parent.mkdir(parents=True, exist_ok=True)
            self.f = open(REGISTRY.parent / ".lock", "w")
            fcntl.flock(self.f, fcntl.LOCK_EX)

        def __exit__(self, *a):
            fcntl.flock(self.f, fcntl.LOCK_UN)
            self.f.close()
    return _Lock()


def save_reg(reg):
    """以 batch id 合併後存檔：手上這份有的條目以手上為準，檔案裡多出來的（別的程式剛送出的）保留。"""
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    with catalog_lock():
        mine = {e["id"]: e for e in reg}
        disk = load_reg()
        merged = [mine.pop(e["id"], e) for e in disk] + list(mine.values())
        tmp = REGISTRY.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2))
        tmp.replace(REGISTRY)
    reg[:] = merged


def pending(r, batch):
    """還沒有第一次寫卡結果、也不在任何未收回 batch 裡的篇。"""
    flying = {rid for e in load_reg() if not e["collected"] for rid in e["rids"]}
    return [rid for rid in ready(r.manifest, batch)
            if r.manifest[rid].get("card", {}).get("state") not in ("passed", "escalate")
            and rid not in flying
            and not (B.ROOT / "eval" / batch / rid / "kimi_write.json").exists()]


def preflight(chars, write_via, label, kimi_extra=0.0, ds_extra=0.0, write_chars=None):
    """送出前的餘額檢查。回傳 True 才可以送。

    需求量用這批每篇的實際字數算：
      Kimi     = 第一次寫卡（write_via 的價格）＋ REWRITE_SHARE 的篇數即時重寫
      DeepSeek = 筆記與檢查（尖峰價）＋ RECHECK_SHARE 的複檢
    任一邊餘額 < 需求 × SAFETY 就不通過；餘額查不到也不通過。
    """
    wc = chars if write_chars is None else write_chars   # 卡片已經寫好的篇不用估第一次寫卡
    first = B.estimate(wc, write_via, "deepseek")[0] if wc and write_via else 0.0
    rewrite, check = B.estimate(chars, "kimi", "deepseek") if chars else (0.0, 0.0)
    # 已送出、還沒收回的 Kimi batch 可能還沒扣款：它們的花費也要留在餘額裡
    flying = [e for e in load_reg() if not e["collected"]]
    held = sum(e.get("est_cny", 0) for e in flying) * BATCH_ACTUAL_RATIO
    need = {"Kimi": first + REWRITE_SHARE * rewrite + kimi_extra + held,
            "DeepSeek": check * (1 + RECHECK_SHARE) + ds_extra}
    ok = True
    print(f"餘額檢查（{label}）：")
    if flying:
        print(f"  （Kimi 需求含 {len(flying)} 個還沒收回的 batch 約 ¥{held:.2f}：送出時的估計 × {BATCH_ACTUAL_RATIO}）")
    for name, fn in [("Kimi", KB.balance), ("DeepSeek", KB.deepseek_balance)]:
        try:
            bal = fn()
        except Exception as e:
            print(f"  {name:<8} 餘額查詢失敗（{str(e)[:80]}）→ 不送")
            ok = False
            continue
        want = need[name] * SAFETY
        enough = bal >= want
        ok &= enough
        print(f"  {name:<8} 估計花費 ¥{need[name]:6.2f}，要求餘額 ≥ ¥{want:6.2f}，目前 ¥{bal:7.2f}  "
              + ("夠" if enough else f"不夠，還差 ¥{want - bal:.2f}"))
    return ok


def cmd_probe(args):
    """用和 batch 完全相同的請求內容打即時端點（原價），驗證格式。"""
    if args.budget is None:
        raise SystemExit("必須給 --budget")
    r = Runner(args.batch, args.budget)
    todo = pending(r, args.batch)[:args.n]
    chars = [r.manifest[rid]["chars_clean"] for rid in todo]
    if not preflight(chars, "kimi", f"即時試打 {len(todo)} 篇"):
        raise SystemExit("餘額不足，沒有送出")
    for rid in todo:
        _, ev = r.paths(rid)
        body = KB.chat_body(r.write_prompt(rid, r.fulltext(rid)), r.ctx.card_schema)
        t0 = time.time()
        resp = KB.chat_realtime(body)
        secs = round(time.time() - t0)
        (ev / "kimi_probe_raw.json").write_text(json.dumps(resp, ensure_ascii=False, indent=2))
        out, err, usage = KB.parse_response(resp, r.ctx.card_schema)
        cost = r.cost_cny("kimi", usage)
        rec = r.manifest[rid].setdefault("card", {"state": "new"})
        rec.setdefault("calls", []).append({"step": "write", "model": "kimi(openai 格式即時)", "cny": cost,
                                            "seconds": secs, "ok": out is not None})
        rec["cost_cny"] = round(rec.get("cost_cny", 0) + cost, 4)
        r.spent += cost
        (ev / "kimi_write.json").write_text(json.dumps({"card": out, "error": err, "usage": usage,
                                                        "via": "realtime-openai"}, ensure_ascii=False, indent=2))
        msg = resp["choices"][0]["message"]
        print(f"{rid}: {secs} 秒，usage {usage}，¥{cost:.2f}；finish={resp['choices'][0].get('finish_reason')}；"
              f"tool_calls={len(msg.get('tool_calls') or [])}；{'卡片合法' if out is not None else '失敗：' + err}")
        r.save()
        if out is not None:
            print("  後續：", r.process(rid)[1])
    print(f"這次花費 ¥{r.spent:.2f}")


def cmd_submit(args):
    r = Runner(args.batch, args.budget, mode="batch")
    todo = pending(r, args.batch)
    if args.limit:
        todo = todo[:args.limit]
    chunks = [todo[i:i + args.chunk] for i in range(0, len(todo), args.chunk)]
    chars = [r.manifest[rid]["chars_clean"] for rid in todo]
    est = B.estimate(chars, "kimi-batch", "deepseek")[0] if chars else 0.0
    print(f"批次 {args.batch}：要送 {len(todo)} 個任務（每篇一個），分成 {len(chunks)} 個 batch（每個最多 {args.chunk} 篇）；"
          f"第一次寫卡估計 ¥{est:.2f}（batch 價）")
    if not todo:
        return
    ok = preflight(chars, "kimi-batch", f"送出 {len(todo)} 篇")
    if args.dry_run:
        for i, ch in enumerate(chunks):
            print(f"  batch {i + 1}: {len(ch)} 篇，例如 {ch[0]}")
        print("（--dry-run：沒有上傳、沒有建立任何 batch）")
        return
    if not ok and not args.dry_run:
        if not args.ignore_balance:
            raise SystemExit("餘額不足，沒有送出任何 batch（使用者明說可以先送、之後再儲值，才加 --ignore-balance）")
        print("!! 餘額不足，但加了 --ignore-balance：照樣送出。收回前要儲值，否則 batch 可能失敗或收回後的檢查停下")
    if args.budget is None:
        raise SystemExit("真的送出時必須給 --budget")
    if est > args.budget:
        raise SystemExit(f"估計 ¥{est:.2f} 超過預算 ¥{args.budget:.2f}，沒有送出")
    out_dir = B.ROOT / "eval" / args.batch / "_kimi_batches"
    out_dir.mkdir(parents=True, exist_ok=True)
    reg = load_reg()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for i, ch in enumerate(chunks):
        path = out_dir / f"{stamp}_{i + 1}.jsonl"
        with open(path, "w") as f:
            for rid in ch:
                body = KB.chat_body(r.write_prompt(rid, r.fulltext(rid)), r.ctx.card_schema)
                f.write(json.dumps({"custom_id": rid, "method": "POST", "url": "/v1/chat/completions", "body": body},
                                   ensure_ascii=False) + "\n")
        fid = KB.upload(path)
        info = KB.create(fid, args.window, {"kb_batch": args.batch})
        reg.append({"id": info["id"], "batch": args.batch, "input_file_id": fid, "jsonl": str(path), "rids": ch,
                    "status": info.get("status"), "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "created_ts": time.time(), "est_cny": round(B.estimate([r.manifest[x]["chars_clean"] for x in ch],
                                                                           "kimi-batch", "deepseek")[0], 2),
                    "collected": False})
        save_reg(reg)   # 每建立一個就存，中途失敗也不會重複送
        for rid in ch:
            r.manifest[rid].setdefault("card", {"state": "new"})["state"] = "waiting"
        r.save()
        print(f"  已送出 batch {i + 1}/{len(chunks)}：{info['id']}（{len(ch)} 篇，狀態 {info.get('status')}）")
    print("收回：python run.py collect " + args.batch + " --budget N [--wait]")


def run_with_offpeak(r, todo, args):
    """逐篇走流程。DeepSeek 的步驟遇到尖峰就停在那裡；加了 --wait-offpeak 會睡到離峰再接續。

    尖峰時段 Kimi 的步驟（程式檢查不過的重寫）照常做，只有 DeepSeek 的筆記與檢查會等。
    """
    r.ignore_alarms = getattr(args, "ignore_alarms", False)
    if r.check_alarms():
        print("開始前就有監控警報，這次不處理任何篇。看過、處理過之後要繼續，加 --ignore-alarms")
        return
    for _ in range(4):   # 一天最多跨兩個尖峰時段，4 輪足夠
        if r.alarm:
            return
        with ThreadPoolExecutor(args.workers) as ex:
            for rid, status in ex.map(r.process, todo):
                print(f"{rid}  {status}", flush=True)
        todo = [rid for rid in todo if r.manifest[rid].get("card", {}).get("state") == "waiting_offpeak"]
        if not todo:
            return
        until = offpeak.next_offpeak()
        if not getattr(args, "wait_offpeak", False):
            print(f"\n{len(todo)} 篇停在 DeepSeek 檢查之前：現在是尖峰時段，{until:%H:%M} 起是離峰。"
                  f"到時候重跑同一個指令即可（或加 --wait-offpeak 讓程式自己等）")
            return
        secs = max(0, (until - datetime.now(offpeak.TZ)).total_seconds()) + 30
        print(f"\n{len(todo)} 篇等離峰：睡到北京時間 {until:%H:%M}（約 {secs / 60:.0f} 分鐘）", flush=True)
        time.sleep(secs)


def collect_one(r, e, info):
    """把一個已結束的 batch 的結果寫回每篇的 kimi_write.json 並記帳。回傳 (成功數, 花費)。"""
    out_dir = B.ROOT / "eval" / e["batch"] / "_kimi_batches"
    results, n_ok, cost_total = {}, 0, 0.0
    for key, fname in [("output_file_id", "output"), ("error_file_id", "error")]:
        if info.get(key):
            text = KB.download(info[key])
            (out_dir / f"{e['id']}.{fname}.jsonl").write_text(text)
            for line in text.splitlines():
                if line.strip():
                    row = json.loads(line)
                    results.setdefault(row.get("custom_id"), row)
    for rid in e["rids"]:
        row = results.get(rid)
        body = ((row or {}).get("response") or {}).get("body")
        if row is None:
            out, err, usage = None, f"結果檔裡沒有這個任務（batch 狀態 {info.get('status')}）", {}
        elif row.get("error") or not body or (row["response"].get("status_code") or 200) >= 400:
            out, err, usage = None, f"任務失敗：{json.dumps(row.get('error') or body, ensure_ascii=False)[:300]}", {}
        else:
            out, err, usage = KB.parse_response(body, r.ctx.card_schema)
        cost = r.cost_cny("kimi-batch", usage)
        cost_total += cost
        n_ok += out is not None
        _, ev = r.paths(rid)
        (ev / "kimi_write.json").write_text(json.dumps({"card": out, "error": err, "usage": usage, "via": "batch",
                                                        "batch_id": e["id"]}, ensure_ascii=False, indent=2))
        rec = r.manifest[rid].setdefault("card", {"state": "new"})
        rec.setdefault("calls", []).append({"step": "write", "model": "kimi-batch", "cny": cost, "ok": out is not None})
        rec["cost_cny"] = round(rec.get("cost_cny", 0) + cost, 4)
        rec["state"] = "written" if out is not None else "write_failed"
    r.save()
    return n_ok, cost_total


def cmd_collect(args):
    if args.budget is None:
        raise SystemExit("必須給 --budget（收回後的檢查與重寫會花錢）")
    r = Runner(args.batch, args.budget)
    reg, t_start = load_reg(), time.time()
    while True:
        save_reg(reg)   # 合併：讀進別的程式剛送出的 batch
        open_entries = [e for e in reg if e["batch"] == args.batch and not e["collected"]]
        for e in open_entries:
            info = KB.retrieve(e["id"])
            e["status"], e["request_counts"] = info.get("status"), info.get("request_counts")
            if info.get("status") in KB.OPEN_STATUS:
                continue
            n_ok, cost = collect_one(r, e, info)
            e.update(collected=True, finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                     wait_minutes=round((time.time() - e["created_ts"]) / 60, 1),
                     ok=n_ok, cost_cny=round(cost, 2))
            print(f"收回 {e['id']}：狀態 {e['status']}，{n_ok}/{len(e['rids'])} 張卡合法，"
                  f"等了 {e['wait_minutes']} 分鐘，batch 價 ¥{cost:.2f}（估計 ¥{e['est_cny']:.2f}）", flush=True)
        save_reg(reg)
        still = [e for e in reg if e["batch"] == args.batch and not e["collected"]]
        if not still or not args.wait:
            for e in still:
                print(f"還沒完成 {e['id']}：{e['status']} {e.get('request_counts')}，已等 {(time.time() - e['created_ts']) / 60:.0f} 分鐘")
            break
        if time.time() - t_start > args.max_wait * 60:
            print(f"已等 {args.max_wait} 分鐘，先停下；之後重跑 collect 即可")
            break
        time.sleep(args.poll)
    # 接續：從 batch 收回、但還沒走完的篇。只碰 batch 的篇，避免和同時在跑的 `cards`（即時）搶同一篇
    from_batch = {rid for e in reg if e["batch"] == args.batch and e["collected"] for rid in e["rids"]}
    todo = [rid for rid in ready(r.manifest, args.batch)
            if rid in from_batch
            and r.manifest[rid].get("card", {}).get("state") not in ("passed", "escalate", "error")
            and (B.ROOT / "eval" / args.batch / rid / "kimi_write.json").exists()]
    if not todo:
        return
    chars = [r.manifest[rid]["chars_clean"] for rid in todo]
    if not preflight(chars, None, f"檢查與重寫 {len(todo)} 篇"):
        print("餘額不足：卡片已收回落檔，檢查與重寫先不做。儲值後重跑 collect 即可，不會重複花錢。")
        return
    run_with_offpeak(r, todo, args)
    followups(r, args)
    print(f"\n這次即時呼叫花費 ¥{r.spent:.2f}" + (f"；提前停下：{r.stop_reason}" if r.stop_reason else ""))
    cmd_report(args)


def cmd_notes(args):
    """先跑檢查員的獨立筆記。筆記只讀全文、不看卡片，所以等 Kimi batch 的時候就能做。

    對象：這個批次還在 Kimi batch 裡的篇，加上接下來會送出的前 --limit 篇（順序和 submit 相同）。
    只寫 eval/<batch>/<id>/notes.json 與 notes_meta.json，不動 manifest，可以和等待中的 collect 同時跑。
    """
    r = Runner(args.batch, args.budget)
    flying = [rid for e in load_reg() if not e["collected"] and e["batch"] == args.batch for rid in e["rids"]]
    todo = [rid for rid in flying + pending(r, args.batch)[:args.limit or 0]
            if not (B.ROOT / "eval" / args.batch / rid / "notes.json").exists()]
    chars = [r.manifest[rid]["chars_clean"] for rid in todo]
    est = B.estimate(chars, "kimi", "deepseek")[1] / 2 if chars else 0.0   # 筆記約佔檢查花費的一半
    print(f"批次 {args.batch}：要先跑筆記 {len(todo)} 篇（batch 中 {len(flying)} 篇），估計 ¥{est:.2f}")
    if not todo or args.dry_run:
        return
    if args.budget is None:
        raise SystemExit("真的送出時必須給 --budget")
    if offpeak.blocked(CHECKER):
        raise SystemExit(offpeak.message())
    bal = KB.deepseek_balance()
    if bal < est * SAFETY:
        raise SystemExit(f"DeepSeek 餘額 ¥{bal:.2f} 不足（要求 ≥ ¥{est * SAFETY:.2f}），沒有送出")

    def one(rid):
        rec, ev = {}, r.paths(rid)[1]
        try:
            out = r.notes(rid, r.fulltext(rid), ev, rec)
        except Stop as e:
            return rid, f"停下：{e}", 0.0
        except OffPeakWait:
            return rid, "進入尖峰時段，沒有跑", 0.0
        if out is None:
            return rid, "失敗", rec.get("cost_cny", 0.0)
        (ev / "notes_meta.json").write_text(json.dumps(
            {"calls": rec.get("calls", []), "cost_cny": rec.get("cost_cny", 0.0)}, ensure_ascii=False, indent=2))
        return rid, "完成", rec.get("cost_cny", 0.0)

    t0, total = time.time(), 0.0
    with ThreadPoolExecutor(args.workers) as ex:
        for rid, state, cost in ex.map(one, todo):
            total += cost
            print(f"  {rid}  {state}  ¥{cost:.3f}", flush=True)
    print(f"筆記花費 ¥{total:.2f}，耗時 {time.time() - t0:.0f} 秒")


def cmd_cards(args):
    r = Runner(args.batch, args.budget, args.dry_run)
    flying = {rid for e in load_reg() if not e["collected"] for rid in e["rids"]}   # 還在 batch 裡的篇不碰
    todo = [rid for rid in ready(r.manifest, args.batch)
            if r.manifest[rid].get("card", {}).get("state") not in ("passed", "escalate")
            and rid not in flying]
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
    unwritten = [r.manifest[rid]["chars_clean"] for rid in todo
                 if not (B.ROOT / "cards" / args.batch / f"{rid}.json").exists()]
    if not preflight(chars, "kimi", f"即時處理 {len(todo)} 篇", write_chars=unwritten):
        raise SystemExit("餘額不足，沒有送出")
    run_with_offpeak(r, todo, args)
    followups(r, args)
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
    print("監控：")
    for line in monitor.summary(m, args.batch):
        print(line)


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
    if offpeak.blocked(CHECKER):
        raise SystemExit(offpeak.message())   # 埋雷做了卻不能檢查，Kimi 的改寫就白花了
    r = Runner(args.batch, args.budget)
    canary_round(r, args.batch, args.every)
    print(f"這次花費 ¥{r.spent:.2f}")


def canary_round(r, batch, every):
    """在還沒埋過雷的通過卡上，每 every 張埋 1 張（依 id 雜湊固定抽樣），送檢查員。回傳這次做了幾個。"""
    d = B.ROOT / "eval" / batch / "_canary"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "_canaries.json"
    canaries = json.loads(path.read_text()) if path.exists() else []
    done = {c["rid"] for c in canaries}
    passed = sorted(rid for rid, v in r.manifest.items()
                    if v["batch"] == batch and v.get("card", {}).get("state") == "passed")
    # 依 id 的雜湊固定抽樣：每 every 張抽 1 張，重跑會抽到同一批
    picks = [rid for rid in passed if int(hashlib.sha1(rid.encode()).hexdigest(), 16) % every == 0 and rid not in done]
    print(f"埋雷：通過的卡 {len(passed)} 張，這次埋 {len(picks)} 張（每 {every} 張 1 張；已做過 {len(done)} 張）")
    if not picks:
        return 0
    # 邏輯類埋雷要 Kimi 改寫一小段（約 ¥0.4），每個埋雷要 DeepSeek 檢查一次（尖峰約 ¥0.15）
    if not preflight([], None, f"埋雷 {len(picks)} 張", kimi_extra=0.4 * len(picks) / 3, ds_extra=0.15 * len(picks)):
        print("餘額不足，埋雷沒有送出")
        return 0
    n = 0
    for rid in picks:
        rec = {}
        try:
            fulltext = r.fulltext(rid)
            card = json.loads((B.ROOT / "cards" / batch / f"{rid}.json").read_text())
            info = make_canary(r, rid, card, rec)
            if not info:
                print(f"{rid}: 做不出埋雷")
                continue
            if gate_v4(card, fulltext, r.ctx.card_schema):
                print(f"{rid}: 埋雷會被程式檢查擋下，不算（{info['type']}）")
                continue
            (d / f"{rid}.json").write_text(json.dumps(card, ensure_ascii=False, indent=2))
            notes = json.loads((B.ROOT / "eval" / batch / rid / "notes.json").read_text())
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
            n += 1
            print(f"{rid} [{info['type']}] {'抓到' if caught else '沒抓到'}（{res['verdict']}）: {info['desc'][:80]}")
        except Stop as e:
            print(f"停下：{e}")
            break
        except OffPeakWait:
            print(f"停下：{offpeak.message()}")
            break
    return n


def followups(r, args):
    """一輪寫卡／檢查之後：在新通過的卡上埋雷、抽樣審計，再看一次監控。"""
    if r.alarm or r.stop_reason:
        print("有警報或已提前停下：這次不做埋雷與審計")
        return
    if not getattr(args, "no_canary", False):
        if offpeak.blocked(CHECKER):
            print("埋雷要 DeepSeek 檢查，現在是尖峰：這次不做，下次跑同一個指令會補上")
        else:
            canary_round(r, args.batch, CANARY_EVERY)
            r.check_alarms()
    if not getattr(args, "no_audit", False) and not r.stop_reason:
        import audit
        left = None if r.budget is None else max(0.0, r.budget - r.spent)
        flagged = audit.stream_round(args.batch, AUDIT_PCT, left)
        if flagged:
            print(f"審計有 {flagged} 張和檢查員不一致：跑 `python audit.py review {args.batch}`，人工看複核表")


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
    import publish
    dest.write_text(publish.keep_judgments(dest, "\n".join(out)))
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
    for name in ["probe", "submit", "collect", "notes", "cards", "canary", "report", "review", "triage"]:
        p = sub.add_parser(name)
        p.add_argument("batch", nargs="?" if name in ("review", "triage") else None)
        if name in ("review", "triage"):
            p.add_argument("--trial", action="store_true", help="用試產 20 篇的資料")
        if name in ("probe", "submit", "collect", "notes", "cards", "canary"):
            p.add_argument("--budget", type=float, help="這次執行的花費上限（人民幣）")
        if name in ("cards", "collect", "notes"):
            p.add_argument("--workers", type=int, default=5)
        if name in ("cards", "collect"):
            p.add_argument("--ignore-alarms", action="store_true", help="看過監控警報、處理過之後繼續跑")
            p.add_argument("--no-canary", action="store_true", help="這次不自動埋雷")
            p.add_argument("--no-audit", action="store_true", help="這次不自動抽樣審計")
        if name in ("cards", "submit", "notes"):
            p.add_argument("--limit", type=int, help="只處理前 N 篇")
            p.add_argument("--dry-run", action="store_true", help="只列出會做什麼，不送出")
        if name == "probe":
            p.add_argument("--n", type=int, default=1)
        if name == "submit":
            p.add_argument("--chunk", type=int, default=20, help="每個 batch 最多幾篇")
            p.add_argument("--window", default="12h", help="completion_window，最短 12h")
            p.add_argument("--ignore-balance", action="store_true",
                           help="餘額預檢不過也送（只在使用者明說之後會儲值時才加）")
        if name == "collect":
            p.add_argument("--wait", action="store_true", help="等到所有 batch 完成")
            p.add_argument("--poll", type=int, default=120, help="每隔幾秒查一次")
            p.add_argument("--max-wait", type=int, default=180, help="最多等幾分鐘")
        if name == "canary":
            p.add_argument("--every", type=int, default=20)
        if name in ("collect", "notes", "cards", "canary"):
            p.add_argument("--allow-peak", action="store_true",
                           help="允許在 DeepSeek 尖峰時段呼叫（原價）。只在使用者明說可以時才加")
        if name in ("collect", "cards"):
            p.add_argument("--wait-offpeak", action="store_true", help="遇到尖峰就睡到離峰再接續 DeepSeek 的步驟")
    args = ap.parse_args()
    offpeak.allow_from_flag(getattr(args, "allow_peak", False))
    globals()[f"cmd_{args.cmd}"](args)


if __name__ == "__main__":
    main()
