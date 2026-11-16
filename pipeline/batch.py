"""量產的本機階段（不呼叫任何模型 API）：入庫 → 轉檔 → 品質閘 → 去固定頁 → 狀態與花費估算。

用法（<batch> 是 inbox/ 下的資料夾名稱，例如 batch1）：
  python batch.py ingest  <batch>                 # 階段 0：掃描 PDF、去重、寫進 manifest
  python batch.py convert <batch> [--tier standard] [--postprocess-only]   # 階段 1：MinerU（本機 GPU）
  python batch.py quality <batch>                 # 階段 1b：轉檔品質閘，不合格的標成 hold
  python batch.py clean   <batch>                 # 階段 2：去固定頁
  python batch.py status  <batch> [--budget 300]  # 各階段篇數、hold 原因、寫卡與檢查的花費估算

每個指令都可以重跑：已完成的篇會跳過，只處理還沒做的。

路徑：
  inbox/<batch>/*.pdf                       原始 PDF（不進 git）
  catalog/manifest.json                     每篇的狀態、來源檔、各階段結果（全庫共用，跨批次去重）
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
import time
import unicodedata
from pathlib import Path

from clean import strip_repeated, strip_sections
from common import KB

ROOT = Path(os.environ.get("QUANT_KB_ROOT", KB))
MANIFEST = ROOT / "catalog" / "manifest.json"
MINERU = "/home/newt/venvs/kb-parse/bin/mineru-kit"
IMG_RE = re.compile(r"!\[([^\]]*)\]\(data:image/([a-z]+);base64,([^)]*)\)")
GARBLED = re.compile(r"[�\u0000-\u0008\u000b\u000c\u000e-\u001f-]")
CJK = re.compile(r"[一-鿿]")
NAME_RE = re.compile(r"(\d{8})-([^-]+)-(.*)")

# 品質閘門檻。超過的篇標成 hold，不進寫卡；門檻偏寬，寧可放行後由檢查員處理。
MAX_GARBLED_PCT = 0.5        # PLAN_POC.md 的 V1 標準
MIN_CHARS = 2000             # 全文太短：轉檔失敗或只有封面
MIN_CHARS_PER_PAGE = 150     # 每頁字數太少：多半是掃描版且 OCR 沒出字
MIN_CJK_OR_LATIN_PCT = 30    # 中文加英文字母佔比太低：多半是表格或亂碼
MAX_CHARS = 120_000          # POC 最長約 4 萬字；更長的還沒測過模型上限，先擋下來人工確認

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


def cmd_ingest(args):
    m = load()
    by_hash = {v["sha1"]: k for k, v in m.items()}
    inbox = ROOT / "inbox" / args.batch
    pdfs = sorted(inbox.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"{inbox} 沒有 PDF")
    new = dup = 0
    for pdf in pdfs:
        sha = hashlib.sha1(pdf.read_bytes()).hexdigest()
        if sha in by_hash:
            # 內容相同的檔案只記一次；重複的來源檔名留在紀錄裡
            rec = m[by_hash[sha]]
            label = f"{args.batch}/{pdf.name}"
            if label != f"{rec['batch']}/{rec['source_pdf']}":
                dup += 1
                if label not in rec.get("duplicates", []):
                    rec.setdefault("duplicates", []).append(label)
            continue
        mt = NAME_RE.match(pdf.name)
        date, broker = (mt.group(1), mt.group(2)) if mt else ("nodate", "unknown")
        rid, n = f"{date}_{broker}_{sha[:6]}", 6
        while rid in m:  # 雜湊前綴撞號時加長
            n += 2
            rid = f"{date}_{broker}_{sha[:n]}"
        m[rid] = {"batch": args.batch, "source_pdf": pdf.name, "sha1": sha,
                  "bytes": pdf.stat().st_size, "pages": page_count(pdf),
                  "name_parsed": bool(mt), "status": "ingested",
                  "ingested_at": time.strftime("%Y-%m-%d %H:%M")}
        by_hash[sha] = rid
        new += 1
    save(m)
    b = of_batch(m, args.batch)
    print(f"{args.batch}: 新增 {new} 篇，重複 {dup} 篇，本批共 {len(b)} 篇")
    bad = [v["source_pdf"] for v in b.values() if not v["name_parsed"]]
    if bad:
        print(f"檔名不符合「日期-券商-標題.pdf」的 {len(bad)} 篇（id 會是 nodate_unknown_*）：")
        for x in bad[:10]:
            print("  ", x)


def postprocess(rid, raw_md, out_dir):
    """NFKC 正規化；base64 內嵌圖片另存成檔案。回傳 (字數, 圖片數)。"""
    text, n = raw_md.read_text(), 0
    img_dir = out_dir / "images" / rid

    def keep(mt):
        nonlocal n
        n += 1
        ext = "jpg" if mt.group(2) == "jpeg" else mt.group(2)
        img_dir.mkdir(parents=True, exist_ok=True)
        name = f"img_{n:02d}.{ext}"
        (img_dir / name).write_bytes(base64.b64decode(mt.group(3)))
        return f"![{mt.group(1)}](images/{rid}/{name})"

    text = unicodedata.normalize("NFKC", IMG_RE.sub(keep, text))
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
            (stage / v["source_pdf"]).symlink_to(inbox / v["source_pdf"])
        log = ROOT / "pipeline" / "logs" / f"mineru-{args.batch}-{args.tier}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        print(f"MinerU {args.tier}：{len(todo)} 篇（log: {log}）", flush=True)
        t0 = time.time()
        with open(log, "a") as f:
            code = subprocess.run([MINERU, "parse", str(stage), "-o", str(raw_dir), "--tier", args.tier, "-v"],
                                  stdout=f, stderr=subprocess.STDOUT).returncode
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
    held = ok = 0
    for rid, v in of_batch(m, args.batch).items():
        if v["status"] != "converted":
            continue
        text = (ROOT / "fulltext" / args.batch / v["tier"] / f"{rid}.md").read_text()
        stats, problems = quality_problems(text, v.get("pages"))
        v["quality"] = stats
        if problems:
            v.update(status="hold", hold_reason="；".join(problems))
            held += 1
            print(f"hold  {rid}: {v['hold_reason']}")
        else:
            v["status"] = "quality_ok"
            ok += 1
    save(m)
    print(f"品質閘：通過 {ok} 篇，hold {held} 篇")


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
    order = ["ingested", "converted", "quality_ok", "cleaned", "hold"]
    counts = {s: sum(v["status"] == s for v in b.values()) for s in order}
    print(f"批次 {args.batch}：共 {len(b)} 篇")
    for s in order:
        print(f"  {s:<11} {counts[s]}")
    for rid, v in b.items():
        if v["status"] == "hold":
            print(f"    hold {rid}: {v['hold_reason']}")
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
    for name in ["ingest", "convert", "quality", "clean", "status"]:
        p = sub.add_parser(name)
        p.add_argument("batch")
        if name == "convert":
            p.add_argument("--tier", default="standard", choices=["basic", "standard"])
            p.add_argument("--postprocess-only", action="store_true", help="不跑 MinerU，只整理已有的原始輸出")
        if name == "status":
            p.add_argument("--budget", type=float, help="這一批的預算上限（人民幣）")
    args = ap.parse_args()
    globals()[f"cmd_{args.cmd}"](args)


if __name__ == "__main__":
    main()
