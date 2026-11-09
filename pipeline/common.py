"""知識庫 pipeline 共用工具：路徑、詞彙表、卡片 schema、prompt 組裝。"""
import hashlib
import json
import re
from pathlib import Path

import yaml

KB = Path(__file__).resolve().parent.parent
PIPELINE = KB / "pipeline"
INBOX = KB / "inbox" / "poc"
FULLTEXT = KB / "fulltext"
CARDS = KB / "cards"

REPORT_TYPES = ["深度专题", "系列专题", "周报点评", "产品分析", "其他"]
LEVELS = ["high", "medium", "low"]


def load_env():
    env = {}
    for line in (KB / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_vocab():
    return yaml.safe_load((PIPELINE / "vocab.yaml").read_text())


def report_id(pdf: Path) -> str:
    """穩定的研報 id：日期_券商_檔案雜湊前 6 碼。"""
    m = re.match(r"(\d{8})-([^-]+)-", pdf.name)
    date, broker = (m.group(1), m.group(2)) if m else ("nodate", "unknown")
    digest = hashlib.sha1(pdf.read_bytes()).hexdigest()[:6]
    return f"{date}_{broker}_{digest}"


def poc_pdfs():
    return sorted(p for p in INBOX.glob("*.pdf"))


def card_schema(vocab):
    fams = list(vocab["families"])
    meths = list(vocab["methods"])
    s = {"type": "string"}
    arr = lambda items, **kw: {"type": "array", "items": items, **kw}
    obj = lambda props: {"type": "object", "properties": props,
                         "required": list(props), "additionalProperties": False}
    props = {
        "title": s, "broker": s,
        "date": {"type": ["string", "null"]}, "series": {"type": ["string", "null"]},
        "report_type": {"enum": REPORT_TYPES},
        "value_level": {"enum": LEVELS}, "value_reason": s,
        "primary_family": {"enum": fams},
        "families": arr({"enum": fams}),
        "methods": arr({"enum": meths}, maxItems=8),
        "new_tag_suggestions": arr(s),
        "data_needed": arr(s),
        "summary": s, "core_idea": s,
        "method_steps": arr(s),
        "key_formulas": arr(obj({"latex": s, "meaning": s}), maxItems=5),
        "key_results": arr(obj({"metric": s, "value": s, "context": s, "quote": s}), maxItems=6),
        "limitations": arr(s),
        "reproducibility": {"enum": LEVELS}, "reproducibility_reason": s,
        "evidence_quotes": arr(obj({"claim": s, "quote": s}), maxItems=4),
    }
    return obj(props)


def build_prompt(vocab, filename, fulltext):
    fams = "\n".join(f"- `{k}`：{v['zh']}" for k, v in vocab["families"].items())
    meths = "\n".join(f"- `{k}`：{v['zh']}（{v['family']}；{'、'.join(map(str, v['aliases']))}）"
                      for k, v in vocab["methods"].items())
    tpl = (PIPELINE / "card_prompt.md").read_text()
    return (tpl.replace("{families}", fams).replace("{methods}", meths)
               .replace("{filename}", filename).replace("{fulltext}", fulltext))


def extract_json(text: str):
    """從模型輸出取出 JSON 物件（容忍 ```json 包裹、前後雜訊、<think> 區塊）。"""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])
