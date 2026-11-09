"""去除研報固定內容（V2）：免責聲明、評級說明、目錄、重複頁首頁尾。

用法：python clean.py --src fulltext/poc/pipeline --out fulltext/poc/clean

規則刻意保守（寧可少刪）：
1. 標題命中「免责声明 / 评级说明 / 分析师声明 / 目录 …」的章節，刪到下一個同級或更高級標題
2. 同一篇中重複出現 ≥ 4 次的短行（< 60 字，非表格、非公式），視為頁首頁尾
「风险提示」屬正文，不刪。

被刪的內容寫到 <out>/_removed/<id>.removed.md 供人工檢查；統計寫到 <out>/_clean_stats.json。
"""
import argparse
import json
import re
from collections import Counter

from common import KB

BOILERPLATE_HEADINGS = re.compile(
    r"(免责声明|免責聲明|重要声明|法律声明|特别声明|一般声明|分析师声明|分析师承诺|研究团队简介|"
    r"分析师简介|作者简介|评级说明|投资评级|行业评级|公司评级|信息披露|联系我们|联系方式|"
    r"本公司具备证券投资咨询|^目录$|^图表目录$|^表格目录$|插图目录|表格目录|图表目录)")
HEADING = re.compile(r"^(#{1,6})\s+(.*)")


def strip_sections(lines):
    kept, removed, skip_level = [], [], None
    for line in lines:
        m = HEADING.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if skip_level is None and BOILERPLATE_HEADINGS.search(title):
                skip_level = level
        (removed if skip_level is not None else kept).append(line)
    return kept, removed


def strip_repeated(lines, min_count=4, max_len=60):
    def candidate(s):
        s = s.strip()
        return s and len(s) < max_len and not s.startswith(("|", "<", "$", "#", "!")) and "$" not in s
    counts = Counter(l.strip() for l in lines if candidate(l))
    repeated = {s for s, c in counts.items() if c >= min_count}
    kept = [l for l in lines if l.strip() not in repeated]
    removed = [l for l in lines if l.strip() in repeated]
    return kept, removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    src, out = KB / args.src, KB / args.out
    (out / "_removed").mkdir(parents=True, exist_ok=True)
    stats = {}
    for md in sorted(src.glob("*.md")):
        if (out / md.name).exists():
            continue
        text = md.read_text()
        lines = text.splitlines()
        kept, rm_sec = strip_sections(lines)
        kept, rm_rep = strip_repeated(kept)
        cleaned = "\n".join(kept)
        (out / md.name).write_text(cleaned)
        (out / "_removed" / f"{md.stem}.removed.md").write_text(
            "## 章節刪除\n\n" + "\n".join(rm_sec) + "\n\n## 重複行刪除\n\n" + "\n".join(sorted(set(rm_rep))))
        stats[md.stem] = {"chars_before": len(text), "chars_after": len(cleaned),
                          "saved_pct": round(100 * (1 - len(cleaned) / max(len(text), 1)), 1),
                          "section_lines_removed": len(rm_sec), "repeated_lines_removed": len(rm_rep)}
        print(f"{stats[md.stem]['saved_pct']:>5}%  {md.stem}")
    prev = out / "_clean_stats.json"
    if prev.exists():
        stats = {**json.loads(prev.read_text()), **stats}
    prev.write_text(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
