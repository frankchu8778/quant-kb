"""DeepSeek 的尖峰時段閘門。

規則：尖峰時段呼叫 DeepSeek 前要先問使用者（2026-11-17 定為「不准呼叫，除非允許」，2026-11-20 改成「先詢問」）。
尖峰＝北京時間週一至週五（不含中國法定節假日）9:00–12:00、14:00–18:00；其餘時間是離峰（半價）。

- 在終端機互動執行時，第一次碰到尖峰就問一次「要用原價執行嗎？」，同一次執行內沿用答案。
- 背景執行（沒有終端機）沒辦法問，就照舊不呼叫；要先問過使用者，同意了再加 --allow-peak 重跑。

- 法定節假日列在 cn_holidays.txt。沒列到的平日一律當成尖峰，所以清單不完整只會多擋、不會誤放。
- 尖峰開始前 MARGIN_MIN 分鐘就不再送新請求：一次呼叫要一到三分鐘，避免跨進尖峰。
- 放行：互動時回答 y，或該次指令加 --allow-peak（它會設環境變數 DEEPSEEK_ALLOW_PEAK=1）。只在使用者明說可以時才加。
- 查餘額不是模型呼叫、不計費，不受這個限制。
"""
import os
import sys
import threading
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
PEAK_WINDOWS = [(time(9, 0), time(12, 0)), (time(14, 0), time(18, 0))]
MARGIN_MIN = 5
HOLIDAY_FILE = Path(__file__).parent / "cn_holidays.txt"
ALLOW_ENV = "DEEPSEEK_ALLOW_PEAK"


class PeakHours(RuntimeError):
    """尖峰時段、且沒有得到允許。"""


def holidays():
    if not HOLIDAY_FILE.exists():
        return set()
    return {line.split("#")[0].strip() for line in HOLIDAY_FILE.read_text().splitlines()} - {""}


def _now(now=None):
    return (now or datetime.now(TZ)).astimezone(TZ)


def _windows(day):
    """某一天的禁止時段（已含提前量）；週末與法定節假日沒有。"""
    if day.weekday() >= 5 or day.isoformat() in holidays():
        return []
    return [(datetime.combine(day, a, TZ) - timedelta(minutes=MARGIN_MIN), datetime.combine(day, b, TZ))
            for a, b in PEAK_WINDOWS]


def is_peak(now=None):
    now = _now(now)
    return any(a <= now < b for a, b in _windows(now.date()))


def next_offpeak(now=None):
    """現在所在的禁止時段什麼時候結束；現在就是離峰則回傳現在。"""
    now = _now(now)
    for a, b in _windows(now.date()):
        if a <= now < b:
            return b
    return now


def allowed():
    return os.environ.get(ALLOW_ENV) == "1"


_ask_lock = threading.Lock()
_answer = None   # 這次執行問過的答案：True / False；None＝還沒問


def interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask(now=None):
    """尖峰時問使用者要不要用原價執行；同一次執行只問一次。背景執行沒辦法問，回傳 False。"""
    global _answer
    with _ask_lock:
        if _answer is None:
            if not interactive():
                return False
            try:
                reply = input(f"現在是 DeepSeek 尖峰時段（北京時間 {_now(now):%m-%d %a %H:%M}，原價，"
                              f"{next_offpeak(now):%H:%M} 起離峰）。要用原價執行嗎？[y/N] ")
            except EOFError:
                reply = ""
            _answer = reply.strip().lower() in ("y", "yes", "是", "要")
            if _answer:
                os.environ[ALLOW_ENV] = "1"
            else:
                print("不在尖峰執行：DeepSeek 的步驟等離峰再做", flush=True)
        return _answer


def is_deepseek(model):
    return str(model).startswith("deepseek")


def blocked(model, now=None):
    return is_deepseek(model) and is_peak(now) and not allowed() and not ask(now)


def message(now=None):
    return (f"現在是 DeepSeek 尖峰時段（北京時間 {_now(now):%m-%d %a %H:%M}），不呼叫；"
            f"{next_offpeak(now):%H:%M} 起是離峰。要在尖峰跑：在終端機直接執行會先詢問；"
            f"背景執行請先問使用者，同意後在指令加 --allow-peak")


def guard(model, now=None):
    """所有 DeepSeek 模型呼叫的最後一道閘門。"""
    if blocked(model, now):
        raise PeakHours(message(now))


def allow_from_flag(flag):
    """指令的 --allow-peak：只影響這一次執行。"""
    if flag:
        os.environ[ALLOW_ENV] = "1"
        if is_peak():
            print("注意：已用 --allow-peak 放行，這次會在尖峰時段以原價呼叫 DeepSeek", flush=True)
