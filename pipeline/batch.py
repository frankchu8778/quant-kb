"""量產的本機階段（不呼叫任何模型 API）：入庫 → 轉檔 → 品質閘 → 去固定頁 → 狀態與花費估算。

用法（<batch> 是 inbox/ 下的資料夾名稱，例如 batch1）：
  python batch.py sample  <batch> --n 100 [--copy]     # 從 .env 的 Research_Report_PATH 隨機選 PDF，只記原始路徑（不複製）；--copy 才複製到 inbox/<batch>/
  python batch.py ingest  <batch>                 # 階段 0：掃描 PDF、去重、寫進 manifest
  python batch.py convert <batch> [--tier standard] [--postprocess-only]   # 階段 1：MinerU（本機 GPU）
  python batch.py dedup   <batch>                 # 階段 1a：全文相似度比對，疑似重複的標成 hold
  python batch.py quality <batch>                 # 階段 1b：轉檔品質閘，不合格的標成 hold
  python batch.py clean   <batch>                 # 階段 2：去固定頁
  python batch.py status  <batch> [--budget 300]  # 各階段篇數、hold 原因、寫卡與檢查的花費估算
  python batch.py release <batch> <id> [<id> ...] # 人工確認後解除 hold，回到被擋下之前的階段（超長篇要加 --force）
  python batch.py holds   <batch>                 # 列出被擋下的篇，並把它們的 PDF 移到 hold/<batch>/

每個指令都可以重跑：已完成的篇會跳過，只處理還沒做的。

路徑：
  inbox/<batch>/*.pdf                       原始 PDF（不進 git）
  hold/<batch>/                             被擋下的篇：PDF 移到這裡，另有一份「待處理清單.md」寫明原因
  catalog/manifest.json                     每篇的狀態、來源檔、各階段結果（全庫共用，跨批次去重）
  catalog/fingerprints.json                 每篇的內容指紋（全庫共用，跨批次比對重複）
  pipeline/_mineru_raw/<batch>/<tier>/      MinerU 原始輸出（不修改）
  fulltext/<batch>/<tier>/<id>.md           整理後的全文
  fulltext/<batch>/clean/<id>.md            去固定頁後的全文（寫卡用這份）

測試時可用環境變數 QUANT_KB_ROOT 把根目錄指到別處，不動到正式資料。
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import zlib
from pathlib import Path

from clean import strip_repeated, strip_sections
from common import KB

ROOT = Path(os.environ.get("QUANT_KB_ROOT", KB))
MANIFEST = ROOT / "catalog" / "manifest.json"
MINERU = "/home/newt/venvs/kb-parse/bin/mineru-kit"
IMG_RE = re.compile(r"!\[([^\]]*)\]\(data:image/([a-z]+);base64,([^)]*)\)")
# MinerU 有時把圖放在 HTML 表格裡，寫成 <img src="data:image/...;base64,...">（sub1 另類交易策略系列之十一：
# 95 萬字裡 93 萬字是這種圖片編碼，2026-11-24 發現），也要抽成檔案
HTML_IMG_RE = re.compile(r'<img\s+[^>]*?src="data:image/([a-z]+);base64,([^"]*)"[^>]*>')
GARBLED = re.compile(r"[�\u0000-\u0008\u000b\u000c\u000e-\u001f-]")
CJK = re.compile(r"[一-鿿]")
NAME_RE = re.compile(r"(\d{8})-([^-]+)-(.*)")

# 品質閘門檻。超過的篇標成 hold，不進寫卡；門檻偏寬，寧可放行後由檢查員處理。
MAX_GARBLED_PCT = 0.5        # lab/docs/PLAN_POC.md 的 V1 標準
MIN_CHARS = 2000             # 全文太短：轉檔失敗或只有封面
MIN_CHARS_PER_PAGE = 150     # 每頁字數太少：多半是掃描版且 OCR 沒出字
MIN_CJK_OR_LATIN_PCT = 30    # 中文加英文字母佔比太低：多半是表格或亂碼
MAX_CHARS = 120_000          # POC 最長約 4 萬字；更長的還沒測過模型上限，先擋下來人工確認
# 超長篇：全文超過 MAX_CHARS 的篇，幾乎都是因子手冊、合集（一篇列上百個因子，一張卡摘不完）。
# 先不處理：要等拆章節寫卡（TODO 第 23 項）做好再放行，所以 release 預設不放行這類。
LONG_KIND = "超長篇"
LONG_NOTE = ("全文超過 12 萬字，通常是因子手冊或合集，一張卡摘不完。目前先不處理，"
             "等拆章節寫卡（TODO 第 23 項）做好再放行；硬要照一般流程寫一張卡才加 --force。")

# 重複性檢查。相似度 = 兩篇文字的 5 字片段集合的重疊比例（Jaccard），用 MinHash 估計，誤差約 ±3 個百分點。
DUP_FULL_THRESHOLD = 0.60    # 全文相似度達到就 hold（暫定，使用者指定）
DUP_HEAD_THRESHOLD = 0.90    # 入庫快篩：PDF 前兩頁文字相似度。門檻較高，因為同系列研報的封面頁本來就很像
HEAD_PAGES = 2
MIN_HEAD_CHARS = 200         # 前兩頁抽不到這麼多字（掃描版）就不做快篩，留給全文比對
SHINGLE = 5
N_HASH = 256
FINGERPRINTS = ROOT / "catalog" / "fingerprints.json"

# 花費估算（人民幣）。token 量依試產 20 篇實測推算，只用來設預算，不是帳單。
TOKENS_PER_CHAR = 0.45       # 全文每字約幾個 token（Kimi 與 DeepSeek 實測 0.41–0.46）
PROMPT_OVERHEAD_TOKENS = 4000  # 詞彙表與欄位說明
PRICE_CNY = {                # 每百萬 token（輸入, 輸出）
    "kimi": (13, 68), "kimi-batch": (7.8, 40.8),
    "deepseek": (2, 8), "deepseek-offpeak": (1, 4),
}
WRITE_OUT_TOKENS = 11_000    # 寫卡輸出（含推理）
CHECK_OUT_TOKENS = 21_000    # 檢查兩段輸出合計（含推理）
CHECK_CARD_TOKENS = 12_000   # 檢查第二段多帶的筆記與卡片


def load():
    return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}


def save(m):
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, ensure_ascii=False, indent=2))
    tmp.replace(MANIFEST)  # 原子替換，中斷時不會留下半個檔


def norm_text(text):
    """只留中文、英文字母、數字：浮水印位置、空白、標點、Markdown 語法的差異都不影響指紋。"""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", unicodedata.normalize("NFKC", text))
    return re.sub(r"[^0-9A-Za-z一-鿿]", "", text).lower()


def minhash(text):
    """回傳 N_HASH 個整數的指紋；文字太短回傳 None。"""
    import numpy as np
    s = norm_text(text)
    if len(s) < SHINGLE + 20:
        return None
    # 每個 5 字片段取一個穩定的 32 位元雜湊（不用內建 hash()，它每次執行結果不同）
    h = np.fromiter({zlib.crc32(s[i:i + SHINGLE].encode()) for i in range(len(s) - SHINGLE + 1)}, dtype=np.uint64)
    # 固定種子：所有文件用同一組雜湊函數，指紋才能互相比較
    seeds = np.random.default_rng(20261117).integers(0, 1 << 63, N_HASH, dtype=np.uint64)
    with np.errstate(over="ignore"):  # 64 位元乘法刻意讓它溢位（splitmix64 混合）
        z = h[None, :] + seeds[:, None]
        z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        sig = (z ^ (z >> np.uint64(31))).min(axis=1)
    return [int(x) for x in sig]


def similarity(sig_a, sig_b):
    return sum(x == y for x, y in zip(sig_a, sig_b)) / N_HASH


def load_fp():
    return json.loads(FINGERPRINTS.read_text()) if FINGERPRINTS.exists() else {}


def save_fp(fp):
    FINGERPRINTS.parent.mkdir(parents=True, exist_ok=True)
    tmp = FINGERPRINTS.with_suffix(".tmp")
    tmp.write_text(json.dumps(fp))
    tmp.replace(FINGERPRINTS)


def nearest(sig, fp, kind, exclude):
    """在指紋庫裡找最像的一篇，回傳 (相似度, id)。"""
    best = (0.0, None)
    for rid, v in fp.items():
        if rid != exclude and v.get(kind):
            sim = similarity(sig, v[kind])
            if sim > best[0]:
                best = (sim, rid)
    return best


def pdf_path(v, batch):
    """這篇的 PDF 在哪：用 --no-copy 選的記原始路徑（不搬檔），舊批次在 inbox/<batch>/。"""
    return Path(v["source_path"]) if v.get("source_path") else ROOT / "inbox" / batch / v["source_pdf"]


def selections():
    """所有批次的選檔紀錄 {batch: _selection.json 內容}。"""
    return {p.parent.name: json.loads(p.read_text()) for p in (ROOT / "inbox").glob("*/_selection.json")}


def hold(v, reason, kind=None):
    """擋下一篇並記住它原本在哪個階段，release 時回得去。kind 是類別（目前只有 LONG_KIND）。"""
    v.update(hold_from=v["status"], status="hold", hold_reason=reason)
    if kind:
        v["hold_kind"] = kind


def sync_holds(m, batch):
    """讓 hold/<batch>/ 和 manifest 一致：被擋下的 PDF 移進去、放行的移回 inbox，並重寫待處理清單。"""
    inbox, hdir = ROOT / "inbox" / batch, ROOT / "hold" / batch
    rows = []
    for rid, v in of_batch(m, batch).items():
        src, parked = inbox / v["source_pdf"], hdir / v["source_pdf"]
        if v.get("source_path"):   # 沒搬檔的篇：原始 PDF 留在原位，只列清單
            if v["status"] == "hold":
                hdir.mkdir(parents=True, exist_ok=True)
                md = ROOT / "fulltext" / batch / v.get("tier", "standard") / f"{rid}.md"
                rows.append((rid, v, md if md.exists() else None))
            continue
        if v["status"] == "hold":
            hdir.mkdir(parents=True, exist_ok=True)
            if src.exists() and not parked.exists():
                src.rename(parked)
            md = ROOT / "fulltext" / batch / v.get("tier", "standard") / f"{rid}.md"
            rows.append((rid, v, md if md.exists() else None))
        elif parked.exists() and not src.exists():
            parked.rename(src)   # 已放行：PDF 移回去
    if not hdir.exists():
        return 0
    lines = [f"# 批次 {batch}：被擋下、等你決定的研報", "",
             f"更新時間：{time.strftime('%Y-%m-%d %H:%M')}。共 {len(rows)} 篇。PDF 就在這個資料夾裡。", "",
             "- 要放行：`python batch.py release " + batch + " <id>`（PDF 會移回 inbox，接著往下處理）",
             "- 不處理：留著不動即可，不會進到花錢的階段", ""]
    rows.sort(key=lambda r: r[1].get("hold_kind") == LONG_KIND)   # 要決定的放前面，超長篇放後面
    long_started = False
    for rid, v, md in rows:
        if v.get("hold_kind") == LONG_KIND and not long_started:
            long_started = True
            lines += [f"# {LONG_KIND}（先不處理）", "", LONG_NOTE, ""]
        lines += [f"## {v['source_pdf']}", "", f"- id：`{rid}`",
                  *([f"- 類別：{v['hold_kind']}"] if v.get("hold_kind") else []), f"- 原因：{v['hold_reason']}"]
        if v.get("source_path"):
            lines.append(f"- PDF（原位，沒有搬）：`{v['source_path']}`")
        lines += [
                  f"- 在哪一步被擋：{v['hold_from']}；頁數 {v.get('pages')}；轉出的全文 {v.get('chars_raw', '（尚未轉檔）')} 字"]
        if md:
            lines.append(f"- 轉出的全文：`{md.relative_to(ROOT)}`")
        lines.append("")
    (hdir / "待處理清單.md").write_text("\n".join(lines))
    return len(rows)


def cmd_holds(args):
    m = load()
    n = sync_holds(m, args.batch)
    for rid, v in of_batch(m, args.batch).items():
        if v["status"] == "hold":
            print(f"hold {rid}  {v['source_pdf'][:50]}" + (f"  [{v['hold_kind']}]" if v.get("hold_kind") else "")
                  + f"\n     → {v['hold_reason']}")
    print(f"共 {n} 篇，PDF 與清單在 {ROOT / 'hold' / args.batch}")


def head_text(pdf):
    """直接從 PDF 抽前幾頁的文字（不經過 MinerU，很快）。抽不到就回傳空字串。"""
    try:
        import pypdfium2
        doc = pypdfium2.PdfDocument(str(pdf))
        try:
            out = []
            for i in range(min(HEAD_PAGES, len(doc))):
                tp = doc[i].get_textpage()
                out.append(tp.get_text_range())
            return "".join(out)
        finally:
            doc.close()
    except Exception:
        return ""


def of_batch(m, batch):
    return {k: v for k, v in m.items() if v["batch"] == batch}


def page_count(pdf):
    try:
        import pypdfium2
        doc = pypdfium2.PdfDocument(str(pdf))
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:
        return None


def source_dir(args):
    if args.src:
        return Path(args.src)
    from common import load_env
    env = {k.strip(): v.strip() for k, v in load_env().items()}  # .env 裡等號兩邊可能有空格
    if "Research_Report_PATH" not in env:
        raise SystemExit(".env 裡沒有 Research_Report_PATH，請用 --src 指定來源資料夾")
    return Path(env["Research_Report_PATH"])


def cmd_sample(args):
    """隨機選 PDF，記在 inbox/<batch>/_selection.json（預設不複製檔案）。已在 manifest 或其他 inbox 批次裡的（依檔案雜湊）不會被選到。"""
    import random
    import shutil
    src = source_dir(args)
    if not src.is_dir():
        raise SystemExit(f"來源資料夾不存在：{src}")
    sha = lambda p: hashlib.sha1(p.read_bytes()).hexdigest()
    known = {v["sha1"] for v in load().values()}
    known |= {sha(p) for p in (ROOT / "inbox").glob("*/*.pdf")}
    known |= {x["sha1"] for b, sel in selections().items() if b != args.batch for x in sel["picked"]}  # 別的批次選過、還沒入庫的
    pool = sorted(p for p in src.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
    random.Random(args.seed).shuffle(pool)
    dest = ROOT / "inbox" / args.batch
    dest.mkdir(parents=True, exist_ok=True)
    picked, skipped = [], 0
    for p in pool:
        if len(picked) >= args.n:
            break
        h = sha(p)
        if h in known:   # 已處理過，或這次已選到內容完全相同的另一份
            skipped += 1
            continue
        known.add(h)
        name = p.name if not any(x["file"] == p.name for x in picked) and not (dest / p.name).exists() \
            else f"{p.stem}__{h[:6]}{p.suffix}"
        if not args.no_copy:
            shutil.copy2(p, dest / name)
        picked.append({"file": name, "from": str(p.relative_to(src)), "sha1": h})
    (dest / "_selection.json").write_text(json.dumps(
        {"source": str(src), "seed": args.seed, "pool": len(pool), "no_copy": args.no_copy, "picked": picked},
        ensure_ascii=False, indent=2))
    print(f"來源 {src}：共 {len(pool)} 份 PDF；選了 {len(picked)} 份"
          + ("（只記路徑，沒有複製）" if args.no_copy else f"複製到 {dest}") + f"；略過已處理或完全相同的 {skipped} 份")


def cmd_ingest(args):
    m = load()
    by_hash = {v["sha1"]: k for k, v in m.items()}
    inbox = ROOT / "inbox" / args.batch
    sel = selections().get(args.batch, {})
    if sel.get("no_copy"):   # 沒搬檔：照選檔紀錄去原位置讀
        pdfs = [(Path(sel["source"]) / x["from"], x["file"]) for x in sel["picked"]]
    else:
        pdfs = [(p, p.name) for p in sorted(inbox.glob("*.pdf"))]
    if not pdfs:
        raise SystemExit(f"{inbox} 沒有 PDF")
    new = dup = 0
    fp, held = load_fp(), []
    for pdf, fname in pdfs:
        sha = hashlib.sha1(pdf.read_bytes()).hexdigest()
        if sha in by_hash:
            # 內容相同的檔案只記一次；重複的來源檔名留在紀錄裡
            rec = m[by_hash[sha]]
            label = f"{args.batch}/{fname}"
            if label != f"{rec['batch']}/{rec['source_pdf']}":
                dup += 1
                if label not in rec.get("duplicates", []):
                    rec.setdefault("duplicates", []).append(label)
            continue
        mt = NAME_RE.match(fname)
        date, broker = (mt.group(1), mt.group(2)) if mt else ("nodate", "unknown")
        rid, n = f"{date}_{broker}_{sha[:6]}", 6
        while rid in m:  # 雜湊前綴撞號時加長
            n += 2
            rid = f"{date}_{broker}_{sha[:n]}"
        m[rid] = {"batch": args.batch, "source_pdf": fname, "sha1": sha,
                  **({"source_path": str(pdf)} if sel.get("no_copy") else {}),
                  "bytes": pdf.stat().st_size, "pages": page_count(pdf),
                  "name_parsed": bool(mt), "status": "ingested",
                  "ingested_at": time.strftime("%Y-%m-%d %H:%M")}
        by_hash[sha] = rid
        new += 1
        # 快篩：檔案不同但前兩頁文字幾乎一樣（加浮水印、重新壓縮的同一篇），轉檔前就擋下
        ht = head_text(pdf)
        sig = minhash(ht) if len(norm_text(ht)) >= MIN_HEAD_CHARS else None
        if sig:
            sim, other = nearest(sig, fp, "head", rid)
            fp.setdefault(rid, {})["head"] = sig
            if sim >= DUP_HEAD_THRESHOLD:
                hold(m[rid], f"疑似重複：前 {HEAD_PAGES} 頁與 {other}（{m[other]['source_pdf']}）相似度 {sim:.0%}")
                held.append(rid)
    save(m)
    save_fp(fp)
    b = of_batch(m, args.batch)
    print(f"{args.batch}: 新增 {new} 篇，檔案完全相同的重複 {dup} 篇，本批共 {len(b)} 篇")
    report_holds(m, held)
    bad = [v["source_pdf"] for v in b.values() if not v["name_parsed"]]
    if bad:
        print(f"檔名不符合「日期-券商-標題.pdf」的 {len(bad)} 篇（id 會是 nodate_unknown_*）：")
        for x in bad[:10]:
            print("  ", x)


def postprocess(rid, raw_md, out_dir):
    """NFKC 正規化；base64 內嵌圖片另存成檔案。回傳 (字數, 圖片數)。"""
    text, n = raw_md.read_text(), 0
    img_dir = out_dir / "images" / rid

    def save_img(alt, kind, data):
        nonlocal n
        n += 1
        ext = "jpg" if kind == "jpeg" else kind
        img_dir.mkdir(parents=True, exist_ok=True)
        name = f"img_{n:02d}.{ext}"
        (img_dir / name).write_bytes(base64.b64decode(data))
        return f"![{alt}](images/{rid}/{name})"

    text = IMG_RE.sub(lambda mt: save_img(mt.group(1), mt.group(2), mt.group(3)), text)
    text = HTML_IMG_RE.sub(lambda mt: save_img("", mt.group(1), mt.group(2)), text)
    text = unicodedata.normalize("NFKC", text)
    (out_dir / f"{rid}.md").write_text(text)
    return len(text), n


def cmd_convert(args):
    m = load()
    b = of_batch(m, args.batch)
    raw_dir = ROOT / "pipeline" / "_mineru_raw" / args.batch / args.tier
    out_dir = ROOT / "fulltext" / args.batch / args.tier
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    inbox = ROOT / "inbox" / args.batch
    todo = [(rid, v) for rid, v in b.items()
            if v["status"] == "ingested" and not (raw_dir / f"{Path(v['source_pdf']).stem}.md").exists()]
    if todo and not args.postprocess_only:
        # 只把還沒轉的 PDF 用符號連結放進暫存資料夾，MinerU 就不會重做已完成的
        stage = ROOT / "pipeline" / "_mineru_raw" / args.batch / f"_todo_{int(time.time())}"
        stage.mkdir(parents=True)
        for _, v in todo:
            (stage / v["source_pdf"]).symlink_to(pdf_path(v, args.batch))
        env = dict(os.environ)
        if args.tier == "standard":  # standard 等級的 VLM 連本機 vLLM（serve_mineru_vlm.sh），不上傳資料
            import socket
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", 8001)) != 0:
                    raise SystemExit("MinerU 的 VLM 服務沒有啟動（port 8001）。先跑：bash serve_mineru_vlm.sh")
            env["MINERU_CONFIG"] = str(KB / "pipeline" / "mineru-standard.yaml")
        log = ROOT / "pipeline" / "logs" / f"mineru-{args.batch}-{args.tier}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        print(f"MinerU {args.tier}：{len(todo)} 篇（log: {log}）", flush=True)
        t0 = time.time()
        with open(log, "a") as f:
            code = subprocess.run([MINERU, "parse", str(stage), "-o", str(raw_dir), "--tier", args.tier, "-v"],
                                  stdout=f, stderr=subprocess.STDOUT, env=env).returncode
        print(f"MinerU 結束 exit={code}，{time.time() - t0:.0f} 秒")
    done = miss = 0
    for rid, v in b.items():
        if v["status"] != "ingested":
            continue
        raw_md = raw_dir / f"{Path(v['source_pdf']).stem}.md"
        if not raw_md.exists():
            miss += 1
            continue
        chars, imgs = postprocess(rid, raw_md, out_dir)
        v.update(status="converted", tier=args.tier, chars_raw=chars, images=imgs)
        done += 1
    save(m)
    print(f"整理完成 {done} 篇；MinerU 沒有輸出 {miss} 篇（維持 ingested，可重跑 convert）")


def report_holds(m, held):
    """把新擋下的篇醒目地印出來（同時寫到 stderr，排程或背景執行時也看得到）。"""
    if not held:
        return
    batch = m[held[0]]["batch"]
    sync_holds(m, batch)
    lines = [f"⚠️  有 {len(held)} 篇被擋下（hold），需要你確認："] + \
            [f"   {rid}  {m[rid]['source_pdf'][:50]}\n      → {m[rid]['hold_reason']}" for rid in held] + \
            [(f"   原始 PDF 留在原位（沒有搬）；清單與原因在 {ROOT / 'hold' / batch}/待處理清單.md。"
              if all(m[r].get("source_path") for r in held) else
              f"   PDF 已移到 {ROOT / 'hold' / batch}，原因寫在裡面的「待處理清單.md」。"),
             "   確認不是問題的，用 `python batch.py release <batch> <id>` 放行。"]
    print("\n".join(lines))
    print(f"HOLD {len(held)}", file=sys.stderr)


def cmd_dedup(args):
    m, fp, held, n = load(), load_fp(), [], 0
    for rid, v in of_batch(m, args.batch).items():
        if v["status"] != "converted":
            continue
        sig = minhash((ROOT / "fulltext" / args.batch / v["tier"] / f"{rid}.md").read_text())
        n += 1
        if sig is None:  # 幾乎沒有文字：交給品質閘處理
            v["status"] = "deduped"
            continue
        sim, other = nearest(sig, fp, "full", rid)
        fp.setdefault(rid, {})["full"] = sig  # 被擋下的也留指紋，第三份重複才比得到
        v["dedup"] = {"nearest": other, "similarity": round(sim, 3)}
        cleared = any(r["reason"].startswith("疑似重複") for r in v.get("released", []))  # 入庫快篩時已人工放行過
        if sim >= DUP_FULL_THRESHOLD and not cleared:
            hold(v, f"疑似重複：全文與 {other}（{m[other]['source_pdf']}）相似度 {sim:.0%}")
            held.append(rid)
        else:
            v["status"] = "deduped"
    save(m)
    save_fp(fp)
    print(f"重複性檢查 {n} 篇：通過 {n - len(held)} 篇，hold {len(held)} 篇（門檻 {DUP_FULL_THRESHOLD:.0%}）")
    report_holds(m, held)


def cmd_release(args):
    m = load()
    for rid in args.ids:
        v = m.get(rid)
        if not v or v["batch"] != args.batch or v["status"] != "hold":
            print(f"略過 {rid}：不在批次 {args.batch} 或不是 hold")
            continue
        if v.get("hold_kind") == LONG_KIND and not args.force:
            print(f"略過 {rid}：{LONG_KIND}，{LONG_NOTE}")
            continue
        # 放行後往下一個階段走，不再被同一道檢查擋下
        nxt = {"ingested": "ingested", "converted": "deduped", "deduped": "quality_ok"}[v["hold_from"]]
        v.setdefault("released", []).append({"reason": v.pop("hold_reason"), "at": time.strftime("%Y-%m-%d %H:%M")})
        v["status"] = nxt
        del v["hold_from"]
        v.pop("hold_kind", None)
        print(f"放行 {rid} → {nxt}")
    save(m)
    sync_holds(m, args.batch)


def quality_problems(text, pages):
    n = max(len(text), 1)
    garbled = 100 * len(GARBLED.findall(text)) / n
    wordy = 100 * (len(CJK.findall(text)) + len(re.findall(r"[A-Za-z]", text))) / n
    stats = {"garbled_pct": round(garbled, 3), "text_pct": round(wordy, 1),
             "chars_per_page": round(len(text) / pages) if pages else None}
    problems = []
    if garbled >= MAX_GARBLED_PCT:
        problems.append(f"亂碼 {garbled:.2f}%")
    if len(text) < MIN_CHARS:
        problems.append(f"全文只有 {len(text)} 字")
    elif pages and len(text) / pages < MIN_CHARS_PER_PAGE:
        problems.append(f"每頁只有 {len(text) / pages:.0f} 字（可能是掃描版）")
    if wordy < MIN_CJK_OR_LATIN_PCT:
        problems.append(f"文字佔比只有 {wordy:.0f}%")
    if len(text) > MAX_CHARS:
        problems.append(f"全文 {len(text)} 字，超過上限 {MAX_CHARS}")
    return stats, problems


def cmd_quality(args):
    m = load()
    held, ok = [], 0
    for rid, v in of_batch(m, args.batch).items():
        if v["status"] != "deduped":
            continue
        text = (ROOT / "fulltext" / args.batch / v["tier"] / f"{rid}.md").read_text()
        stats, problems = quality_problems(text, v.get("pages"))
        v["quality"] = stats
        if problems:
            hold(v, "轉檔品質：" + "；".join(problems), LONG_KIND if len(text) > MAX_CHARS else None)
            held.append(rid)
        else:
            v["status"] = "quality_ok"
            ok += 1
    save(m)
    print(f"品質閘：通過 {ok} 篇，hold {len(held)} 篇")
    report_holds(m, held)


def cmd_clean(args):
    m = load()
    out = ROOT / "fulltext" / args.batch / "clean"
    (out / "_removed").mkdir(parents=True, exist_ok=True)
    n = 0
    for rid, v in of_batch(m, args.batch).items():
        if v["status"] != "quality_ok":
            continue
        text = (ROOT / "fulltext" / args.batch / v["tier"] / f"{rid}.md").read_text()
        kept, rm_sec = strip_sections(text.splitlines())
        kept, rm_rep = strip_repeated(kept)
        cleaned = "\n".join(kept)
        (out / f"{rid}.md").write_text(cleaned)
        (out / "_removed" / f"{rid}.removed.md").write_text(
            "## 章節刪除\n\n" + "\n".join(rm_sec) + "\n\n## 重複行刪除\n\n" + "\n".join(sorted(set(rm_rep))))
        saved = round(100 * (1 - len(cleaned) / max(len(text), 1)), 1)
        v.update(status="cleaned", chars_clean=len(cleaned), clean_saved_pct=saved)
        n += 1
        if saved > 30:  # POC 平均 9%；刪掉太多要人工看 _removed
            print(f"注意 {rid}: 刪掉 {saved}%，請檢查 {out / '_removed' / (rid + '.removed.md')}")
    save(m)
    print(f"去固定頁 {n} 篇 → {out}")


def estimate(chars_list, writer, checker):
    """回傳 (寫卡花費, 檢查花費)，人民幣。"""
    wi, wo = PRICE_CNY[writer]
    ci, co = PRICE_CNY[checker]
    write = check = 0.0
    for c in chars_list:
        t = c * TOKENS_PER_CHAR + PROMPT_OVERHEAD_TOKENS
        write += (t * wi + WRITE_OUT_TOKENS * wo) / 1e6
        check += ((2 * t + CHECK_CARD_TOKENS) * ci + CHECK_OUT_TOKENS * co) / 1e6
    return write, check


def cmd_status(args):
    b = of_batch(load(), args.batch)
    if not b:
        raise SystemExit(f"manifest 裡沒有批次 {args.batch}，先跑 ingest")
    order = ["ingested", "converted", "deduped", "quality_ok", "cleaned", "hold"]
    counts = {s: sum(v["status"] == s for v in b.values()) for s in order}
    print(f"批次 {args.batch}：共 {len(b)} 篇")
    for s in order:
        print(f"  {s:<11} {counts[s]}")
    for rid, v in b.items():
        if v["status"] == "hold":
            print(f"    hold {rid}: " + (f"[{v['hold_kind']}] " if v.get("hold_kind") else "") + v["hold_reason"])
    ready = [v["chars_clean"] for v in b.values() if v["status"] == "cleaned"]
    if not ready:
        return
    print(f"\n可以寫卡的 {len(ready)} 篇：全文平均 {sum(ready) / len(ready):.0f} 字，最長 {max(ready)} 字")
    print("花費估算（人民幣；不含重寫與複檢，試產時約多一成）：")
    worst = 0.0
    for writer in ["kimi", "kimi-batch", "deepseek-offpeak"]:
        w, c = estimate(ready, writer, "deepseek-offpeak")
        worst = max(worst, w + c)
        print(f"  寫卡 {writer:<17} ¥{w:7.1f}  ＋ 檢查 deepseek-offpeak ¥{c:6.1f}  ＝ ¥{w + c:7.1f}")
    if args.budget is not None:
        verdict = "在預算內" if worst * 1.1 <= args.budget else "超過預算"
        print(f"預算 ¥{args.budget:.0f}：最貴的組合加一成是 ¥{worst * 1.1:.0f}，{verdict}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ["holds", "sample", "ingest", "convert", "dedup", "quality", "clean", "status", "release"]:
        p = sub.add_parser(name)
        p.add_argument("batch")
        if name == "convert":
            p.add_argument("--tier", default="standard", choices=["basic", "standard"])
            p.add_argument("--postprocess-only", action="store_true", help="不跑 MinerU，只整理已有的原始輸出")
        if name == "sample":
            p.add_argument("--n", type=int, default=100)
            p.add_argument("--seed", default="batch-sample-v1")
            p.add_argument("--src", help="來源資料夾；不給就用 .env 的 Research_Report_PATH")
            # 預設不複製 PDF：只記原始路徑，轉檔時直接讀原位置；--copy 才複製進 inbox/<batch>/（舊做法）
            p.add_argument("--copy", dest="no_copy", action="store_false", help="把 PDF 複製進 inbox/<batch>/")
            p.add_argument("--no-copy", dest="no_copy", action="store_true", help=argparse.SUPPRESS)
            p.set_defaults(no_copy=True)
        if name == "release":
            p.add_argument("ids", nargs="+")
            p.add_argument("--force", action="store_true", help="超長篇也放行（照一般流程寫一張卡）")
        if name == "status":
            p.add_argument("--budget", type=float, help="這一批的預算上限（人民幣）")
    args = ap.parse_args()
    globals()[f"cmd_{args.cmd}"](args)


if __name__ == "__main__":
    main()
