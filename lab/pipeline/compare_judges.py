"""比較不同審稿模型（eval/<judge>/）的結果：埋雷偵測、排名、評分、問題數、成本。

用法：python compare_judges.py fable opus-high deepseek-flash-high
"""
import json
import sys
from collections import Counter

from common import KB

# 每個埋雷：命中條件 = 問題落在被改的那張卡片上，欄位相符，且內容提到關鍵字之一
CANARY_RULES = {
    "canary-number": ("key_results", ["0.93", "0.99"]),
    "canary-tag": ("methods", ["dividend", "分红", "分紅"]),
    "canary-core-idea": ("core_idea", ["反向", "回归", "回歸", "噪声高", "噪聲高", "顺势", "順勢", "趋势", "趨勢"]),
}


def load(judge, rid, name):
    p = KB / "eval" / judge / rid / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def main():
    judges = sys.argv[1:]
    canaries = json.loads((KB / "eval" / "fable" / "_canaries.json").read_text())
    rids = sorted({p.parent.name for j in judges for p in (KB / "eval" / j).glob("*/pass2.json")})

    print("## 埋雷偵測")
    for cn in canaries:
        field, kws = CANARY_RULES[cn["id"]]
        for j in judges:
            p2 = load(j, cn["rid"], "pass2")
            if not p2:
                print(f"  {cn['id']:<18} {j:<22} （未跑）")
                continue
            mp = json.loads((KB / "eval" / j / "_mapping.json").read_text())[cn["rid"]]
            label = next(k for k, v in mp.items() if v == cn["engine"])
            hits = [i for i in p2["cards"][label]["issues"]
                    if field in i["field"] and any(k in i["card_says"] + i["source_says"] for k in kws)]
            mark = f"✅ {hits[0]['severity']}" if hits else "❌"
            print(f"  {cn['id']:<18} {j:<22} {mark}  {hits[0]['source_says'][:80] if hits else ''}")

    print("\n## 每篇：排名、評分（忠實/核心/方法/結果/資料/標籤）、問題數 c/M/m、借用者測試")
    for rid in rids:
        print(f"\n### {rid}")
        for j in judges:
            p2 = load(j, rid, "pass2")
            if not p2:
                continue
            mp = json.loads((KB / "eval" / j / "_mapping.json").read_text())[rid]
            rank = " > ".join(mp[x].split("-")[0] for x in p2["ranking"]["order"])
            print(f"  {j:<22} 排名 {rank}")
            for label in "ABC":
                c = p2["cards"][label]
                sc = "".join(str(v) for v in c["scores"].values())
                sev = Counter(i["severity"] for i in c["issues"])
                print(f"    {mp[label]:<15} {sc}  c{sev['critical']}/M{sev['major']}/m{sev['minor']}  "
                      f"{c['borrower_test']['verdict']}")

    print("\n## 成本（API 等值 USD）與耗時")
    for j in judges:
        cost = secs = n = 0
        for rid in rids:
            for name in ["pass1", "pass2"]:
                p = KB / "eval" / j / rid / f"{name}.meta.json"
                if p.exists():
                    m = json.loads(p.read_text())
                    cost += m.get("total_cost_usd_api_equiv") or 0
                    secs += m.get("seconds") or 0
                    n += name == "pass2"
        if n:
            print(f"  {j:<22} {n} 篇  每篇 ${cost / n:.3f}  每篇 {secs / n:.0f} 秒")


if __name__ == "__main__":
    main()
