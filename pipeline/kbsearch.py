"""知識庫查詢工具：語意＋關鍵詞＋標籤混合排序。給 quant-kb skill 和使用者共用（2026-11-26）。

agent 不自己 grep 卡片，只把問題交給這支工具，拿到排好序的清單，再決定讀哪幾張的精簡卡或整卡。
token 只花在讀進來的內容上，排序在本機做，所以每題的成本不隨庫的篇數增加。

用法（`pipeline/bin/kb-search` 是包好 venv 的捷徑）：
  kb-search search "问题" [--freq intraday] [--type trend-following] [--asset a-share] [--family machine-learning]
                         [--since 2015] [--top 15] [--all] [--brief N] [--json]
  kb-search brief <id> [<id> ...]      精簡卡：每篇約 1,000 字（整卡約 7,000 字）
  kb-search card <id>                  整張卡
  kb-search grep <id> "正则" [--max 2]  只在這一篇的全文裡找段落，最多 2 段、每段 ≤600 字
  kb-search index [--rebuild]          更新向量索引（publish.py publish 跑完會自動呼叫）
  kb-search serve | stop | status      常駐查詢服務（search 會自動啟動它，只用 CPU，閒置 15 分鐘自動關閉）
id 可以只打最後 6 碼。問題用簡體或繁體都可以（工具會轉成簡體）。

排序：三份名次用 RRF（倒數名次加權）合併——
  語意：問題 vs 卡片的「標題＋摘要＋核心思路」向量（bge-m3），
  關鍵詞：字元雙字組 BM25，比對標題、摘要、核心思路、方法步驟、標籤與同義詞，
  標籤：問題裡出現某個標籤的中文、英文 id 或別名，就加分給有這個標籤的卡。
週報點評（report_type 周报点评）一律不列：幾乎沒有方法論，等使用者整理完再說。

只有 search 需要模型，走常駐服務；brief、card、grep 在本機直接讀檔，不用等服務啟動。
索引只存成 numpy 檔案（3,000 篇約 6 MB）；之後要換向量庫，只改 VectorStore 這個類別。
"""
import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
KB = Path(os.environ.get("KB_DIR", HERE.parent / "kb"))
INDEX = KB / "index"
MODEL = Path(os.environ.get("KB_EMBED_MODEL", Path.home() / "models" / "bge-m3"))
HOST, PORT = "127.0.0.1", int(os.environ.get("KB_SEARCH_PORT", "8765"))
IDLE = int(os.environ.get("KB_SEARCH_IDLE", "900"))      # 閒置幾秒後服務自動關閉
LOG = HERE / "logs" / "kbsearch.log"
CPU_THREADS = 4
EXCLUDE_TYPES = {"周报点评"}
RRF_K = 60
WEIGHTS = {"vec": 1.0, "kw": 1.0, "tag": 0.7}
GREP_CHARS = 600


# ---------- 讀卡 ----------

def sections(md):
    """卡片 markdown → {節名: 內文}。"""
    parts = re.split(r"^## (.+)$", md, flags=re.M)
    return {parts[i].strip(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def load_cards():
    """catalog 的每一行，加上卡片各節。順序照 catalog。"""
    import yaml
    cards = []
    for line in (KB / "catalog.jsonl").read_text().splitlines():
        c = json.loads(line)
        md = (KB / "cards" / f"{c['id']}.md").read_text()
        fm = yaml.safe_load(md.split("---", 2)[1])
        c["keywords"] = [str(k) for k in fm.get("keywords") or []]
        c["checked"] = fm.get("checked")
        c["sec"] = sections(md)
        cards.append(c)
    return cards


def steps(sec):
    """方法步驟的每一步（不含原文引文）。"""
    return [m.group(1).strip() for m in re.finditer(r"^\d+\.\s+(.+)$", sec.get("方法步骤", ""), re.M)]


def embed_text(c):
    return f"{c['title']}\n{c['sec'].get('摘要', '')}\n{c['sec'].get('核心思路', '')}"


def lexical_text(c):
    tags = c["strategy_types"] + c["families"] + c["methods"]
    return "\n".join([c["title"], c["sec"].get("摘要", ""), c["sec"].get("核心思路", ""), *steps(c["sec"]),
                      *tags, *c["keywords"], c.get("series") or ""])


def resolve(ids):
    """完整 id 或最後 6 碼 → 完整 id。"""
    all_ids = [json.loads(l)["id"] for l in (KB / "catalog.jsonl").read_text().splitlines()]
    out = []
    for x in ids:
        hit = [i for i in all_ids if i == x or i.endswith("_" + x) or i.endswith(x)]
        if len(hit) != 1:
            raise SystemExit(f"找不到唯一的卡：{x}（符合 {len(hit)} 張）")
        out.append(hit[0])
    return out


# ---------- 精簡卡 ----------

def clip(s, n):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def brief(c):
    """agent 主要讀的那一層：從卡片自動組，不另外花寫卡的點數。"""
    sec = c["sec"]
    others = [t.split("/")[1] for t in c["strategy_types"] if t != c["primary_strategy_type"]]
    head = [f"### [{c['id']}] {c['title']}",
            f"{c['broker']}｜{c['date'] or '无日期'}｜{c['report_type']}｜频率 {c['frequency']}｜"
            f"{c['primary_strategy_type'].split('/')[1]}" + (f"（另 {'、'.join(others)}）" if others else "")
            + f"｜{'、'.join(c['asset_class'])}",
            f"方法：{'、'.join(t.split('/')[1] for t in c['methods'])}",
            f"原文：`{c.get('source_pdf')}`" + (f"（{c['source_folder']}/）" if c.get("source_folder")
                                             else "（根目录）" if c.get("source_folder") == "" else "（子文件夹未记录）")]
    body = [f"摘要：{clip(sec.get('摘要', ''), 220)}", f"核心思路：{clip(sec.get('核心思路', ''), 160)}"]
    st = steps(sec)
    if st:
        body.append("步骤：" + "；".join(f"{n}) {clip(s, 30)}" for n, s in enumerate(st[:5], 1))
                    + (f"；…共 {len(st)} 步" if len(st) > 5 else ""))
    rows = [r for r in re.findall(r"^\|(.+)\|$", sec.get("关键结果", ""), re.M)
            if not re.match(r"\s*(指标|-+)\s*\|", r)]
    for r in rows[:2]:
        m, v, ctx = (x.strip() for x in (r.split("|") + ["", ""])[:3])
        body.append(f"结果：{clip(m, 24)} = {clip(v, 24)}（{clip(ctx, 45)}）")
    conds = re.findall(r"^- (.+)$", sec.get("市场条件", ""), re.M)
    if conds:
        body.append("市场条件：" + "；".join(clip(x, 30) for x in conds[:2]))
    lim = re.findall(r"^- (.+)$", sec.get("局限", ""), re.M)
    if lim:
        body.append(f"局限：{clip(lim[0], 50)}" + (f"（另 {len(lim) - 1} 条见整卡）" if len(lim) > 1 else ""))
    tail = []
    if c["known_issues"]:
        tail.append(f"⚠ 已知问题 {c['known_issues']} 条，用到相关部分前读整卡末尾「已知问题」")
    if c.get("checked") and c["checked"] != "pass":
        tail.append(f"核对：{c['checked']}")
    return "\n".join(head + body + tail)


# ---------- 索引 ----------

class VectorStore:
    """向量存成 numpy 檔案、暴力算 cosine。3,000 篇內夠用；換向量庫時只改這個類別。"""

    def __init__(self, path=INDEX):
        self.path = path

    def exists(self):
        return (self.path / "meta.json").exists()

    def mtime(self):
        p = self.path / "meta.json"
        return p.stat().st_mtime if p.exists() else 0

    def load(self):
        import numpy as np
        meta = json.loads((self.path / "meta.json").read_text())
        vec = np.load(self.path / "vectors.npy").astype("float32")
        return meta, vec

    def save(self, meta, vec):
        import numpy as np
        self.path.mkdir(parents=True, exist_ok=True)
        np.save(self.path / "vectors.npy", vec.astype("float16"))
        tmp = self.path / "meta.json.tmp"
        tmp.write_text(json.dumps(meta, ensure_ascii=False))
        tmp.replace(self.path / "meta.json")   # meta 最後寫：服務看它的時間決定要不要重載


def load_model(device):
    from sentence_transformers import SentenceTransformer
    import torch
    if device == "cpu":
        torch.set_num_threads(CPU_THREADS)
    m = SentenceTransformer(str(MODEL), device=device)
    if device == "cuda":
        m.half()
    return m


def cmd_index(args):
    """只重算新卡或內容變了的卡；刪掉已不在 catalog 的卡。"""
    import numpy as np
    cards = load_cards()
    store = VectorStore()
    old_meta, old_vec = store.load() if store.exists() and not args.rebuild else ({"ids": [], "hashes": {}}, None)
    if old_meta.get("model") not in (None, str(MODEL)):
        old_meta, old_vec = {"ids": [], "hashes": {}}, None
    pos = {i: n for n, i in enumerate(old_meta["ids"])}
    hashes = {c["id"]: hashlib.sha1(embed_text(c).encode()).hexdigest() for c in cards}
    todo = [c for c in cards if old_meta["hashes"].get(c["id"]) != hashes[c["id"]]]
    new = {}
    if todo:
        import torch
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        t = time.time()
        vec = load_model(dev).encode([embed_text(c) for c in todo], batch_size=32, normalize_embeddings=True)
        new = {c["id"]: v for c, v in zip(todo, vec)}
        print(f"算了 {len(todo)} 張卡的向量（{dev}，{time.time() - t:.1f} 秒）")
    rows = [new[c["id"]] if c["id"] in new else old_vec[pos[c["id"]]] for c in cards]
    removed = len(set(old_meta["ids"]) - set(hashes))
    if not todo and not removed and old_meta["ids"] == [c["id"] for c in cards]:
        print(f"索引已是最新（{len(cards)} 張）")
        return
    store.save({"model": str(MODEL), "ids": [c["id"] for c in cards], "hashes": hashes,
                "built_at": time.strftime("%Y-%m-%d %H:%M:%S")}, np.array(rows))
    print(f"索引更新：共 {len(cards)} 張（新算 {len(todo)}、移除 {removed}）→ {INDEX}")


# ---------- 排序 ----------

def tokens(text):
    """中文取字元雙字組，英數取整個詞（小寫）。"""
    text = text.lower()
    out = []
    for run in re.findall(r"[一-鿿]+|[a-z0-9][a-z0-9\-+.]*", text):
        if run[0].isascii():
            if len(run) >= 2:
                out.append(run)
        else:
            out += [run[i:i + 2] for i in range(len(run) - 1)] or [run]
    return out


class Searcher:
    def __init__(self, with_model=True):
        self.model = load_model("cpu") if with_model else None
        self.loaded = -1
        self.lock = threading.Lock()
        self.reload()

    def reload(self):
        store = VectorStore()
        if not store.exists():
            raise SystemExit("還沒有索引：先跑 kb-search index")
        if store.mtime() == self.loaded:
            return
        meta, vec = store.load()
        cards = load_cards()
        if [c["id"] for c in cards] != meta["ids"]:
            raise SystemExit("索引和 catalog 對不上：先跑 kb-search index")
        self.cards, self.vec, self.loaded = cards, vec, store.mtime()
        self.lex, self._df = [lexical_text(c).lower() for c in cards], {}
        # BM25
        self.docs = [Counter(tokens(lexical_text(c))) for c in cards]
        self.dl = [sum(d.values()) for d in self.docs]
        self.avgdl = sum(self.dl) / len(self.dl)
        df = Counter(t for d in self.docs for t in d)
        n = len(cards)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        # 標籤別名 → 卡片
        self.alias = {}
        for i, c in enumerate(cards):
            for t in c["strategy_types"] + c["families"] + c["methods"]:
                for a in t.split("/"):
                    self.alias.setdefault(a.lower(), set()).add(i)
            for k in c["keywords"]:
                self.alias.setdefault(k.lower(), set()).add(i)
        from opencc import OpenCC
        self.t2s = OpenCC("t2s")

    def bm25(self, q):
        k1, b = 1.5, 0.75
        qt = set(tokens(q))
        s = []
        for d, dl in zip(self.docs, self.dl):
            s.append(sum(self.idf[t] * d[t] * (k1 + 1) / (d[t] + k1 * (1 - b + b * dl / self.avgdl))
                         for t in qt if t in d))
        return s

    def tag_hits(self, q):
        """問題裡出現的標籤／別名。英文要整個詞對上、至少 3 個字母（避免 IF、IC 這類誤中）。"""
        ql = q.lower()
        hits = {}
        for a, idx in self.alias.items():
            if a.isascii():
                if len(a) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", ql):
                    hits[a] = idx
            elif len(a) >= 2 and a in ql:
                hits[a] = idx
        return hits

    def rare(self, seg):
        """太常見的片段（超過 15% 的卡都有，例如「交易」「市场」）不列為命中。"""
        if seg not in self._df:
            self._df[seg] = sum(seg in t for t in self.lex) / len(self.lex)
        return self._df[seg] <= 0.15

    def matched_terms(self, q, c):
        """問題裡在這張卡出現過的最長片段，給清單的「命中」欄看。"""
        text = lexical_text(c).lower()
        ql, out, i = re.sub(r"[^\u4e00-\u9fffa-z0-9\-+]+", " ", q.lower()), [], 0
        while i < len(ql):
            j = i + 1
            while j <= len(ql) and ql[i:j] in text:
                j += 1
            seg = ql[i:j - 1]
            if len(seg) >= 2 and " " not in seg:
                if self.rare(seg):
                    out.append(seg)
                i = j - 1
            else:
                i += 1
        return sorted(set(out), key=lambda x: (self._df[x], -len(x)))[:4]

    def search(self, q, freq=None, type_=None, asset=None, family=None, since=None, top=15, all_=False):
        with self.lock:
            self.reload()
            q = self.t2s.convert(q)
            keep = []
            for i, c in enumerate(self.cards):
                if c["report_type"] in EXCLUDE_TYPES:
                    continue
                if freq and c["frequency"] not in freq:
                    continue
                if type_ and not any(t.split("/")[0] in type_ for t in c["strategy_types"]):
                    continue
                if asset and not set(c["asset_class"]) & set(asset):
                    continue
                if family and not any(t.split("/")[0] in family for t in c["families"] + c["methods"]):
                    continue
                if since and (not c["date"] or str(c["date"]) < since):
                    continue
                keep.append(i)
            if not keep:
                return {"query": q, "total": 0, "results": []}
            qv = self.model.encode([q], normalize_embeddings=True)[0]
            vs = self.vec @ qv
            ks = self.bm25(q)
            th = self.tag_hits(q)
            tcount = Counter(i for idx in th.values() for i in idx)
            rank = lambda score, pool: {i: r for r, i in enumerate(sorted(pool, key=lambda i: -score(i)), 1)}
            ranks = {"vec": rank(lambda i: vs[i], keep),
                     "kw": rank(lambda i: ks[i], [i for i in keep if ks[i] > 0]),
                     "tag": rank(lambda i: tcount[i], [i for i in keep if tcount[i]])}
            fused = {i: sum(WEIGHTS[k] / (RRF_K + r[i]) for k, r in ranks.items() if i in r) for i in keep}
            order = sorted(keep, key=lambda i: -fused[i])
            res = []
            for n, i in enumerate(order if all_ else order[:top], 1):
                c = self.cards[i]
                res.append({"rank": n, "id": c["id"], "title": c["title"], "broker": c["broker"], "date": c["date"],
                            "frequency": c["frequency"], "type": c["primary_strategy_type"].split("/")[1],
                            "gist": clip(re.split(r"(?<=[。；])", c["sec"].get("摘要", ""))[0], 60),
                            "hits": self.matched_terms(q, c), "known_issues": c["known_issues"],
                            "via": "".join(k[0] for k in ("vec", "kw", "tag") if i in ranks[k] and ranks[k][i] <= 20),
                            "score": round(fused[i] * 1000, 1)})
            return {"query": q, "total": len(keep), "results": res,
                    "tags": sorted(th, key=len, reverse=True)[:8]}


# ---------- 常駐服務 ----------

def cmd_serve(args):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    s = Searcher()
    last = [time.time()]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            last[0] = time.time()
            if self.path == "/health":
                self.reply(200, {"ok": True, "cards": len(s.cards), "pid": os.getpid(), "idle_after": IDLE})
            elif self.path == "/stop":
                self.reply(200, {"ok": True})
                threading.Thread(target=srv.shutdown).start()
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self):
            last[0] = time.time()
            if self.path != "/search":
                return self.reply(404, {"error": "not found"})
            try:
                p = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.reply(200, s.search(**p))
            except SystemExit as e:
                self.reply(409, {"error": str(e)})
            except Exception as e:   # 不讓一次壞查詢把服務弄掛
                self.reply(500, {"error": repr(e)})

    srv = ThreadingHTTPServer((HOST, PORT), H)

    def watchdog():
        while True:
            time.sleep(30)
            if time.time() - last[0] > IDLE:
                srv.shutdown()
                return
    threading.Thread(target=watchdog, daemon=True).start()
    print(f"kb-search 服務 {HOST}:{PORT}（pid {os.getpid()}，閒置 {IDLE} 秒自動關閉）", flush=True)
    srv.serve_forever()


def http(method, path, payload=None, timeout=30):
    req = urllib.request.Request(f"http://{HOST}:{PORT}{path}", method=method,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ensure_server():
    try:
        return http("GET", "/health", timeout=2)
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        pass
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a") as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve"], stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    print("（查詢服務沒開，正在啟動，第一次約 15 秒…）", file=sys.stderr)
    for _ in range(240):
        time.sleep(0.5)
        try:
            return http("GET", "/health", timeout=2)
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
    raise SystemExit(f"查詢服務啟動失敗，看 {LOG}")


# ---------- 命令 ----------

def split_opt(xs):
    return [y for x in xs or [] for y in x.split(",")] or None


def cmd_search(args):
    ensure_server()
    p = {"q": args.query, "freq": split_opt(args.freq), "type_": split_opt(args.type), "asset": split_opt(args.asset),
         "family": split_opt(args.family), "since": args.since, "top": args.top, "all_": args.all}
    try:
        r = http("POST", "/search", p, timeout=60)
    except urllib.error.HTTPError as e:
        raise SystemExit(json.loads(e.read()).get("error"))
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return
    filt = "、".join(f"{k}={','.join(v) if isinstance(v, list) else v}" for k, v in p.items()
                    if k not in ("q", "top", "all_") and v)
    print(f"查询：{r['query']}" + (f"｜筛选 {filt}" if filt else "") + f"｜符合 {r['total']} 张，列 {len(r['results'])} 张"
          + (f"｜问题里的标签词：{'、'.join(r['tags'])}" if r.get("tags") else ""))
    print("（via：v 语意、k 关键词、t 标签，表示哪几种方法把它排进前 20）")
    for x in r["results"]:
        warn = f"｜⚠已知问题{x['known_issues']}" if x["known_issues"] else ""
        print(f"{x['rank']}. [{x['id']}] {x['title']}（{x['broker']} {str(x['date'] or '无日期')[:7]}｜{x['frequency']}｜{x['type']}{warn}）"
              f"\n   {x['gist']}｜命中：{'、'.join(x['hits']) or '—'}｜via {x['via'] or '—'}")
    if args.brief:
        ids = [x["id"] for x in r["results"][:args.brief]]
        print("\n" + "\n\n".join(brief(c) for c in load_cards() if c["id"] in ids))


def cmd_brief(args):
    ids = resolve(args.ids)
    by = {c["id"]: c for c in load_cards()}
    print("\n\n".join(brief(by[i]) for i in ids))


def cmd_card(args):
    print((KB / "cards" / f"{resolve([args.id])[0]}.md").read_text())


def cmd_grep(args):
    """在單篇全文裡找段落：以命中處為中心取 ≤600 字，附上最近的標題。"""
    rid = resolve([args.id])[0]
    text = (KB / "fulltext" / f"{rid}.md").read_text()
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    rx = re.compile(args.pattern, re.I)
    spans, last_end = [], -1
    for m in rx.finditer(text):
        if m.start() < last_end:
            continue
        a = max(0, m.start() - GREP_CHARS // 3)
        b = min(len(text), a + GREP_CHARS)
        head = [h for h in re.finditer(r"^#+ .+$", text[:m.start()], re.M)]
        spans.append((head[-1].group(0) if head else "（开头）", text[a:b]))
        last_end = b
    total = len(spans)
    if not total:
        print(f"[{rid}] 全文没有符合 /{args.pattern}/ 的段落")
        return
    print(f"[{rid}] 全文符合 {total} 处，列前 {min(total, args.max)} 处（每处 ≤{GREP_CHARS} 字）")
    for h, s in spans[:args.max]:
        print(f"\n--- 所在章节：{h.lstrip('#').strip()}\n{re.sub(r'<[^>]+>', ' ', s).strip()}")


def cmd_status(args):
    try:
        print(json.dumps(http("GET", "/health", timeout=2), ensure_ascii=False))
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        print("查詢服務沒在跑（下次 search 會自動啟動）")
    st = VectorStore()
    print(f"索引：{json.loads((INDEX / 'meta.json').read_text())['built_at'] if st.exists() else '還沒建'}（{INDEX}）")


def cmd_stop(args):
    try:
        http("GET", "/stop", timeout=5)
        print("已關閉查詢服務")
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        print("查詢服務本來就沒在跑")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("search")
    sp.add_argument("query")
    sp.add_argument("--freq", action="append", help="high-frequency,intraday,daily,weekly-or-longer,unspecified")
    sp.add_argument("--type", action="append", help="策略大類 id，例如 trend-following")
    sp.add_argument("--asset", action="append", help="a-share、stock-index-futures、commodity-futures…")
    sp.add_argument("--family", action="append", help="方法家族或方法 id，例如 machine-learning")
    sp.add_argument("--since", help="只列這個日期之後的（例如 2018 或 2018-06）；沒日期的卡會被排除")
    sp.add_argument("--top", type=int, default=15)
    sp.add_argument("--all", action="store_true", help="列出所有符合篩選的卡（列表型問題用）")
    sp.add_argument("--brief", type=int, default=0, help="順便印出前 N 張的精簡卡")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("brief")
    sp.add_argument("ids", nargs="+")
    sp = sub.add_parser("card")
    sp.add_argument("id")
    sp = sub.add_parser("grep")
    sp.add_argument("id")
    sp.add_argument("pattern")
    sp.add_argument("--max", type=int, default=2)
    sp = sub.add_parser("index")
    sp.add_argument("--rebuild", action="store_true")
    for name in ("serve", "stop", "status"):
        sub.add_parser(name)
    args = ap.parse_args()
    globals()[f"cmd_{args.cmd}"](args)


if __name__ == "__main__":
    main()
