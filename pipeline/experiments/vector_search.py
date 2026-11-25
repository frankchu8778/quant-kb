"""小實驗：向量檢索 vs 現在 skill 的關鍵詞／標籤查詢（2026-11-25）。

只在本機跑：bge-m3（~/models/bge-m3）在 GPU 上算向量，不送任何 API。
比較四種找候選卡的方法，看 10 個問題的標準答案各找回幾張：
  kw       ：照 skill 的做法，事先寫好的簡體關鍵詞正則 grep 卡片（＋標籤／頻率欄位），結果不排序
  vec_card ：卡片的「標題＋摘要＋核心思路」一張一個向量，取前 k
  vec_full ：全文切段（約 600 字），每張卡取最相似那段的分數，取前 k
  hybrid   ：kw 的候選 ∪ vec_card 前 10

用法：~/venvs/kb-parse/bin/python experiments/vector_search.py   （第一次會建索引，存在 experiments/_cache/）
結果寫到 pipeline/results/vector_experiment.md
"""
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
KB = HERE.parents[1] / "kb"
CACHE = HERE / "_cache"
OUT = HERE.parent / "results" / "vector_experiment.md"
MODEL = Path.home() / "models" / "bge-m3"
CHUNK, OVERLAP = 600, 100

# 問題、關鍵詞正則（照 skill 步驟事先寫好，沒看向量結果）、額外的結構化條件、標準答案（卡片 id 尾碼）
QUERIES = [
    ("股指期货日内交易策略", "列表型",
     r"日内|intraday", lambda c: c["frequency"] in ("intraday", "high-frequency"),
     "bc120a 0d695e c6d941 dc6e75 390b56 c652a3 33c30e 9dbd98 badcc4 69f67d 5d809e 8e8832 a2b9c7 9accd0 afdcae 6d2c78 f3ecce 8b41bf b5a6cc c3e3a3"),
    ("开盘区间突破策略", "具體方法",
     r"开盘区间|区间突破|开盘价.{0,6}突破|突破.{0,6}开盘", None,
     "dc6e75 5d809e f3ecce 9accd0"),
    ("用遗传规划自动挖掘因子或交易信号", "具體方法",
     r"遗传规划|genetic programming|gplearn|基因表达式规划|符号回归", None,
     "badcc4 c6d941 c652a3 8028f9 1deca7 4fb166 39b90c"),
    ("用分钟级高频数据构建低频选股因子", "具體方法",
     r"分钟.{0,10}(因子|数据)|高频因子|高频数据低频化|Level-?2", None,
     "b4e2fb 8cd304 bcd062 4b2299 eaa280 559b26 18bdec 8eb84f"),
    ("根据历史上相似的走势来预测未来涨跌", "換句話說",
     r"相似|模式匹配|模板匹配|动态时间规整|DTW|历史重演", None,
     "0d695e 9b9a4b ae7d06 1934bc"),
    ("先判断市场处于趋势还是震荡，再决定交易方式", "換句話說",
     r"趋势.{0,4}震荡|震荡.{0,4}趋势|市场状态|趋势市|震荡市", None,
     "6d2c78 390b56 bc120a 9dbd98 afdcae 7c2ac9"),
    ("大单主动买入卖出的资金流向", "換句話說",
     r"资金流|主动买|主动卖|大单", None,
     "8b41bf c5ca80 656481"),
    ("用信号处理方法给价格去噪后做择时", "換句話說",
     r"希尔伯特|经验模态|EMD|小波|傅里叶|滤波|最大熵谱|MESA|去噪|消噪", None,
     "939a2f 51e430 dcc0be bc120a 390b56 6d2c78"),
    ("股指期货跨品种或大小盘风格套利", "具體方法",
     r"跨品种|风格套利|IF.{0,3}IC|上证50.{0,6}中证500", None,
     "ea49c3 86cad1 ef3374"),
    ("比较不同止损止盈方式对日内策略收益的影响", "細節型",
     r"止损.{0,10}(比较|对比|方式)|止盈止损|跟踪止盈|移动止损", None,
     "5d809e f3ecce ae7d06 9accd0 0d695e"),
]


def load_cards():
    cards = []
    for line in (KB / "catalog.jsonl").read_text().splitlines():
        c = json.loads(line)
        if c["report_type"] == "周报点评":   # 問方法時 skill 也先排除週報
            continue
        md = (KB / "cards" / f"{c['id']}.md").read_text()
        sec = lambda name: (re.search(rf"## {name}\n\n(.*?)\n\n## ", md, re.S) or [None, ""])[1]
        c["text"] = f"{c['title']}\n{sec('摘要')}\n{sec('核心思路')}"
        c["md"] = md
        c["full"] = (KB / "fulltext" / f"{c['id']}.md").read_text()
        cards.append(c)
    return cards


def chunks(text):
    text = re.sub(r"<[^>]+>", " ", text)            # HTML 表格只留文字
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)   # 圖片
    text = re.sub(r"[ \t]+", " ", text)
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + CHUNK])
        i += CHUNK - OVERLAP
    return [c for c in out if len(c.strip()) > 50]


def build(cards, model):
    CACHE.mkdir(exist_ok=True)
    ids = [c["id"] for c in cards]
    key = CACHE / "ids.json"
    if key.exists() and json.loads(key.read_text()) == ids:
        return np.load(CACHE / "card.npy"), np.load(CACHE / "chunk.npy"), json.loads((CACHE / "chunk_owner.json").read_text()), None
    t = time.time()
    card_vec = model.encode([c["text"] for c in cards], batch_size=32, normalize_embeddings=True)
    t_card = time.time() - t
    texts, owner = [], []
    for n, c in enumerate(cards):
        for ch in chunks(c["full"]):
            texts.append(ch)
            owner.append(n)
    t = time.time()
    chunk_vec = model.encode(texts, batch_size=64, normalize_embeddings=True, show_progress_bar=True)
    t_chunk = time.time() - t
    np.save(CACHE / "card.npy", card_vec)
    np.save(CACHE / "chunk.npy", chunk_vec)
    (CACHE / "chunk_owner.json").write_text(json.dumps(owner))
    key.write_text(json.dumps(ids))
    return card_vec, chunk_vec, owner, (t_card, t_chunk, len(texts))


def main():
    from sentence_transformers import SentenceTransformer
    cards = load_cards()
    model = SentenceTransformer(str(MODEL), device="cuda")
    model.half()
    card_vec, chunk_vec, owner, timing = build(cards, model)
    owner = np.array(owner)
    suffix = {c["id"][-6:]: n for n, c in enumerate(cards)}
    rows, detail = [], []
    for q, kind, kw, cond, gold in QUERIES:
        gold = {suffix[g] for g in gold.split()}
        t = time.time()
        qv = model.encode([q], normalize_embeddings=True)[0]
        s_card = card_vec @ qv
        s_chunk = np.full(len(cards), -1.0)
        np.maximum.at(s_chunk, owner, chunk_vec @ qv)
        q_ms = (time.time() - t) * 1000
        rank_card = list(np.argsort(-s_card))
        rank_full = list(np.argsort(-s_chunk))
        rx = re.compile(kw, re.I)
        kw_set = {n for n, c in enumerate(cards) if rx.search(c["md"]) or (cond and cond(c))}
        hyb = kw_set | set(rank_card[:10])
        # kw 候選照卡片向量分數排序後，讀到第幾張才把 kw 找到的標準答案讀齊（不排序時要讀完全部 len(kw_set) 張）
        kw_sorted = [n for n in rank_card if n in kw_set]
        hit = [i for i, n in enumerate(kw_sorted, 1) if n in gold]
        read_to = hit[-1] if hit else 0
        rec = lambda got: len(got & gold) / len(gold)
        rows.append((q, kind, len(gold), rec(kw_set), len(kw_set),
                     rec(set(rank_card[:5])), rec(set(rank_card[:10])), rec(set(rank_full[:5])), rec(set(rank_full[:10])),
                     rec(hyb), len(hyb), q_ms, read_to))
        name = lambda n: f"{cards[n]['id'][-6:]} {cards[n]['title'][:18]}"
        detail.append((q, [name(n) for n in gold - kw_set], [name(n) for n in gold - set(rank_card[:10])],
                       [name(n) for n in rank_card[:10] if n not in gold][:5], [name(n) for n in gold - hyb]))
    pct = lambda x: f"{x:.0%}"
    lines = ["# 向量檢索小實驗：bge-m3 vs 現在的關鍵詞／標籤查詢", "",
             f"2026-11-25。知識庫 {len(cards)} 張卡（排除週報點評）。程式：`pipeline/experiments/vector_search.py`。",
             "召回率＝標準答案裡被找回的比例。kw 不排序，「候選數」是要逐張讀的卡數；向量取前 5、前 10。",
             "「kw 依向量排序讀到第幾張」：把 kw 候選照卡片向量分數排序，讀到第幾張才把 kw 找到的標準答案讀齊（分母是不排序要讀的張數）。",
             "注意：標準答案和 kw 的關鍵詞都是同一個人（Claude）定的，而且事先讀過這些卡，kw 的結果偏樂觀。", ""]
    if timing:
        lines += [f"建索引：卡片 {len(cards)} 個向量 {timing[0]:.1f} 秒；全文切成 {timing[2]} 段，{timing[1]:.1f} 秒（RTX 5090，fp16）。", ""]
    lines += ["| 問題 | 類型 | 標準答案 | kw 召回 | kw 候選數 | 卡片向量@5 | 卡片向量@10 | 全文向量@5 | 全文向量@10 | 混合召回 | 混合候選數 | kw 依向量排序讀到第幾張 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {pct(r[3])} | {r[4]} | {pct(r[5])} | {pct(r[6])} | {pct(r[7])} | {pct(r[8])} | {pct(r[9])} | {r[10]} | {r[12]}/{r[4]} |")
    avg = lambda i: sum(r[i] for r in rows) / len(rows)
    lines += [f"| **平均** | | | {pct(avg(3))} | {avg(4):.0f} | {pct(avg(5))} | {pct(avg(6))} | {pct(avg(7))} | {pct(avg(8))} | {pct(avg(9))} | {avg(10):.0f} | {avg(12):.0f}/{avg(4):.0f} |", "",
              f"每次向量查詢（含把問題轉向量）平均 {avg(11):.0f} 毫秒。", "", "## 各問題漏掉的卡", ""]
    for q, kw_miss, vec_miss, vec_noise, hyb_miss in detail:
        lines += [f"### {q}", "",
                  f"- kw 漏掉：{'、'.join(kw_miss) or '無'}",
                  f"- 卡片向量前 10 漏掉：{'、'.join(vec_miss) or '無'}",
                  f"- 卡片向量前 10 裡不相關的（最多 5 張）：{'、'.join(vec_noise) or '無'}",
                  f"- 混合仍漏掉：{'、'.join(hyb_miss) or '無'}", ""]
    OUT.write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
