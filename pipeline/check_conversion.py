"""V1 轉檔品質的自動檢查：亂碼比例、公式 / 表格 / 圖片數量、長度。

用法：python check_conversion.py fulltext/poc/pipeline fulltext/poc/vlm
輸出每個後端的逐篇統計與彙總，寫到 pipeline/results/v1_auto.json
"""
import json
import re
import sys

from common import KB, PIPELINE

GARBLED = re.compile(r"[�-\u0000-\u0008\u000b\u000c\u000e-\u001f]")
CJK = re.compile(r"[一-鿿]")


def stats(text):
    n = max(len(text), 1)
    return {
        "chars": len(text),
        "cjk_chars": len(CJK.findall(text)),
        "garbled_pct": round(100 * len(GARBLED.findall(text)) / n, 3),
        "display_formulas": len(re.findall(r"\$\$.+?\$\$", text, flags=re.S)),
        "inline_formulas": len(re.findall(r"(?<!\$)\$[^$\n]+\$(?!\$)", text)),
        "html_tables": text.count("<table"),
        "md_table_rows": len(re.findall(r"^\|.*\|$", text, flags=re.M)),
        "images": len(re.findall(r"!\[[^\]]*\]\(", text)),
        "headings": len(re.findall(r"^#{1,6}\s", text, flags=re.M)),
    }


def main():
    report = {}
    for d in sys.argv[1:]:
        rows = {md.stem: stats(md.read_text()) for md in sorted((KB / d).glob("*.md"))}
        agg = {k: round(sum(r[k] for r in rows.values()) / max(len(rows), 1), 3) for k in next(iter(rows.values()), {})}
        agg["files"] = len(rows)
        agg["files_garbled_over_0.5pct"] = [k for k, r in rows.items() if r["garbled_pct"] >= 0.5]
        report[d] = {"summary_mean": agg, "per_file": rows}
        print(d, json.dumps(agg, ensure_ascii=False))
    (PIPELINE / "results").mkdir(exist_ok=True)
    (PIPELINE / "results" / "v1_auto.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
