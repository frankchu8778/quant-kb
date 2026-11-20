"""卡片的自動檢查：schema、詞彙表、數字與引用能否在全文中找到（V3 的自動項目）。"""
import html
import re


from common import LEVELS, REPORT_TYPES

REQUIRED = ["title", "broker", "date", "series", "report_type", "value_level", "value_reason",
            "primary_family", "families", "methods", "new_tag_suggestions", "data_needed",
            "summary", "core_idea", "method_steps", "key_formulas", "key_results", "limitations",
            "reproducibility", "reproducibility_reason", "evidence_quotes"]


def validate_card(card, vocab):
    """回傳問題清單（空清單 = 格式與詞彙表都合法）。"""
    errs = []
    for k in REQUIRED:
        if k not in card:
            errs.append(f"缺欄位 {k}")
    fams, meths = set(vocab["families"]), set(vocab["methods"])
    if card.get("primary_family") not in fams:
        errs.append(f"primary_family 不在詞彙表: {card.get('primary_family')}")
    for f in card.get("families") or []:
        if f not in fams:
            errs.append(f"family 不在詞彙表: {f}")
    for m in card.get("methods") or []:
        if m not in meths:
            errs.append(f"method 不在詞彙表: {m}")
    if card.get("report_type") not in REPORT_TYPES:
        errs.append(f"report_type 非法: {card.get('report_type')}")
    for k in ("value_level", "reproducibility"):
        if card.get(k) not in LEVELS:
            errs.append(f"{k} 非法: {card.get(k)}")
    return errs


HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>")


def _norm(s):
    """比對用正規化：去 HTML 標籤與實體、空白與常見標點差異。

    MinerU 的全文用 HTML 表示表格（<td>）與底線、粗體（<u>、<strong>），寫卡模型引用時不會照抄標籤；
    表格的引文常寫成 Markdown 的 | 分隔，所以 | 也當標點去掉。先去標籤再解實體，&lt; 才不會被當成標籤。
    """
    s = html.unescape(HTML_TAG_RE.sub("", s or ""))
    return re.sub(r"[\s　，,。．.：:；;、（）()\[\]【】\"'“”‘’%％\-—_*$\\{}|｜]", "", s)


def quote_found(quote, fulltext_norm):
    """引文正規化後要逐字出現在全文裡。

    以前比不到時會退回模糊比對（partial_ratio ≥ 90），結果引文改掉一個數字或刪掉「不」字都還判在原文
    （TODO 第 26 項）。既有 659 條引文有 658 條逐字比得到，所以只比逐字。
    """
    q = _norm(quote)
    return bool(q) and q in fulltext_norm


NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def numbers_found(value, fulltext):
    """key_results.value 裡的每個數字都要出現在全文中；回傳找不到的數字。"""
    missing = []
    for n in NUM_RE.findall(value or ""):
        if len(n.replace("-", "").replace(".", "")) < 2:  # 單一位數太容易誤判，略過
            continue
        variants = {n, n.lstrip("-"), n.rstrip("0").rstrip(".") if "." in n else n}
        if not any(v and v in fulltext for v in variants):
            missing.append(n)
    return missing


def grounding_report(card, fulltext):
    """檢查卡片裡的引用與數字是否有原文根據。"""
    ft_norm = _norm(fulltext)
    quotes = [r.get("quote", "") for r in card.get("key_results") or []] + \
             [q.get("quote", "") for q in card.get("evidence_quotes") or []]
    found = [quote_found(q, ft_norm) for q in quotes]
    missing_nums = []
    for r in card.get("key_results") or []:
        for n in numbers_found(r.get("value", ""), fulltext):
            missing_nums.append({"metric": r.get("metric"), "value": r.get("value"), "missing": n})
    return {"quotes_total": len(quotes), "quotes_found": sum(found),
            "quotes_missing": [q for q, ok in zip(quotes, found) if not ok],
            "numbers_missing": missing_nums}


# 完整的數字：千分位寫法（1,234.5）或一般寫法；前後不能再接數字或小數點，27.9 才不會被 27.91 包含
NUM_TOKEN_RE = re.compile(r"(?<![\d.])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\d)|(?<![\d.])\d+(?:\.\d+)?")


def _dec(t):
    """13.00 → 13、27.90 → 27.9；整數不動（400 還是 400）。"""
    return t.rstrip("0").rstrip(".") if "." in t else t


def _num_tokens(s):
    """切出字串裡的完整數字。逗號可能是千分位也可能是列舉（1,2,3），兩種讀法都收。"""
    s = s or ""
    out = set()
    for t in NUM_TOKEN_RE.findall(s) + re.findall(r"\d+(?:\.\d+)?", s):
        t = t.replace(",", "")
        out.update({t, _dec(t)})
    return out


def value_numbers_not_in_quote(card):
    """key_results.value 的每個數字（兩位數以上）都要是同一列 quote 裡的一個完整數字；回傳不符合的列。

    以前用字串包含，27.9 在 27.91 裡也算通過（TODO 第 27 項）。
    """
    bad = []
    for i, r in enumerate(card.get("key_results") or []):
        qt = _num_tokens(r.get("quote"))
        miss = []
        for n in NUM_TOKEN_RE.findall(r.get("value") or ""):
            t = n.replace(",", "")
            if len(t.replace(".", "")) >= 2 and t not in qt and _dec(t) not in qt:
                miss.append(n)
        if miss:
            bad.append({"row": i, "metric": r.get("metric"), "value": r.get("value"), "missing": miss})
    return bad


def gate_v4(card, fulltext, schema):
    """量產的程式硬檢查（卡片 schema v4）。回傳問題清單，空清單 = 通過。

    檢查：schema（含詞彙表 enum）、所有引文在全文、數字在同列引文、每個方法步驟都有引文。
    """
    import jsonschema
    problems = [f"schema {list(e.absolute_path)}: {e.message[:150]}"
                for e in jsonschema.Draft202012Validator(schema).iter_errors(card)]
    if problems:
        return problems
    ft_norm = _norm(fulltext)
    quotes = ([(f"key_results.{i}", r["quote"]) for i, r in enumerate(card["key_results"])] +
              [(f"evidence_quotes.{i}", r["quote"]) for i, r in enumerate(card["evidence_quotes"])] +
              [(f"method_steps.{i}", r["quote"]) for i, r in enumerate(card["method_steps"])])
    for path, q in quotes:
        if "..." in q or "……" in q or "…" in q:
            problems.append(f"{path}.quote 用省略號拼接：{q[:60]}")
        elif not quote_found(q, ft_norm):
            problems.append(f"{path}.quote 在全文找不到（要逐字照抄原文）：{q[:60]}")
    for b in value_numbers_not_in_quote(card):
        problems.append(f"key_results.{b['row']}.value 的數字 {b['missing']} 不在同列引文裡（{b['value'][:40]}）")
    return problems
