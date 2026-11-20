"""量產監控：從 manifest 與埋雷紀錄算出各項比例，超過警戒線就回報警報（PLAN.md「監控指標」）。

run.py 每做完一篇就重算一次；有警報就不再開始新的篇（已經在跑的照常做完），整批停下等人看。
看過、處理過（例如修了 prompt）之後要繼續，加 `--ignore-alarms`。

比例都是整個批次累計；樣本不到 MIN_N 不判，免得前幾篇剛好出錯就誤停。
"""
import json

import batch as B

MIN_N = 20          # 寫卡、檢查類的比例至少要這麼多篇才判
MIN_CANARY = 10     # 埋雷至少要這麼多個才判

# (名稱, 怎麼算, 警戒, 方向)；方向 "<" 表示低於警戒就報警
THRESHOLDS = [
    ("程式檢查第一次通過率", "gate_first_pass", 0.85, "<"),
    ("檢查員退回率", "check1_back", 0.25, ">"),
    ("升級率", "escalate", 0.05, ">"),
    ("寫卡或檢查失敗率", "error", 0.10, ">"),
    ("埋雷漏抓率", "canary_miss", 0.10, ">"),
]
WHAT_TO_DO = {
    "gate_first_pass": "寫卡的引文或數字常對不上原文：看 `run.py report` 的程式檢查問題，修寫卡 prompt",
    "check1_back": "寫卡品質變差：看檢查員退回的理由（`run.py review`），修寫卡 prompt 或換寫卡模型",
    "escalate": "重寫也救不回來的卡太多：先看待升級清單，修 prompt 再繼續",
    "error": "API 常失敗：看 `run.py report` 的錯誤原因（餘額、格式、服務異常）",
    "canary_miss": "檢查員漏抓埋雷：打開 `eval/<batch>/_canary/` 看漏掉的是哪一類，考慮換檢查模型",
}


def stats(manifest, batch):
    """回傳 {key: (分子, 分母)}。"""
    recs = [v["card"] for v in manifest.values() if v["batch"] == batch and v.get("card")]
    gate = [r for r in recs if r.get("gate")]
    checked = [r for r in recs if "check1" in r]
    finished = [r for r in recs if r["state"] in ("passed", "escalate", "error")]
    out = {
        "gate_first_pass": (sum(r["gate"][0]["problems"] == 0 for r in gate), len(gate)),
        "check1_back": (sum(r["check1"] != "pass" for r in checked), len(checked)),
        "escalate": (sum(r["state"] == "escalate" for r in checked), len(checked)),
        "error": (sum(r["state"] == "error" for r in finished), len(finished)),
    }
    cn = B.ROOT / "eval" / batch / "_canary" / "_canaries.json"
    cs = [c for c in json.loads(cn.read_text()) if "caught" in c] if cn.exists() else []
    out["canary_miss"] = (sum(not c["caught"] for c in cs), len(cs))
    return out


def alarms(manifest, batch):
    """回傳超標的項目 [(名稱, 說明, 建議)]；空清單 = 正常。"""
    st, found = stats(manifest, batch), []
    for name, key, limit, way in THRESHOLDS:
        k, n = st[key]
        if n < (MIN_CANARY if key == "canary_miss" else MIN_N):
            continue
        rate = k / n
        if (rate < limit) if way == "<" else (rate > limit):
            found.append((name, f"{k}/{n}（{rate:.0%}，警戒 {'低於' if way == '<' else '高於'} {limit:.0%}）",
                          WHAT_TO_DO[key]))
    return found


def summary(manifest, batch):
    """report 用：每一項的數字與狀態。"""
    st, lines = stats(manifest, batch), []
    bad = {a[0] for a in alarms(manifest, batch)}
    for name, key, limit, way in THRESHOLDS:
        k, n = st[key]
        need = MIN_CANARY if key == "canary_miss" else MIN_N
        state = "**警報**" if name in bad else ("樣本不足" if n < need else "正常")
        lines.append(f"  {name}：{k}/{n}" + (f"（{k / n:.0%}）" if n else "") +
                     f"；警戒 {'<' if way == '<' else '>'} {limit:.0%}；{state}")
    return lines
