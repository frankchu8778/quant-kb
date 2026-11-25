"""驗收 kb-search（2026-11-26）：用 vector_search.py 的 10 題，比較新舊流程的召回與讀進的字數。

舊流程（現在的 skill）：agent 用關鍵詞正則 grep 卡片，候選卡整張讀。
新流程：kb-search 排序清單（前 15 名，每行約 100 字）＋前 8 名精簡卡；列表型問題用欄位篩選＋--all 只讀清單。
「讀進的字數」只算知識庫內容，不算 agent 自己的思考與回答。

用法：~/venvs/kb-parse/bin/python experiments/kbsearch_eval.py   結果寫到 results/kbsearch_eval.md
注意：標準答案與舊流程的關鍵詞都是 Claude 定的，舊流程的召回偏樂觀（同 vector_search.py）。
"""
import io
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import kbsearch as K                      # noqa: E402
from vector_search import QUERIES         # noqa: E402

OUT = HERE.parent / "results" / "kbsearch_eval.md"
TOP, BRIEF = 15, 8
FILTERS = {0: {"freq": ["intraday", "high-frequency"], "all_": True}}   # 第 1 題是列表型


def list_text(r):
    """照命令列印出來的清單，算字數用。"""
    args = type("A", (), {"json": False, "brief": 0})()
    buf = io.StringIO()
    with redirect_stdout(buf):
        for x in r["results"]:
            print(f"{x['rank']}. [{x['id']}] {x['title']}（{x['broker']} {str(x['date'] or '无日期')[:7]}｜{x['frequency']}｜{x['type']}）"
                  f"\n   {x['gist']}｜命中：{'、'.join(x['hits']) or '—'}｜via {x['via'] or '—'}")
    return buf.getvalue()


def main():
    s = K.Searcher()
    cards = {c["id"]: c for c in s.cards}
    full = {i: len((K.KB / "cards" / f"{i}.md").read_text()) for i in cards}
    briefs = {i: K.brief(c) for i, c in cards.items()}
    pool = [c for c in s.cards if c["report_type"] not in K.EXCLUDE_TYPES]
    suffix = {c["id"][-6:]: c["id"] for c in pool}
    rows = []
    for n, (q, kind, kw, cond, gold) in enumerate(QUERIES):
        gold = {suffix[g] for g in gold.split()}
        # 舊流程
        rx = re.compile(kw, re.I)
        old = {c["id"] for c in pool if rx.search((K.KB / "cards" / f"{c['id']}.md").read_text()) or (cond and cond(c))}
        old_chars = sum(full[i] for i in old)
        # 新流程
        f = FILTERS.get(n, {})
        r = s.search(q, top=TOP, **f)
        got = [x["id"] for x in r["results"]]
        lst = len(list_text(r))
        if f.get("all_"):
            new_read, new_chars, depth = set(got), lst, "清单（--all）"
        else:
            new_read = set(got[:BRIEF])
            new_chars = lst + sum(len(briefs[i]) + 2 for i in got[:BRIEF])
            depth = f"清单 {TOP}＋精简卡 {BRIEF}"
        rec = lambda x: len(x & gold) / len(gold)
        miss = [f"{i[-6:]}（第 {got.index(i) + 1} 名）" if i in got else f"{i[-6:]}（不在前 {TOP}）"
                for i in sorted(gold - new_read)]
        rows.append((q, kind, len(gold), rec(old), len(old), old_chars, rec(set(got)), rec(new_read), depth,
                     new_chars, miss))
    pct = lambda x: f"{x:.0%}"
    lines = ["# kb-search 驗收：新舊流程的召回與讀進字數", "",
             "2026-11-26。題目與標準答案沿用 `vector_search.py`（同一個人定的，舊流程偏樂觀）。",
             "舊流程：關鍵詞 grep 卡片，候選整張讀。新流程：kb-search 清單前 15 名＋前 8 名精簡卡；第 1 題（列表型）用頻率篩選＋`--all`，只讀清單。",
             "「讀進字數」只算知識庫內容。中文大約一字一 token。", "",
             "| 問題 | 類型 | 標準答案 | 舊召回 | 舊候選數 | 舊讀進字數 | 新：清單召回 | 新：精簡卡召回 | 新流程讀什麼 | 新讀進字數 | 新流程沒細讀到的標準答案 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {pct(r[3])} | {r[4]} | {r[5]:,} | {pct(r[6])} | {pct(r[7])} | {r[8]} | {r[9]:,} | "
                     f"{'、'.join(r[10]) or '無'} |")
    avg = lambda i: sum(r[i] for r in rows) / len(rows)
    lines += [f"| **平均** | | | {pct(avg(3))} | {avg(4):.0f} | {avg(5):,.0f} | {pct(avg(6))} | {pct(avg(7))} | | {avg(9):,.0f} | |", "",
              f"精簡卡平均 {sum(map(len, briefs.values())) / len(briefs):.0f} 字；整卡平均 {sum(full.values()) / len(full):.0f} 字。"]
    # 精簡卡相對整卡少了什麼（不用模型，只數各節的條目）
    n_steps = [len(K.steps(c["sec"])) for c in pool]
    n_res = [len([r for r in re.findall(r"^\|(.+)\|$", c["sec"].get("关键结果", ""), re.M)
                  if not re.match(r"\s*(指标|-+)\s*\|", r)]) for c in pool]
    has = lambda name: sum(1 for c in pool if c["sec"].get(name)) / len(pool)
    lines += ["", "## 精簡卡相對整卡少了什麼", "",
              f"- 方法步驟：平均每卡 {sum(n_steps) / len(pool):.1f} 步，精簡卡列前 5 步的標題（各 ≤30 字，不含引文）；"
              f"步驟超過 5 步的卡占 {sum(x > 5 for x in n_steps) / len(pool):.0%}。",
              f"- 關鍵結果：平均每卡 {sum(n_res) / len(pool):.1f} 條，精簡卡只列前 2 條；超過 2 條的卡占 {sum(x > 2 for x in n_res) / len(pool):.0%}。",
              f"- 精簡卡完全沒有的節：核心公式（{has('核心公式'):.0%} 的卡有）、局限（{has('局限'):.0%}）、"
              f"复现缺口（{has('复现缺口（原文没交代的）'):.0%}）、所需数据（{has('所需数据'):.0%}）、原文引文。",
              "- 所以精簡卡夠回答「有沒有、誰做過、核心做法與結論」；要照著實現（完整步驟、參數、公式、所需數據）或評估風險（局限、復現缺口）就要讀整卡。"]
    OUT.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
