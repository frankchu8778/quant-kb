"""卡片的自動檢查：schema、詞彙表、數字與引用能否在全文中找到（V3 的自動項目）。"""
import html
import re

from rapidfuzz import fuzz

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


def quote_found(quote, fulltext_norm, threshold=90):
    q = _norm(quote)
    if not q:
        return False
    if q in fulltext_norm:
        return True
    return fuzz.partial_ratio(q, fulltext_norm) >= threshold


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


def value_numbers_not_in_quote(card):
    """key_results.value 的每個數字都要出現在同一列的 quote 裡；回傳不符合的列。"""
    bad = []
    for i, r in enumerate(card.get("key_results") or []):
        q = r.get("quote") or ""
        miss = [n for n in NUM_RE.findall(r.get("value") or "")
                if len(n.replace("-", "").replace(".", "")) >= 2 and n.lstrip("-") not in q]
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
            problems.append(f"{path}.quote 在全文找不到：{q[:60]}")
    for b in value_numbers_not_in_quote(card):
        problems.append(f"key_results.{b['row']}.value 的數字 {b['missing']} 不在同列引文裡（{b['value'][:40]}）")
    return problems
