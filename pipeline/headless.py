"""用 Claude Code 的無頭模式（`claude -p`）接火山方舟 Agent Plan，呼叫寫卡（Kimi）與檢查（DeepSeek）模型。

為什麼不用 API：Agent Plan 的額度只能在 AI 工具裡用，直接打 API 可能被判違規。所以由程式啟動
Claude Code，讓它把請求送到火山的端點。

每一次呼叫都是：一篇研報的一個步驟、一個新程序、一個空的工作資料夾，結束就丟掉。
agent 只負責回傳一份 JSON，不能讀寫檔、不能執行指令、看不到其他篇；
落檔、程式檢查、狀態轉換都由 run.py 做，agent 的回報不算數。

隔離措施（docs/PIPELINE.md「Agent Plan 路線」有對照表）：
  --tools ""＋設定檔 deny 全部工具      不能做任務以外的事
  --bare、專用 CLAUDE_CONFIG_DIR、空 cwd  讀不到專案與個人的 CLAUDE.md、記憶、hooks、外掛、MCP
  --system-prompt-file agent_plan/system.md  固定的系統提示
  --no-session-persistence               不存對話，下一篇接不回來
  環境變數白名單                          子程序拿不到 Kimi／DeepSeek／MiniMax 的 API 金鑰
  程序群組逾時                            卡住就整組砍掉
  回來後再驗 schema、檢查有沒有動用工具      不信任它的輸出

設定：
  pipeline/agent_plan/<role>.settings.json  deny 清單、關 hooks 與遙測（進 git，沒有金鑰）
  .env：ARK_AGENT_API_KEY、ARK_AGENT_BASE_URL         從火山控制台複製（Agent Plan 的，不是 Coding Plan 的）
        ARK_KIMI_MODEL_NAME、ARK_DEEPSEEK_MODEL_NAME   寫卡、檢查用的模型名稱（以火山控制台上的為準）

控速（不讓額度用超過）：每次呼叫前，用 logs/headless_calls.log 估算最近 5 小時、7 天、31 天用掉的點數，
加上這次呼叫的估計會超過上限的 PACE_SHARE 就先不送，丟 PaceWait（run.py 當成 waiting_quota，等到估計可以送的時間）。
點數是估的：CLI 看不到火山的點數，用 token 數換算（TOKENS_PER_POINT）。在這個專案以外用掉的點數算不到。

用法：
  python headless.py ping writer|checker    # 送一題極小的題目，確認連線、模型名稱、回傳格式（用一點套餐額度）
  python headless.py usage                  # 估算目前各時間窗用掉的點數（不送請求）
  python headless.py console 5h|7d|31d <點數>  # 記下火山控制台的讀數，控速改從讀數起算（控制台是固定時間重置，滾動估計會偏高）
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import jsonschema

from common import KB, PIPELINE, load_env

AGENT_DIR = PIPELINE / "agent_plan"
CONFIG_ROOT = AGENT_DIR / ".config"          # Claude Code 的設定資料夾（每個角色一個；不進 git）
LOG = PIPELINE / "logs" / "headless_calls.log"   # 每次呼叫一行 JSON：時間、角色、秒數、用量、錯誤；不含提示與金鑰
ROLES = ("writer", "checker")
TIMEOUT = 1200   # 秒；一次寫卡通常一到三分鐘

# 第一次連線測試（ping）後依實測調整：
BARE = True            # --bare 只認 ANTHROPIC_API_KEY；火山若只收 Bearer token，改 False 並把 AUTH_ENV 改成 ANTHROPIC_AUTH_TOKEN
AUTH_ENV = "ANTHROPIC_API_KEY"
PASS_EFFORT = True     # 火山的模型若不接受推理強度參數，改 False
# 額度用完：等額度恢復，不算失敗（第一次碰到時把火山的錯誤訊息補進來）
QUOTA_HINTS = ("quota", "额度", "限额", "usage limit", "exceeded your", "insufficient_quota", "afp")
ENV_KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")


# Small 檔的上限（秒數, 點數, 名稱）。週、月用滾動時間窗：滾動 7 天不超過，任何一個日曆週也不會超過；月取 31 天
PLAN_LIMITS = [(5 * 3600, 2000, "5 小時"), (7 * 86400, 7000, "7 天"), (31 * 86400, 20000, "31 天")]
# 控制台讀數：火山控制台的額度是固定時間重置，滾動估計會偏高；使用者看了控制台可以用
# `python headless.py console 5h 172` 記下讀數，之後這個時間窗改用「讀數＋讀數之後的估計」，讀數滑出時間窗後回到滾動估計
CONSOLE = PIPELINE / "logs" / "console_readings.jsonl"
RECHECK = 900   # 控速等待最多幾秒就重算一次
WINDOW_KEYS = {"5h": "5 小時", "7d": "7 天", "31d": "31 天"}
PACE_SHARE = 0.8         # 估計用量最多到上限的八成，留餘地給估計誤差與專案外的使用
# 實測（2026-11-23 三篇對照）：17 次成功呼叫共 388k token，控制台扣 76 點 ≈ 5,100 token／點。取 4,000 偏保守。
# 之後每次到控制台看到點數，可以重算這個值（`python headless.py usage` 會印出估計值方便對照）
TOKENS_PER_POINT = 4000
DEFAULT_NEXT = {"writer": 20, "checker": 15}   # 還沒有紀錄時，下一次呼叫的估計點數


class QuotaExhausted(RuntimeError):
    """Agent Plan 的 5 小時、每週或每月額度用完。"""
    until = None


class PaceWait(QuotaExhausted):
    """控速：照估計再送會超過上限的 PACE_SHARE。until＝估計可以再送的時間（epoch 秒）。"""
    def __init__(self, msg, until):
        super().__init__(msg)
        self.until = until


def points_of(usage):
    u = usage or {}
    toks = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                       "cache_creation_input_tokens", "output_tokens"))
    return toks / TOKENS_PER_POINT


def ledger():
    """[(epoch 秒, 角色, 估計點數, 成功與否)]，從呼叫紀錄讀。逾時被砍的呼叫記的是送出前的預估。"""
    out = []
    if not LOG.exists():
        return out
    for line in LOG.read_text().splitlines():
        try:
            e = json.loads(line)
            ts = e.get("ts") or time.mktime(time.strptime(e["time"], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, KeyError):
            continue
        pts = e["afp_est"] if "afp_est" in e else points_of(e.get("usage"))
        out.append((ts, e.get("role"), pts, e.get("ok")))
    return out


def next_cost(role, rows=None):
    """下一次呼叫的估計：這個角色最近 31 天內、最近 10 次成功呼叫裡最大的一次。"""
    since = time.time() - PLAN_LIMITS[-1][0]
    past = [p for ts, r, p, ok in (rows if rows is not None else ledger()) if r == role and ok and ts > since][-10:]
    return max(past) if past else DEFAULT_NEXT[role]


_pace_lock = threading.Lock()
_inflight = {}   # 已放行、還沒回來的呼叫：id → (送出時間, 預估點數)


def readings():
    out = []
    if CONSOLE.exists():
        for line in CONSOLE.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def window_basis(rows, now, secs, name, rds=None):
    """(讀數點數, 讀數時間或 None, 要逐筆加上的呼叫)：有還在時間窗內的控制台讀數就從讀數起算。"""
    rds = [r for r in (readings() if rds is None else rds) if r.get("window") == name and r["at"] > now - secs]
    if rds:
        rd = max(rds, key=lambda r: r["at"])
        return rd["points"], rd["at"], [r for r in rows if r[0] > rd["at"]]
    return 0, None, [r for r in rows if r[0] > now - secs]


def windows(rows, now):
    """各時間窗的估計用量（含還沒回來的呼叫）。"""
    pending, rds, out = list(_inflight.values()), readings(), []
    for secs, limit, name in PLAN_LIMITS:
        base, _, wrows = window_basis(rows, now, secs, name, rds)
        out.append((secs, limit, name, base + sum(p for _, _, p, _ in wrows) + sum(p for _, p in pending)))
    return out


def reserve(role, now=None):
    """放行就回傳一個 id（呼叫完要 release）；會超過就丟 PaceWait。"""
    with _pace_lock:
        now = now or time.time()
        rows = ledger()
        need = next_cost(role, rows)
        for secs, limit, name, used in windows(rows, now):
            cap = limit * PACE_SHARE
            if used + need <= cap:
                continue
            # 從最舊的開始等它們滑出時間窗，直到放得下
            until, over = now, used + need - cap
            base, rd_at, wrows = window_basis(rows, now, secs, name)
            if rd_at is not None:   # 讀數最舊，先等它滑出時間窗
                over -= base
                until = rd_at + secs
            for ts, _, p, _ in (sorted(wrows) if over > 0 else []):
                over -= p
                until = ts + secs
                if over <= 0:
                    break
            if over > 0:   # 只剩還沒回來的呼叫：等一輪再看
                until = now + 600
            # 最多等 RECHECK 秒就重算（只在本機算，不用額度）：中途記了新的控制台讀數才會馬上生效
            until = min(until, now + RECHECK)
            raise PaceWait(f"控速：最近 {name} 估計已用 {used:.0f} 點，這次估 {need:.0f} 點，會超過上限 {limit} 的 "
                           f"{PACE_SHARE:.0%}（{cap:.0f}）；{time.strftime('%m-%d %H:%M', time.localtime(until))} 後再送", until)
        rid = object()
        _inflight[rid] = (now, need)
        return rid


def release(rid):
    with _pace_lock:
        return _inflight.pop(rid, (0, 0))[1]


def claude_bin():
    return os.environ.get("QUANT_KB_CLAUDE_BIN") or shutil.which("claude") or "claude"


def settings_path(role):
    if role not in ROLES:
        raise ValueError(f"未知角色 {role}（只有 {ROLES}）")
    return AGENT_DIR / f"{role}.settings.json"


MODEL_ENV = {"writer": "ARK_KIMI_MODEL_NAME", "checker": "ARK_DEEPSEEK_MODEL_NAME"}
MODEL_ALIASES = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                 "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")


def model_of(role):
    settings_path(role)   # 檢查角色名稱
    name = load_env().get(MODEL_ENV[role])
    if not name:
        raise RuntimeError(f".env 缺 {MODEL_ENV[role]}")
    return name


def child_env(role):
    """子程序的環境變數：白名單，不繼承其他金鑰。"""
    env = load_env()
    key, url = env.get("ARK_AGENT_API_KEY"), env.get("ARK_AGENT_BASE_URL")
    if not key or not url:
        raise RuntimeError(".env 缺 ARK_AGENT_API_KEY 或 ARK_AGENT_BASE_URL（從火山控制台的 Agent Plan 頁面複製）")
    cfg = CONFIG_ROOT / role
    cfg.mkdir(parents=True, exist_ok=True)
    out = {k: os.environ[k] for k in ENV_KEEP if k in os.environ}
    out.update(HOME=str(cfg), CLAUDE_CONFIG_DIR=str(cfg), ANTHROPIC_BASE_URL=url, **{AUTH_ENV: key})
    # 所有模型別名都指向同一個模型，Claude Code 的背景小任務才不會去叫火山上不存在的模型
    out.update({k: model_of(role) for k in MODEL_ALIASES})
    return out


def explicit_types(schema):
    """火山的端點要求每個欄位都明寫 type：只有 enum 的欄位（例如標籤）不補，整個請求會回 400
    「A parameter specified in the request is not valid」（2026-11-23 實測）。只用在送出的 schema；
    回來後仍用原本的 schema 驗證。"""
    from kimi_batch import moonshot_schema
    return moonshot_schema(schema)


def command(role, schema, effort):
    cmd = [claude_bin(), "-p", "--model", model_of(role), "--settings", str(settings_path(role)),
           "--system-prompt-file", str(AGENT_DIR / "system.md"), "--tools", "", "--strict-mcp-config",
           "--no-session-persistence", "--output-format", "json",
           "--json-schema", json.dumps(explicit_types(schema), ensure_ascii=False)]
    if BARE:
        cmd.insert(2, "--bare")
    else:
        cmd += ["--setting-sources", ""]
    if PASS_EFFORT and effort:
        cmd += ["--effort", effort]
    return cmd


def _log(entry):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _spawn(cmd, prompt, env, timeout):
    """在空的暫存資料夾裡起新程序（自己一個程序群組），逾時整組砍掉。回傳 (returncode, stdout, stderr, 逾時與否)。"""
    with tempfile.TemporaryDirectory(prefix="kb-headless-") as cwd:
        env = {**env, "TMPDIR": cwd}
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            out, err = p.communicate(prompt, timeout=timeout)
            return p.returncode, out, err, False
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out, err = p.communicate()
            return p.returncode, out, err, True


def _structured(res):
    """取出結構化結果；沒有就試著從文字結果解析 JSON。"""
    out = res.get("structured_output")
    if out is None and isinstance(res.get("result"), str):
        from common import extract_json
        try:
            out = extract_json(res["result"])
        except Exception:
            out = None
    return out


def run(role, prompt, schema, effort="high", timeout=TIMEOUT):
    """一次呼叫。回傳 (res, tail, secs)，格式同 judge_fable.run_deepseek：
    res["structured_output"] 是通過 schema 驗證的結果，失敗時是 None；額度用完丟 QuotaExhausted。"""
    slot = reserve(role)   # 控速：會超過就丟 PaceWait，不送
    t0 = time.time()
    try:
        code, out, err, timed_out = _spawn(command(role, schema, effort), prompt, child_env(role), timeout)
    finally:
        reserved = release(slot)
    secs = round(time.time() - t0, 1)
    try:
        raw = json.loads(out)
    except json.JSONDecodeError:
        raw = {}
    problems = []
    if timed_out:
        problems.append(f"逾時 {timeout} 秒，已砍掉程序")
    if not raw:
        problems.append(f"輸出不是 JSON（結束碼 {code}）")
    if raw.get("is_error"):
        problems.append(f"Claude Code 回報錯誤：{str(raw.get('result') or raw.get('subtype'))[:300]}")
    if raw.get("permission_denials"):
        problems.append(f"嘗試動用工具 {len(raw['permission_denials'])} 次（不該發生），結果不採用")
    result = _structured(raw) if raw and not problems else None
    if result is not None:
        errs = sorted(jsonschema.Draft202012Validator(schema).iter_errors(result), key=lambda e: list(e.absolute_path))
        if errs:
            problems.append("不符 schema：" + "；".join(f"{list(e.absolute_path)}: {e.message[:120]}" for e in errs[:5]))
            result = None
    text = (str(raw.get("result") or "") + "\n" + (err or "")).lower()
    tail = "\n".join(problems) + ("\n" + (err or "")[-1500:] if err else "")
    usage = raw.get("usage") or {}
    # 估計點數：有用量就照用量換算；逾時被砍、沒有用量的，記送出前的預估（寧可多算）
    est = points_of(usage) if usage else (reserved if timed_out else 0.0)
    _log({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "ts": round(t0, 1), "role": role, "model": model_of(role),
          "seconds": secs, "ok": result is not None, "num_turns": raw.get("num_turns"), "usage": usage,
          "afp_est": round(est, 2),
          "problems": problems, "stderr_tail": (err or "")[-300:]})
    if result is None and any(h in text for h in QUOTA_HINTS):
        raise QuotaExhausted(tail[:300])
    res = {"structured_output": result, "usage": {"input_tokens": (usage.get("input_tokens") or 0)
                                                  + (usage.get("cache_read_input_tokens") or 0)
                                                  + (usage.get("cache_creation_input_tokens") or 0),
                                                  "output_tokens": usage.get("output_tokens") or 0},
           "is_error": result is None, "num_turns": raw.get("num_turns"), "model": model_of(role), "via": "agent-plan"}
    return res, tail, secs


def cmd_ping(role):
    schema = {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"],
              "additionalProperties": False}
    print(f"角色 {role}，模型 {model_of(role)}，--bare={BARE}，認證變數 {AUTH_ENV}，effort={'high' if PASS_EFFORT else '不傳'}")
    try:
        res, tail, secs = run(role, "1+1 等于几？把答案填进 answer。", schema, timeout=300)
    except QuotaExhausted as e:
        print(f"額度用完：{e}")
        return
    print(f"{secs} 秒；結果 {res['structured_output']}；num_turns {res['num_turns']}；用量 {res['usage']}")
    if tail:
        print("問題：\n" + tail)
    print(f"完整紀錄（不含金鑰）：{LOG}")


def cmd_usage():
    rows, now = ledger(), time.time()
    print(f"估計值：{TOKENS_PER_POINT} token／點；控速到上限的 {PACE_SHARE:.0%}；專案外的用量算不到")
    for secs, limit, name, used in windows(rows, now):
        base, rd_at, _ = window_basis(rows, now, secs, name)
        src = f"；從控制台讀數 {base:.0f} 點（{time.strftime('%m-%d %H:%M', time.localtime(rd_at))}）起算" if rd_at else ""
        print(f"  最近 {name}：約 {used:.0f} 點／上限 {limit}（控速線 {limit * PACE_SHARE:.0f}）{src}")
    print(f"  下一次估計：寫卡 {next_cost('writer', rows):.0f} 點、檢查 {next_cost('checker', rows):.0f} 點")


if __name__ == "__main__":
    if sys.argv[1:] == ["usage"]:
        cmd_usage()
    elif len(sys.argv) == 4 and sys.argv[1] == "console" and sys.argv[2] in WINDOW_KEYS:
        CONSOLE.parent.mkdir(parents=True, exist_ok=True)
        with CONSOLE.open("a") as f:
            f.write(json.dumps({"window": WINDOW_KEYS[sys.argv[2]], "points": float(sys.argv[3]), "at": time.time()},
                               ensure_ascii=False) + "\n")
        cmd_usage()
    elif len(sys.argv) == 3 and sys.argv[1] == "ping" and sys.argv[2] in ROLES:
        cmd_ping(sys.argv[2])
    else:
        raise SystemExit(__doc__)
