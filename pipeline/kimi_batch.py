"""Kimi（Moonshot）的 OpenAI 格式介面：batch 任務、即時呼叫、餘額查詢。只管 HTTP，不管流程。

batch 的流程（官方文件：platform.kimi.com/docs/guide/use-batch-api）：
  1. 把請求寫成 JSONL（每行一個任務），用 /v1/files 上傳，purpose="batch"
  2. /v1/batches 建立任務，endpoint 固定 /v1/chat/completions，completion_window 最短 12h
  3. 查 /v1/batches/<id>，status 變成 completed 後下載 output_file_id（與 error_file_id）
batch 比即時便宜 40%，但只支援 OpenAI 格式；Anthropic 相容端點沒有 batch。
"""
import json

import httpx
import jsonschema

from common import extract_json, load_env
from judge_fable import SUBMIT_HINT

BASE = "https://api.moonshot.cn/v1"
MODEL = "kimi-k3.1-pro"
MAX_TOKENS = 32000   # 輸出含推理，試產實測每張卡約 1.1–1.3 萬 token
OPEN_STATUS = {"validating", "in_progress", "finalizing", "cancelling"}


def _client(timeout=120):
    key = {k.strip(): v.strip() for k, v in load_env().items()}["KIMI_API_KEY"]
    return httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {key}"}, timeout=timeout)


def _json(r):
    if r.status_code >= 400:
        raise RuntimeError(f"Kimi HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


def moonshot_schema(node):
    """Kimi 的工具參數要求每個欄位都明寫 type；只有 enum 的欄位補上型別。回傳新物件，不改原 schema。"""
    if isinstance(node, list):
        return [moonshot_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {k: moonshot_schema(v) for k, v in node.items()}
    if "enum" in out and "type" not in out:
        out["type"] = "integer" if all(isinstance(x, int) and not isinstance(x, bool) for x in out["enum"]) else "string"
    return out


def chat_body(prompt, schema):
    """一個任務的請求內容。即時與 batch 用同一個 body，試打即時就能驗證 batch 的格式。

    不設 temperature / top_p 等：batch 不允許修改。
    """
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt + SUBMIT_HINT}],
        "tools": [{"type": "function",
                   "function": {"name": "submit", "description": "提交结果", "parameters": moonshot_schema(schema)}}],
    }


def parse_response(body, schema):
    """從 chat completions 的回應取出結果。回傳 (結果或 None, 錯誤訊息, usage)。

    優先取工具呼叫的參數；模型沒有呼叫工具時，退回解析正文裡的 JSON。兩者都要通過 schema 驗證。
    """
    u = body.get("usage") or {}
    usage = {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}
    try:
        msg = body["choices"][0]["message"]
        finish = body["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return None, f"回應沒有 choices：{json.dumps(body, ensure_ascii=False)[:300]}", usage
    out, source = None, None
    for tc in msg.get("tool_calls") or []:
        if (tc.get("function") or {}).get("name") == "submit":
            try:
                out, source = json.loads(tc["function"]["arguments"]), "tool_call"
            except json.JSONDecodeError as e:
                return None, f"工具參數不是合法 JSON（finish_reason={finish}）：{e}", usage
            break
    if out is None and msg.get("content"):
        try:
            out, source = extract_json(msg["content"]), "content"
        except Exception:
            pass
    if out is None:
        return None, f"沒有工具呼叫，正文也解析不出 JSON（finish_reason={finish}）", usage
    errs = sorted(jsonschema.Draft202012Validator(schema).iter_errors(out), key=lambda e: list(e.absolute_path))
    if errs:
        return None, f"不符合 schema（{len(errs)} 處，來自 {source}）：" + "；".join(
            f"{list(e.absolute_path)}: {e.message[:120]}" for e in errs[:5]), usage
    return out, None, usage


def chat_realtime(body, timeout=120):
    """用同一個 body 打即時端點（原價），以串流接收，回傳和非串流相同形狀的回應。

    一定要串流：寫一張卡要好幾分鐘，非串流的連線等不到回應就會斷，而伺服器照樣跑完、照樣扣錢。
    timeout 是「多久沒有新資料就放棄」，不是整個請求的上限。
    """
    msg = {"role": "assistant", "content": "", "reasoning_content": "", "tool_calls": []}
    finish, usage = None, {}
    with _client(httpx.Timeout(timeout, read=timeout)) as c:
        with c.stream("POST", "/chat/completions",
                      json={**body, "stream": True, "stream_options": {"include_usage": True}}) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"Kimi HTTP {r.status_code}: {r.read().decode()[:400]}")
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                usage = chunk.get("usage") or usage
                for ch in chunk.get("choices") or []:
                    usage = ch.get("usage") or usage
                    finish = ch.get("finish_reason") or finish
                    d = ch.get("delta") or {}
                    msg["content"] += d.get("content") or ""
                    msg["reasoning_content"] += d.get("reasoning_content") or ""
                    for tc in d.get("tool_calls") or []:
                        i = tc.get("index", 0)
                        while len(msg["tool_calls"]) <= i:
                            msg["tool_calls"].append({"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                        slot = msg["tool_calls"][i]
                        slot["id"] = tc.get("id") or slot["id"]
                        f = tc.get("function") or {}
                        slot["function"]["name"] += f.get("name") or ""
                        slot["function"]["arguments"] += f.get("arguments") or ""
    return {"choices": [{"message": msg, "finish_reason": finish}], "usage": usage}


def upload(path):
    with _client(300) as c, open(path, "rb") as f:
        return _json(c.post("/files", data={"purpose": "batch"}, files={"file": (path.name, f, "application/jsonl")}))["id"]


def create(file_id, window="12h", metadata=None):
    payload = {"input_file_id": file_id, "endpoint": "/v1/chat/completions", "completion_window": window}
    if metadata:
        payload["metadata"] = metadata
    with _client() as c:
        return _json(c.post("/batches", json=payload))


def retrieve(batch_id):
    with _client() as c:
        return _json(c.get(f"/batches/{batch_id}"))


def download(file_id):
    with _client(300) as c:
        r = c.get(f"/files/{file_id}/content")
        if r.status_code >= 400:
            raise RuntimeError(f"Kimi HTTP {r.status_code}: {r.text[:400]}")
        return r.text


def cancel(batch_id):
    with _client() as c:
        return _json(c.post(f"/batches/{batch_id}/cancel"))


def balance():
    """可用餘額（人民幣）。"""
    with _client(30) as c:
        return float(_json(c.get("/users/me/balance"))["data"]["available_balance"])


def deepseek_balance():
    key = {k.strip(): v.strip() for k, v in load_env().items()}["DEEPSEEK_API_KEY"]
    with httpx.Client(timeout=30) as c:
        r = c.get("https://api.deepseek.com/user/balance", headers={"Authorization": f"Bearer {key}"})
        if r.status_code >= 400:
            raise RuntimeError(f"DeepSeek HTTP {r.status_code}: {r.text[:300]}")
        for b in r.json()["balance_infos"]:
            if b["currency"] == "CNY":
                return float(b["total_balance"])
    raise RuntimeError("DeepSeek 餘額回應裡沒有 CNY")
