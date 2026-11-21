"""把已經寫好的卡（例如試產時 Kimi 寫的）登記成正式批次，只跑後半段：筆記、檢查、需要時重寫、審計、發布。

用法：
  python adopt.py poc --cards lab/cards/trial/kimi --fulltext lab/fulltext/poc/clean-v2 --pdfs inbox/poc
接著：
  python run.py cards poc --budget N --wait-offpeak   # 卡片已存在，不會重寫第一次；從檢查員筆記開始
  python publish.py reviews poc && python publish.py publish poc

不送 API。卡片先過一次現在的程式檢查，不過的不登記（列出來，留給人看）。
"""
import argparse
import json
import shutil
import time

import batch as B
import trial as T
from common import KB
from run import catalog_lock
from validate import gate_v4


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("batch")
    ap.add_argument("--cards", required=True, help="卡片 JSON 資料夾（相對 repo）")
    ap.add_argument("--fulltext", required=True, help="卡片當初對照的全文資料夾（相對 repo）")
    ap.add_argument("--pdfs", help="原始 PDF 所在的資料夾（相對 repo）；只用來記檔名對照")
    args = ap.parse_args()
    ctx = T.ctx()
    src_cards, src_ft = KB / args.cards, KB / args.fulltext
    index = {}
    for p in (src_ft / "_index.json", src_ft.parent / "basic" / "_index.json"):
        if p.exists():
            index = json.loads(p.read_text())
            break
    cards_dir = B.ROOT / "cards" / args.batch
    ft_dir = B.ROOT / "fulltext" / args.batch / "clean"
    cards_dir.mkdir(parents=True, exist_ok=True)
    ft_dir.mkdir(parents=True, exist_ok=True)
    added, failed, skipped = [], [], []
    with catalog_lock():
        m = B.load()
        known = {v["sha1"] for v in m.values()}
        for p in sorted(src_cards.glob("*.json")):
            if ".meta" in p.name or ".attempt" in p.name:
                continue
            rid = p.stem
            if rid in m:
                skipped.append(rid)
                continue
            ftp = src_ft / f"{rid}.md"
            card, fulltext = json.loads(p.read_text()), ftp.read_text()
            problems = gate_v4(card, fulltext, ctx.card_schema)
            if problems:
                failed.append((rid, problems))
                continue
            pdf_name = index.get(rid, {}).get("source_pdf", "")
            pdf = (KB / args.pdfs / pdf_name) if args.pdfs and pdf_name else None
            sha = __import__("hashlib").sha1(pdf.read_bytes()).hexdigest() if pdf and pdf.exists() else None
            if sha and sha in known:
                skipped.append(rid)
                continue
            shutil.copy(ftp, ft_dir / f"{rid}.md")
            (cards_dir / f"{rid}.json").write_text(json.dumps(card, ensure_ascii=False, indent=2))
            m[rid] = {"batch": args.batch, "source_pdf": pdf_name, "sha1": sha, "status": "cleaned",
                      "chars_clean": len(fulltext), "ingested_at": time.strftime("%Y-%m-%d %H:%M"),
                      "adopted": {"cards": args.cards, "fulltext": args.fulltext},
                      "card": {"state": "written", "gate": [{"step": "adopted", "problems": 0}]}}
            added.append(rid)
        B.save(m)
    print(f"{args.batch}：登記 {len(added)} 張；程式檢查不過、沒登記 {len(failed)} 張；已在 manifest、略過 {len(skipped)} 張")
    for rid, pr in failed:
        print(f"  不過 {rid}：{pr[:2]}")


if __name__ == "__main__":
    main()
