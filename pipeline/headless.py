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

用法：
  python headless.py ping writer|checker    # 送一題極小的題目，確認連線、模型名稱、回傳格式（用一點套餐額度）
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
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


class QuotaExhausted(RuntimeError):
    """Agent Plan 的 5 小時、每週或每月額度用完。"""


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
    t0 = time.time()
    code, out, err, timed_out = _spawn(command(role, schema, effort), prompt, child_env(role), timeout)
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
    _log({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "role": role, "model": model_of(role), "seconds": secs,
          "ok": result is not None, "num_turns": raw.get("num_turns"), "usage": usage,
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


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "ping" or sys.argv[2] not in ROLES:
        raise SystemExit(__doc__)
    cmd_ping(sys.argv[2])
