"""因子库 API 桥接层 — 通过 subprocess 调用系统 Python 执行因子操作

架构：
- 过滤快照：每天独立生成一次（4 个过滤的全量剔除名单），缓存到 data/filter_cache/{date}.json
- 因子执行：只读取快照中的剔除名单传给脚本，脚本不再自己跑过滤
- 结果缓存：按 (factor, date, topN, filters) 哈希缓存到 data/factor_cache/
"""
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

_SYS_PYTHON = "/opt/miniconda3/bin/python3"
_ASTOCK_PATH = "/Users/shinechow/Documents/code/a-stock-data"

_CACHE_DIR = Path(__file__).resolve().parent / "factor_cache"
_FILTER_DIR = Path(__file__).resolve().parent / "filter_cache"
_DEFAULT_FILTERS = ("st", "new_stock", "liquidity")

_LIST_SCRIPT = r"""
import sys, json
sys.path.insert(0, '{astock}')
from quant.factor_lib import FACTORS, FILTERS
result = {{
    "factors": [{{"name": k, "desc": v["desc"]}} for k, v in FACTORS.items()],
    "filters": [{{"name": k, "desc": v["desc"]}} for k, v in FILTERS.items()],
}}
print("__JSON_START__")
print(json.dumps(result, ensure_ascii=False))
"""

_FILTER_SNAPSHOT_SCRIPT = r"""
import sys, json
sys.path.insert(0, '{astock}')
from quant.factor_lib import FILTERS, QuantDB

date = sys.argv[1] if len(sys.argv) > 1 else None
db = QuantDB()
if not date:
    conn = db._connect()
    row = conn.execute("SELECT MAX(date) FROM daily_bars WHERE volume > 0").fetchone()
    conn.close()
    date = row[0] if row and row[0] else __import__('time').strftime("%Y-%m-%d")

conn = db._connect()
# 股票名称
name_map = {{row[0]: row[1].replace(chr(0), '') for row in conn.execute("SELECT code, name FROM stocks")}}
# 当日行情
bars = {{row[0]: {{"close": row[1], "amount": row[2]}}
        for row in conn.execute("SELECT code, close, amount FROM daily_bars WHERE date=? AND close>0", (date,))}}
# 估值数据
val_rows = conn.execute("SELECT code, change_pct, mcap, turnover_pct FROM daily_valuation WHERE date=?", (date,)).fetchall()
for row in val_rows:
    if row[0] in bars:
        bars[row[0]]["change_pct"] = row[1]
        bars[row[0]]["mcap"] = row[2]
        bars[row[0]]["turnover_pct"] = row[3]
# 近30日均成交额（用于流动性过滤判断）
from datetime import timedelta
start_30d = (__import__('datetime').datetime.strptime(date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
avg_amounts = {{row[0]: row[1] for row in conn.execute(
    "SELECT code, AVG(amount) FROM daily_bars WHERE date BETWEEN ? AND ? AND amount>0 GROUP BY code",
    (start_30d, date),
)}}
for code in bars:
    bars[code]["amount_avg_30d"] = avg_amounts.get(code)
conn.close()

filters = {{}}
for fname, meta in FILTERS.items():
    try:
        codes = sorted(meta["func"](db, date))
    except Exception:
        codes = []
    stocks = []
    for code in codes:
        info = bars.get(code, {{}})
        stocks.append({{
            "code": code,
            "name": name_map.get(code, ""),
            "close": info.get("close"),
            "change_pct": info.get("change_pct"),
            "mcap": info.get("mcap"),
            "turnover_pct": info.get("turnover_pct"),
            "amount": info.get("amount"),
            "amount_avg_30d": info.get("amount_avg_30d"),
        }})
    filters[fname] = {{"desc": meta["desc"], "stocks": stocks}}

print("__JSON_START__")
print(json.dumps({{"date": date, "filters": filters}}, ensure_ascii=False, default=str))
"""

_RUN_SCRIPT = r"""
import sys, json, time, math
sys.path.insert(0, '{astock}')
from quant.factor_lib import rank_stocks, QuantDB

def safe_val(v):
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
    return v

args = json.loads(sys.stdin.read())
t0 = time.time()
db = QuantDB()
date = args.get("date") or None
filters = args.get("filters")
kwargs = {{"factor_name": args["factor_name"], "top_n": args.get("top_n", 20)}}
if date:
    kwargs["date"] = date
if filters is not None:
    kwargs["filters"] = tuple(filters)
# filters=None → rank_stocks 用默认值；filters=[] → 不过滤
df = rank_stocks(db, **kwargs)
elapsed = round(time.time() - t0, 2)

if df.empty:
    result = {{"rows": [], "columns": [], "elapsed": elapsed, "count": 0}}
else:
    rows = []
    for _, row in df.iterrows():
        rows.append({{k: safe_val(v) for k, v in row.items()}})
    result = {{"columns": list(df.columns), "rows": rows, "elapsed": elapsed, "count": len(df)}}
print("__JSON_START__")
print(json.dumps(result, ensure_ascii=False))
"""


def _run_script(script: str, stdin_data: Optional[dict] = None,
                extra_args: Optional[list[str]] = None) -> dict:
    proc = subprocess.run(
        [_SYS_PYTHON, "-c", script] + (extra_args or []),
        input=json.dumps(stdin_data) if stdin_data else None,
        capture_output=True, text=True, timeout=120,
        cwd=_ASTOCK_PATH,
    )
    if proc.returncode != 0:
        return {"error": True, "message": proc.stderr.strip() or "执行失败"}
    out = proc.stdout.strip()
    marker = "__JSON_START__"
    idx = out.find(marker)
    if idx >= 0:
        json_str = out[idx + len(marker):].strip()
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"error": True, "message": out[:200] or "解析结果失败"}


# ═══════════════════════════════════════════════════════════════
# 过滤快照（每日独立生成）
# ═══════════════════════════════════════════════════════════════

def _ensure_filter_dir():
    _FILTER_DIR.mkdir(parents=True, exist_ok=True)


def _get_actual_date(date: Optional[str] = None) -> str:
    """确定实际日期（None → 最新交易日）"""
    if date:
        return date
    script = r"""
import sys, json
sys.path.insert(0, '{astock}')
from quant.factor_lib import QuantDB
db = QuantDB()
conn = db._connect()
row = conn.execute("SELECT MAX(date) FROM daily_bars WHERE volume > 0").fetchone()
conn.close()
print("__JSON_START__")
print(json.dumps({{"date": row[0] if row and row[0] else None}}))
"""
    result = _run_script(script.format(astock=_ASTOCK_PATH))
    return (result.get("date") or datetime.now().strftime("%Y-%m-%d"))


def _check_date_has_data(date: str) -> tuple[bool, str]:
    """检查指定日期是否数据完整。返回 (ok, 缺失说明)"""
    script = r"""
import sys, json
sys.path.insert(0, '{astock}')
from quant.factor_lib import QuantDB
db = QuantDB()
conn = db._connect()
bars = conn.execute("SELECT COUNT(*) FROM daily_bars WHERE date=? AND close>0", [sys.argv[1]]).fetchone()[0]
val = conn.execute("SELECT COUNT(*) FROM daily_valuation WHERE date=?", [sys.argv[1]]).fetchone()[0]
conn.close()
issues = []
if bars == 0: issues.append("daily_bars")
if val == 0: issues.append("daily_valuation")
print("__JSON_START__")
print(json.dumps({{"bars": bars, "valuation": val, "issues": issues}}))
"""
    result = _run_script(script.format(astock=_ASTOCK_PATH), extra_args=[date])
    issues = result.get("issues", [])
    if issues:
        return False, "、".join(issues)
    return True, ""


def generate_filter_snapshot(date: Optional[str] = None) -> dict:
    """生成过滤快照（运行所有过滤，保存到 filter_cache/{date}.json）"""
    _ensure_filter_dir()
    actual_date = _get_actual_date(date)
    snapshot_file = _FILTER_DIR / f"{actual_date}.json"

    # 已存在则直接返回
    if snapshot_file.exists():
        try:
            return json.loads(snapshot_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass

    result = _run_script(
        _FILTER_SNAPSHOT_SCRIPT.format(astock=_ASTOCK_PATH),
        extra_args=[actual_date],
    )
    if "error" not in result:
        snapshot_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def get_filter_snapshot(date: Optional[str] = None) -> dict:
    """读取过滤快照，不存在则生成"""
    _ensure_filter_dir()
    actual_date = _get_actual_date(date)
    if date:
        ok, missing = _check_date_has_data(actual_date)
        if not ok:
            return {"error": True, "message": f"交易日期 {actual_date} 数据不完整（缺失: {missing}），可能是周末或节假日"}
    snapshot_file = _FILTER_DIR / f"{actual_date}.json"
    if snapshot_file.exists():
        try:
            return json.loads(snapshot_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass
    return generate_filter_snapshot(actual_date)


def build_filtered_out(snapshot: dict, active_filters: Optional[list[str]] = None) -> list[dict]:
    """从快照构建 filtered_out 列表（仅含指定过滤）"""
    if active_filters is None:
        active_filters = list(_DEFAULT_FILTERS)
    filters_data = snapshot.get("filters", {})
    result = []
    for fname in active_filters:
        fdata = filters_data.get(fname)
        if not fdata:
            continue
        for s in fdata.get("stocks", []):
            result.append({
                "code": s["code"],
                "name": s.get("name", ""),
                "close": s.get("close"),
                "change_pct": s.get("change_pct"),
                "mcap": s.get("mcap"),
                "turnover_pct": s.get("turnover_pct"),
                "amount": s.get("amount"),
                "amount_avg_30d": s.get("amount_avg_30d"),
                "filter_name": fname,
                "filter_desc": fdata.get("desc", ""),
            })
    return result


# ═══════════════════════════════════════════════════════════════
# 因子列表 & 执行
# ═══════════════════════════════════════════════════════════════

def list_factors() -> dict:
    return _run_script(_LIST_SCRIPT.format(astock=_ASTOCK_PATH))


def _ensure_cache_dir():
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_key(factor_name: str, date: Optional[str], top_n: int,
               filters: Optional[list[str]]) -> str:
    payload = json.dumps(
        [factor_name, date or "", top_n, sorted(filters) if filters else None],
        sort_keys=True, ensure_ascii=True,
    )
    return hashlib.md5(payload.encode()).hexdigest()[:12]


def execute_factor(
    factor_name: str,
    date: Optional[str] = None,
    top_n: int = 100,
    filters: Optional[list[str]] = None,
) -> dict:
    """执行因子排名，优先读缓存。

    1. 确保当天过滤快照存在
    2. 从快照取 excluded_codes 传给脚本
    3. 从快照构建 filtered_out 附在结果中
    """
    _ensure_cache_dir()
    actual_date = _get_actual_date(date)
    if date:
        ok, missing = _check_date_has_data(actual_date)
        if not ok:
            return {"error": True, "message": f"交易日期 {actual_date} 数据不完整（缺失: {missing}），可能是周末或节假日"}
    active_filters = filters if filters is not None else list(_DEFAULT_FILTERS)

    key = _cache_key(factor_name, date, top_n, filters)
    cache_file = _CACHE_DIR / f"{key}.json"

    # 命中缓存
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            cached["cached"] = True
            return cached
        except (json.JSONDecodeError, KeyError):
            cache_file.unlink(missing_ok=True)

    # 确保当天过滤快照（用于展示剔除明细）
    snapshot = get_filter_snapshot(actual_date)

    # 执行因子（rank_stocks 内部处理过滤逻辑）
    # filters=None 时传默认过滤（不含 limit），避免 rank_stocks 用自己的默认值
    result = _run_script(_RUN_SCRIPT.format(astock=_ASTOCK_PATH), {
        "factor_name": factor_name,
        "date": date,
        "top_n": top_n,
        "filters": active_filters,
    })

    if "error" not in result:
        result["cache_key"] = key
        result["cached"] = False
        result["factor_name"] = factor_name
        result["date"] = date
        result["top_n"] = top_n
        result["filters"] = filters
        result["saved_at"] = datetime.now().isoformat(timespec="seconds")
        result["filtered_out"] = build_filtered_out(snapshot, active_filters)
        cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    return result


# ═══════════════════════════════════════════════════════════════
# 缓存历史
# ═══════════════════════════════════════════════════════════════

def get_history() -> list[dict]:
    _ensure_cache_dir()
    history = []
    for f in sorted(_CACHE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            history.append({
                "cache_key": data.get("cache_key", f.stem),
                "factor_name": data.get("factor_name", ""),
                "date": data.get("date"),
                "top_n": data.get("top_n", 20),
                "filters": data.get("filters"),
                "count": data.get("count", 0),
                "elapsed": data.get("elapsed", 0),
                "saved_at": data.get("saved_at", ""),
            })
        except (json.JSONDecodeError, KeyError):
            pass
    return history


def get_cached_result(cache_key: str) -> Optional[dict]:
    cache_file = _CACHE_DIR / f"{cache_key}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        data["cached"] = True
        return data
    except (json.JSONDecodeError, KeyError):
        return None


def delete_cached_result(cache_key: str) -> bool:
    cache_file = _CACHE_DIR / f"{cache_key}.json"
    if cache_file.exists():
        cache_file.unlink()
        return True
    return False


# ═══════════════════════════════════════════════════════════════
# 数据健康检查（持久化缓存）
# ═══════════════════════════════════════════════════════════════

_HEALTH_FILE = Path(__file__).resolve().parent / "data_health.json"

_HEALTH_SCRIPT = r"""
import sys, json
sys.path.insert(0, '{astock}')
from quant.factor_lib import QuantDB
from datetime import date, timedelta

db = QuantDB()
conn = db._connect()

# 检查指定日期的数据完整性
target_date = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
today = date.fromisoformat(target_date)
# 从 2026-05-12 起检查数据完整性
check_from = date(2026, 5, 12)
bars_dates = {{row[0] for row in conn.execute(
    "SELECT DISTINCT date FROM daily_bars WHERE date>=? AND close>0", (check_from.isoformat(),)
)}}
val_dates = {{row[0] for row in conn.execute(
    "SELECT DISTINCT date FROM daily_valuation WHERE date>=?", (check_from.isoformat(),)
)}}

# 找最新交易日
all_trading = sorted(bars_dates & val_dates)
latest_trading = all_trading[-1] if all_trading else None

# 今天状态
today_str = today.isoformat()
is_weekend = today.weekday() >= 5
today_bars = today_str in bars_dates
today_val = today_str in val_dates
today_bars_cnt = 0
today_val_cnt = 0
if today_bars:
    today_bars_cnt = conn.execute("SELECT COUNT(*) FROM daily_bars WHERE date=? AND close>0", (today_str,)).fetchone()[0]
if today_val:
    today_val_cnt = conn.execute("SELECT COUNT(*) FROM daily_valuation WHERE date=?", (today_str,)).fetchone()[0]

# 检查从 5/12 以来的所有交易日
issues = []
d = check_from
while d <= today:
    if d.weekday() < 5:
        ds = d.isoformat()
        has_b = ds in bars_dates
        has_v = ds in val_dates
        if has_b != has_v:
            issues.append({{"date": ds, "bars": has_b, "valuation": has_v}})
    d += timedelta(days=1)

conn.close()

today_status = "ok"
if not is_weekend:
    if not today_bars and not today_val:
        today_status = "missing"
    elif not today_bars or not today_val:
        today_status = "partial"

result = {{
    "checked_at": today_str,
    "check_from": "2026-05-12",
    "is_weekend": is_weekend,
    "today_status": today_status,
    "today": {{"bars": today_bars_cnt, "valuation": today_val_cnt}},
    "latest_trading_date": latest_trading,
    "issues_since_may12": len(issues),
    "ok": today_status == "ok" or is_weekend,
}}
print("__JSON_START__")
print(json.dumps(result, ensure_ascii=False))
"""


def get_data_health(target_date: Optional[str] = None) -> dict:
    """返回数据健康报告。同一天内缓存复用。target_date 为要检查的日期。"""
    check_date = target_date or datetime.now().strftime("%Y-%m-%d")
    cache_key = f"health_{check_date}"

    # 读缓存（同一天同日期复用）
    if _HEALTH_FILE.exists():
        try:
            cached = json.loads(_HEALTH_FILE.read_text(encoding="utf-8"))
            if cached.get("cache_key") == cache_key:
                return cached
        except (json.JSONDecodeError, KeyError):
            pass

    # 重新检查
    result = _run_script(_HEALTH_SCRIPT.format(astock=_ASTOCK_PATH), extra_args=[check_date])
    if "error" not in result:
        result["cache_key"] = cache_key
        _HEALTH_FILE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result
