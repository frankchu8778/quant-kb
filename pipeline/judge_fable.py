"""用 Claude（預設 Fable 5.2，可用 --model 換 Opus 等）審核卡片（流程見 judge/README.md）。

用法：
  python judge_fable.py --only 20190702_财通证券_8a9283      # 試跑 1 篇
  python judge_fable.py                                       # 全部 20 篇

每篇兩次 headless 呼叫（無工具、空 system prompt、structured output）：
  pass1：只看全文 → Fable 參考卡片 + 必須涵蓋的要點 + 分類回饋
  pass2：全文 + pass1 筆記 + 匿名卡片 A/B/C → 問題清單、評分、排名
輸出在 eval/fable/<id>/；已存在的結果會跳過，中斷後重跑即可續跑。
"""
import argparse
import copy
import os
import json
import random
import subprocess
import time
from pathlib import Path

import yaml

from common import KB, PIPELINE, REPORT_TYPES, load_vocab, vocab_blocks

JUDGE = PIPELINE / "judge"
OUT = KB / "eval" / "fable"
SRC = KB / "fulltext" / "poc" / "clean-v2"
ENGINES = ["sonnet-v2", "minimax-v2", "qwen-v2-think"]
REMOVED = ["value_level", "value_reason", "reproducibility", "reproducibility_reason"]
CARD_ORDER = ["title", "broker", "date", "series", "report_type", "primary_family", "families",
              "methods", "new_tag_suggestions", "data_needed", "summary", "core_idea",
              "method_steps", "key_formulas", "key_results", "limitations", "evidence_quotes"]
ISSUE_TYPES = ["事实错误", "数字张冠李戴", "过度推论", "引用不实", "遗漏要点", "标签错误", "其他"]
SEVERITIES = ["critical", "major", "minor"]
RATE_LIMIT_HINTS = ("rate limit", "usage limit", "limit reached", "429", "overloaded")

s = {"type": "string"}
arr = lambda items: {"type": "array", "items": items}
obj = lambda props: {"type": "object", "properties": props,
                     "required": list(props), "additionalProperties": False}
enum = lambda vals: {"enum": vals}


def card_v3_schema(vocab, st):
    """新卡片 schema：去掉評分欄位，加策略大類、頻率、資產類別、implementation_gaps。"""
    fams, meths, types = list(vocab["families"]), list(vocab["methods"]), list(st["strategy_types"])
    return obj({
        "title": s, "broker": s,
        "date": {"type": ["string", "null"]}, "series": {"type": ["string", "null"]},
        "report_type": enum(REPORT_TYPES),
        "primary_strategy_type": enum(types), "strategy_types": arr(enum(types)),
        "frequency": enum(st["frequency"]), "asset_class": arr(enum(st["asset_class"])),
        "families": arr(enum(fams)), "methods": arr(enum(meths)),
        "new_tag_suggestions": arr(s), "data_needed": arr(s),
        "summary": s, "core_idea": s, "method_steps": arr(s),
        "key_formulas": arr(obj({"latex": s, "meaning": s})),
        "key_results": arr(obj({"metric": s, "value": s, "context": s, "quote": s})),
        "limitations": arr(s), "implementation_gaps": arr(s),
        "evidence_quotes": arr(obj({"claim": s, "quote": s})),
    })


def pass1_schema(vocab, st):
    return obj({
        "reference_card": card_v3_schema(vocab, st),
        "must_capture": arr(obj({"point": s, "why": s, "quote": s, "section": s})),
        "classification_notes": s,
        "taxonomy_feedback": s,
        "conversion_issues": arr(obj({"location": s, "problem": s, "impact": s})),
    })


def pass2_schema():
    score = {"type": "integer", "enum": [0, 1, 2]}
    card_eval = obj({
        "issues": arr(obj({"field": s, "type": enum(ISSUE_TYPES), "severity": enum(SEVERITIES),
                           "card_says": s, "source_says": s, "quote": s, "section": s})),
        "scores": obj({k: score for k in ["faithfulness", "core_idea", "method_borrowable",
                                          "key_results", "data_needed", "tags"]}),
        "borrower_test": obj({"verdict": enum(["correct", "partial", "wrong"]), "reason": s}),
    })
    cov = enum(["covered", "partial", "missing"])
    return obj({
        "cards": obj({k: card_eval for k in "ABC"}),
        "ranking": obj({"order": arr(enum(list("ABC"))), "reason": s}),
        "must_capture_coverage": arr(obj({"point": s, "A": cov, "B": cov, "C": cov})),
        "reference_corrections": s,
        "conversion_issues": arr(obj({"location": s, "problem": s, "impact": s})),
    })


def run_fable(prompt, schema, effort, wait, max_waits, model="claude-fable-5-2", no_cache=False):
    env = os.environ | ({"DISABLE_PROMPT_CACHING": "1"} if no_cache else {})
    cmd = ["claude", "-p", "--model", model, "--effort", effort,
           "--tools", "", "--system-prompt", "", "--strict-mcp-config",
           "--no-session-persistence", "--output-format", "json",
           "--json-schema", json.dumps(schema, ensure_ascii=False)]
    for attempt in range(max_waits + 1):
        t0 = time.time()
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=3600, env=env)
        secs = round(time.time() - t0, 1)
        try:
            res = json.loads(p.stdout)
        except json.JSONDecodeError:
            res = None
        text = ((res or {}).get("result") or "") + p.stderr
        limited = (res is None or res.get("is_error")) and any(h in text.lower() for h in RATE_LIMIT_HINTS)
        if limited and attempt < max_waits:
            print(f"    rate-limited, wait {wait}s ({attempt + 1}/{max_waits})", flush=True)
            time.sleep(wait)
            continue
        return res, (p.stdout + "\n" + p.stderr)[-3000:], secs


# DeepSeek 離峰價（USD / 百萬 token；第三方整理，2026-09～10，未經官方確認）；尖峰為 2 倍
DEEPSEEK_PRICE = {"deepseek-flash": (0.15, 0.60), "deepseek-v4-pro": (0.66, 1.98)}
SUBMIT_HINT = "\n\n---\n完成后，调用 `submit` 工具提交结果（参数必须符合工具的 schema），不要在正文输出结果。"


def run_deepseek(prompt, schema, effort, model, attempts=3):
    """DeepSeek 的 Anthropic 相容端點。沒有 structured output，改用工具呼叫取 JSON，再用 jsonschema 驗證。"""
    import anthropic
    import jsonschema
    from common import load_env
    client = anthropic.Anthropic(base_url="https://api.deepseek.com/anthropic",
                                 api_key=load_env()["DEEPSEEK_API_KEY"], timeout=3600, max_retries=3)
    tool = {"name": "submit", "description": "提交审核结果", "input_schema": schema}
    t0, log, total = time.time(), [], {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}
    messages = [{"role": "user", "content": prompt + SUBMIT_HINT}]
    last_bad = None
    for i in range(attempts):
        with client.messages.stream(model=model, max_tokens=64000, tools=[tool], tool_choice={"type": "auto"},
                                    messages=messages,
                                    extra_body={"output_config": {"effort": effort}}) as st:
            r = st.get_final_message()
        u = r.usage
        total["input_tokens"] += u.input_tokens + (u.cache_read_input_tokens or 0)
        total["output_tokens"] += u.output_tokens
        total["thinking_tokens"] += getattr(u.output_tokens_details, "thinking_tokens", 0) or 0
        uses = [b for b in r.content if b.type == "tool_use" and b.name == "submit"]
        if not uses:
            log.append(f"attempt {i + 1}: 沒有呼叫 submit（stop_reason={r.stop_reason}）")
            messages = messages + [{"role": "assistant", "content": r.content},
                                   {"role": "user", "content": "请调用 `submit` 工具提交完整结果。"}]
            continue
        calls = [uses[0].input]
        errs = sorted(jsonschema.Draft202012Validator(schema).iter_errors(calls[0]), key=lambda e: list(e.absolute_path))
        if not errs:
            break
        msg = "；".join(f"{list(e.absolute_path)}: {e.message[:200]}" for e in errs[:10])
        log.append(f"attempt {i + 1}: schema 不符（{len(errs)} 處）：{msg}")
        last_bad = calls[0]
        # 把錯誤回給模型，讓它補正後重新提交（保留 thinking 與 tool_use 區塊，DeepSeek 要求）
        messages = messages + [
            {"role": "assistant", "content": r.content},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": uses[0].id, "is_error": True,
                                          "content": f"提交的结果不符合 schema：{msg}。请修正后重新调用 submit，提交完整结果（所有必填字段）。"}]}]
    else:
        calls = [None]
    pin, pout = DEEPSEEK_PRICE.get(model, (0, 0))
    cost = round(total["input_tokens"] / 1e6 * pin + total["output_tokens"] / 1e6 * pout, 5)
    res = {"structured_output": calls[0], "usage": total, "total_cost_usd": cost,
           "is_error": calls[0] is None, "num_turns": len(log) + (calls[0] is not None),
           "stop_reason": r.stop_reason, "retry_log": log, "last_invalid_submission": last_bad,
           "utc_time": time.strftime("%Y-%m-%d %H:%M %a", time.gmtime())}
    return res, "\n".join(log), round(time.time() - t0, 1)


def set_path(card, path, value):
    """path 例：key_results.0.value、methods.1、core_idea"""
    keys = [int(k) if k.isdigit() else k for k in path.split(".")]
    node = card
    for k in keys[:-1]:
        node = node[k]
    old = node[keys[-1]]
    node[keys[-1]] = value
    return old


def prepare_cards(rid, canaries, seed):
    cards = {}
    for eng in ENGINES:
        c = json.loads((KB / "cards" / "poc" / eng / f"{rid}.json").read_text())
        c = {k: c[k] for k in CARD_ORDER if k in c} | {k: v for k, v in c.items()
                                                       if k not in CARD_ORDER and k not in REMOVED}
        for cn in canaries:
            if cn["rid"] == rid and cn["engine"] == eng:
                c = copy.deepcopy(c)
                cn["original"] = set_path(c, cn["path"], cn["new"])
        cards[eng] = c
    order = ENGINES[:]
    random.Random(f"{seed}-{rid}").shuffle(order)
    return {label: cards[eng] for label, eng in zip("ABC", order)}, dict(zip("ABC", order))


def fill(tpl, **kw):
    for k, v in kw.items():
        tpl = tpl.replace("{" + k + "}", v)
    return tpl


def call_and_save(d, name, prompt, schema, args):
    if (d / f"{name}.json").exists():
        print(f"  skip {name}", flush=True)
        return json.loads((d / f"{name}.json").read_text())
    (d / f"{name}.prompt.md").write_text(prompt)
    if args.model.startswith("deepseek"):
        res, tail, secs = run_deepseek(prompt, schema, args.effort, args.model)
    else:
        res, tail, secs = run_fable(prompt, schema, args.effort, args.wait, args.max_waits, args.model, args.no_cache)
    meta = {"seconds": secs, "prompt_chars": len(prompt), "effort": args.effort,
            "model": args.model, "no_cache": args.no_cache}
    if res:
        meta.update(total_cost_usd_api_equiv=res.get("total_cost_usd"), usage=res.get("usage"),
                    model_usage=res.get("modelUsage"), is_error=res.get("is_error"),
                    num_turns=res.get("num_turns"), stop_reason=res.get("stop_reason"))
        (d / f"{name}.cli.json").write_text(json.dumps(res, ensure_ascii=False, indent=2))
    out = (res or {}).get("structured_output")
    if not out:
        meta["error"] = tail
        (d / f"{name}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        raise RuntimeError(f"{name} 沒有 structured_output，詳見 {d / (name + '.meta.json')}")
    (d / f"{name}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    (d / f"{name}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    u = meta.get("usage") or {}
    print(f"  {name}: {secs}s  in={u.get('input_tokens', 0) + u.get('cache_creation_input_tokens', 0) + u.get('cache_read_input_tokens', 0)}"
          f"  out={u.get('output_tokens')}  ${meta.get('total_cost_usd_api_equiv')}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--model", default="claude-fable-5-2")
    ap.add_argument("--out", default="eval/fable", help="輸出資料夾（相對 KB）")
    ap.add_argument("--no-cache", action="store_true", help="關閉 prompt 快取（每篇只讀一次，快取寫入是白花的）")
    ap.add_argument("--seed", default="fable-judge-v1")
    ap.add_argument("--canaries", default="eval/fable/_canaries.json")
    ap.add_argument("--wait", type=int, default=900, help="撞到額度限制時等待秒數")
    ap.add_argument("--max-waits", type=int, default=24)
    args = ap.parse_args()

    vocab = load_vocab()
    st_text = (JUDGE / "strategy_types.yaml").read_text()
    st = yaml.safe_load(st_text)
    fams, meths = vocab_blocks(vocab)
    tpl1, tpl2 = (JUDGE / "pass1_prompt.md").read_text(), (JUDGE / "pass2_prompt.md").read_text()
    s1, s2 = pass1_schema(vocab, st), pass2_schema()
    canaries_path = KB / args.canaries
    canaries = json.loads(canaries_path.read_text()) if canaries_path.exists() else []

    out_root = KB / args.out
    out_root.mkdir(parents=True, exist_ok=True)
    mapping_path = out_root / "_mapping.json"
    mapping = json.loads(mapping_path.read_text()) if mapping_path.exists() else {}
    mds = sorted(SRC.glob("*.md"))
    if args.only:
        mds = [m for m in mds if m.stem in args.only]
    for md in mds:
        rid = md.stem
        print(f"== {rid}", flush=True)
        d = out_root / rid
        d.mkdir(exist_ok=True)
        fulltext = md.read_text()
        try:
            p1 = call_and_save(d, "pass1", fill(tpl1, strategy_types=st_text, families=fams, methods=meths,
                                                report_id=rid, fulltext=fulltext), s1, args)
            cards, mp = prepare_cards(rid, canaries, args.seed)
            mapping[rid] = mp
            mapping_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2))
            notes = json.dumps({"reference_card": p1["reference_card"], "must_capture": p1["must_capture"]},
                               ensure_ascii=False, indent=2)
            dump = lambda c: json.dumps(c, ensure_ascii=False, indent=2)
            call_and_save(d, "pass2", fill(tpl2, families=fams, methods=meths, report_id=rid,
                                           fulltext=fulltext, pass1_notes=notes,
                                           card_a=dump(cards["A"]), card_b=dump(cards["B"]),
                                           card_c=dump(cards["C"])), s2, args)
        except Exception as e:  # 單篇失敗不中斷整批
            print(f"  ERROR {type(e).__name__}: {e}", flush=True)
    if canaries:
        canaries_path.write_text(json.dumps(canaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
