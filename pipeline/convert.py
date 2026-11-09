"""PDF → Markdown（MinerU 4），整理成以研報 id 命名的全文檔。

用法：
  python convert.py --tier basic    # MinerU 解析 + 整理
  python convert.py --tier standard
  python convert.py --tier basic --postprocess-only   # 只重做整理，不重新解析

流程：
1. mineru-kit parse 原始輸出 → pipeline/_mineru_raw/<tier>/（完整保留，不修改）
2. 整理 → fulltext/poc/<tier>/<id>.md
   - 檔名改成穩定 id（日期_券商_雜湊）
   - NFKC 正規化：數學花體字 𝑹𝟐 → R2、全形英數 → 半形（grep 才搜得到）
   - base64 內嵌圖片另存成檔案 fulltext/poc/<tier>/images/<id>/img_NN.<ext>，Markdown 裡換成相對連結
3. 對照表 → fulltext/poc/<tier>/_index.json（id ↔ 原始檔名、頁數耗時等）
"""
import argparse
import base64
import json
import re
import subprocess
import time
import unicodedata

from common import FULLTEXT, INBOX, PIPELINE, poc_pdfs, report_id

MINERU = "/home/newt/venvs/kb-parse/bin/mineru-kit"
IMG_RE = re.compile(r"!\[([^\]]*)\]\(data:image/([a-z]+);base64,([^)]*)\)")


def parse(tier, raw_dir, log):
    raw_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with open(log, "a") as f:
        p = subprocess.run([MINERU, "parse", str(INBOX), "-o", str(raw_dir), "--tier", tier, "-v"],
                           stdout=f, stderr=subprocess.STDOUT)
    return p.returncode, round(time.time() - t0, 1)


def postprocess(tier, raw_dir):
    out = FULLTEXT / "poc" / tier
    out.mkdir(parents=True, exist_ok=True)
    index = {}
    for pdf in poc_pdfs():
        rid = report_id(pdf)
        raw_md = raw_dir / f"{pdf.stem}.md"
        if not raw_md.exists():
            index[rid] = {"source_pdf": pdf.name, "error": "MinerU 沒有輸出"}
            continue
        text = raw_md.read_text()
        img_dir = out / "images" / rid
        n = 0

        def save(m):
            nonlocal n
            n += 1
            ext = "jpg" if m.group(2) == "jpeg" else m.group(2)
            img_dir.mkdir(parents=True, exist_ok=True)
            name = f"img_{n:02d}.{ext}"
            (img_dir / name).write_bytes(base64.b64decode(m.group(3)))
            return f"![{m.group(1)}](images/{rid}/{name})"

        text = unicodedata.normalize("NFKC", IMG_RE.sub(save, text))
        (out / f"{rid}.md").write_text(text)
        index[rid] = {"source_pdf": pdf.name, "chars": len(text), "images": n}
    (out / "_index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2))
    return index


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", required=True, choices=["basic", "standard", "advanced", "flash"])
    ap.add_argument("--postprocess-only", action="store_true")
    args = ap.parse_args()
    raw_dir = PIPELINE / "_mineru_raw" / args.tier
    meta = {}
    if not args.postprocess_only:
        code, secs = parse(args.tier, raw_dir, PIPELINE / "logs" / f"mineru-{args.tier}.log")
        meta = {"exit_code": code, "seconds": secs}
        print(f"MinerU {args.tier}: exit={code}, {secs}s")
    index = postprocess(args.tier, raw_dir)
    ok = sum(1 for v in index.values() if "error" not in v)
    print(f"整理完成 {ok}/{len(index)} 篇 → fulltext/poc/{args.tier}/")
    if meta:
        (FULLTEXT / "poc" / args.tier / "_run.json").write_text(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
