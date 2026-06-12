"""Flask 路由 — 页面 + JSON API"""
import json
import os
import sqlite3
import subprocess
import threading
from datetime import date, datetime
from pathlib import Path

from flask import Blueprint, render_template, jsonify, request, send_file, abort, session

from data.jobs import get_jobs_by_date, get_job_dates, get_job_summary, load_all_jobs
from data.job_status import get_all as job_status_all, set_status as job_status_set
from data.stocks import (
    get_available_dates, get_latest_date,
    get_market_summary, get_sector_rankings, get_northbound,
    get_dragon_tiger, get_hot_stocks, get_collection_status,
    get_realtime_indices, get_historical_indices,
)
from data.wiki import scan_wiki, get_file_content, WIKI_ROOT
from data import vault
from data.family import (
    get_all as family_get_all, add as family_add, update as family_update,
    delete as family_delete,
    delete_item as family_delete_item, delete_group as family_delete_group,
    rename_group as family_rename_group, rename_item as family_rename_item,
    get_groups as family_get_groups, get_items as family_get_items,
    get_file_info,
)

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
LOCK_FILE = BASE_DIR / ".monitor.lock"
STOCK_LOCK_FILE = BASE_DIR / ".stock-trigger.lock"

# monitor.py / venv 仍在原 searchjob 目录
SEARCHJOB_DIR = Path.home() / "Documents/searchjob"

api = Blueprint("api", __name__, url_prefix="/api")
pages = Blueprint("pages", __name__)


# ── 页面 ──────────────────────────────────────────────

@pages.route("/")
def index():
    return render_template("index.html")


@pages.route("/detail/<module>")
def detail(module):
    """详情页：jobs / stocks / wiki"""
    valid = {"jobs": "社招监控", "stocks": "A股市场", "wiki": "读书笔记", "family": "家庭资料", "vault": "密码保险库"}
    if module not in valid:
        return "Not found", 404
    return render_template("detail.html", module=module, title=valid[module])


# ── API: 总体状态 ─────────────────────────────────────

@api.route("/status")
def status():
    target = request.args.get("date", date.today().isoformat())
    stock_latest = get_latest_date()

    # 岗位统计（指定日期）
    today_jobs = get_jobs_by_date(target)
    by_tag = {}
    for j in today_jobs:
        tag = j.get("tag", "📋 一般岗位")
        by_tag[tag] = by_tag.get(tag, 0) + 1
    has_new_jobs = len(today_jobs) > 0

    # A股状态（指定日期）
    stock_status = get_collection_status(target)
    stock_ok = stock_status and not stock_status.get("has_errors")
    stock_has_data = stock_status is not None

    issues = []
    if stock_status and stock_status.get("has_errors"):
        issues.append(f"A股数据采集异常: {stock_status['errors'][:100]}")
    if stock_status and stock_status.get("valuation_coverage", 100) < 80:
        issues.append(f"估值覆盖率偏低: {stock_status['valuation_coverage']}%")

    return jsonify({
        "date": target,
        "job_latest_date": target,
        "stock_latest_date": stock_latest,
        "new_jobs_count": len(today_jobs),
        "has_new_jobs": has_new_jobs,
        "stock_ok": stock_ok,
        "stock_has_data": stock_has_data,
        "issues": issues,
        "job_by_tag": by_tag,
    })


# ── API: 岗位 ─────────────────────────────────────────

@api.route("/jobs")
def api_jobs():
    target = request.args.get("date", "")
    statuses = job_status_all()
    if target == "all" or not target:
        all_jobs = load_all_jobs()
        # 附带状态
        for j in all_jobs:
            j["status"] = statuses.get(j["id"], {}).get("status", "")
        by_date: dict[str, list[dict]] = {}
        for j in all_jobs:
            d = j.get("first_seen", "未知")
            if d not in by_date:
                by_date[d] = []
            by_date[d].append(j)
        return jsonify({
            "total": len(all_jobs),
            "by_date": by_date,
            "dates": sorted(by_date.keys(), reverse=True),
        })
    jobs = get_jobs_by_date(target)
    # 附带状态（与 date=all 分支保持一致）
    for j in jobs:
        j["status"] = statuses.get(j["id"], {}).get("status", "")
    return jsonify({"date": target, "count": len(jobs), "jobs": jobs})


@api.route("/jobs/dates")
def api_job_dates():
    return jsonify({"dates": get_job_dates()})


@api.route("/jobs/<job_id>/status", methods=["PUT"])
def api_job_status(job_id):
    body = request.get_json(force=True) or {}
    status = body.get("status", "")
    job_status_set(job_id, status)
    return jsonify({"ok": True, "job_id": job_id, "status": status})


@api.route("/jobs/all")
def api_jobs_all():
    jobs = load_all_jobs()
    # 按日期分组
    by_date: dict[str, list[dict]] = {}
    for j in jobs:
        d = j.get("first_seen", "未知")
        if d not in by_date:
            by_date[d] = []
        by_date[d].append(j)
    return jsonify({
        "total": len(jobs),
        "by_date": by_date,
        "dates": sorted(by_date.keys(), reverse=True),
    })


# ── API: A股 ──────────────────────────────────────────

@api.route("/stocks/dates")
def api_stock_dates():
    return jsonify({"dates": get_available_dates()})


@api.route("/stocks/summary")
def api_stock_summary():
    target = request.args.get("date")
    if not target:
        target = get_latest_date()
    if not target:
        return jsonify({"error": "No data"}), 404
    summary = get_market_summary(target)
    status = get_collection_status(target)
    from datetime import date as dt
    today_str = dt.today().isoformat()
    if target == today_str:
        realtime = get_realtime_indices()
    else:
        realtime = get_historical_indices(target)
    return jsonify({"date": target, "summary": summary, "collection": status, "realtime_indices": realtime})


@api.route("/stocks/sectors")
def api_sectors():
    target = request.args.get("date")
    if not target:
        target = get_latest_date()
    if not target:
        return jsonify({"error": "No data"}), 404
    sectors = get_sector_rankings(target)
    return jsonify({"date": target, "count": len(sectors), "sectors": sectors})


@api.route("/stocks/northbound")
def api_northbound():
    target = request.args.get("date")
    if not target:
        target = get_latest_date()
    if not target:
        return jsonify({"error": "No data"}), 404
    nb = get_northbound(target)
    return jsonify({"date": target, "northbound": nb})


@api.route("/stocks/dragon-tiger")
def api_dragon_tiger():
    target = request.args.get("date")
    if not target:
        target = get_latest_date()
    if not target:
        return jsonify({"error": "No data"}), 404
    dt = get_dragon_tiger(target)
    return jsonify({"date": target, "count": len(dt), "dragon_tiger": dt})


@api.route("/stocks/hot")
def api_hot_stocks():
    target = request.args.get("date")
    if not target:
        target = get_latest_date()
    if not target:
        return jsonify({"error": "No data"}), 404
    hot = get_hot_stocks(target)
    return jsonify({"date": target, "count": len(hot), "hot_stocks": hot})


# ── API: 因子库 ──────────────────────────────────────────

@api.route("/factors")
def api_factors():
    """列出所有因子和过滤"""
    from data.factors import list_factors
    return jsonify(list_factors())


@api.route("/factors/filters")
def api_factors_filters():
    """返回当天过滤快照（剔除明细）"""
    from data.factors import get_filter_snapshot
    date = request.args.get("date") or None
    return jsonify(get_filter_snapshot(date))


@api.route("/stocks/industries")
def api_stock_industries():
    """返回股票→行业映射 {code: industry}"""
    import sqlite3
    from pathlib import Path
    db_path = Path.home() / ".tradingagents/data/quant.db"
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT code, industry FROM stock_industry").fetchall()
    conn.close()
    return jsonify({r[0]: r[1] for r in rows})


@api.route("/backtest/run", methods=["POST"])
def api_backtest_run():
    """执行回测"""
    from data.backtest import run_backtest
    body = request.get_json(force=True) or {}
    return jsonify(run_backtest(
        start_date=body.get("start_date", "2026-05-13"),
        end_date=body.get("end_date") or None,
        factor_name=body.get("factor_name", "composite_short"),
        top_n=int(body.get("top_n", 10)),
    ))


@api.route("/backtest/layer", methods=["POST"])
def api_backtest_layer():
    """分层收益分析"""
    from data.backtest import run_layer_analysis
    body = request.get_json(force=True) or {}
    return jsonify(run_layer_analysis(
        start_date=body.get("start_date", "2026-05-13"),
        end_date=body.get("end_date") or None,
        factor_name=body.get("factor_name", "composite_short"),
    ))


@api.route("/backtest/history")
def api_backtest_history():
    """回测历史列表"""
    from data.backtest import list_backtest_history
    return jsonify(list_backtest_history())


@api.route("/backtest/result/<cache_key>")
def api_backtest_result(cache_key):
    """读取缓存回测结果"""
    from data.backtest import get_backtest_result
    result = get_backtest_result(cache_key)
    if result is None:
        return jsonify({"error": True, "message": "缓存不存在"}), 404
    return jsonify(result)


@api.route("/research/layer")
def api_research_layer():
    """分层收益报告"""
    from data.backtest import _run_report_script
    return jsonify(_run_report_script("layer"))


@api.route("/research/quantile")
def api_research_quantile():
    """十分位收益报告"""
    from data.backtest import _run_report_script
    return jsonify(_run_report_script("quantile"))


@api.route("/research/history", methods=["GET", "DELETE"])
def api_research_history():
    """因子测试执行记录（GET 查询 / DELETE 删除指定记录）"""
    import sqlite3
    from pathlib import Path
    db_path = Path.home() / ".tradingagents/data/quant.db"
    conn = sqlite3.connect(str(db_path))
    if request.method == "DELETE":
        fid = request.args.get("id")
        if fid:
            row = conn.execute("SELECT factor_name FROM research_history WHERE id=?", (fid,)).fetchone()
            if row:
                conn.execute("DELETE FROM ic_stats WHERE factor_name=?", (row[0],))
            conn.execute("DELETE FROM research_history WHERE id=?", (fid,))
            conn.commit()
        conn.close()
        return jsonify({"ok": True})
    # 支持 ?id=X 取详情（含 saved_result）
    fid = request.args.get("id")
    if fid:
        row = conn.execute("SELECT id, run_at, factor_name, saved_result FROM research_history WHERE id=?", (fid,)).fetchone()
        conn.close()
        if row and row[3]:
            detail = json.loads(row[3])
            detail["id"] = row[0]; detail["run_at"] = row[1]; detail["factor_name"] = row[2]
            return jsonify(detail)
        conn.close() if not conn else None
        return jsonify({"error": True, "message": "无缓存"})
    rows = conn.execute("SELECT id, run_at, factor_name, start_date, end_date, total_days, factor_desc, ic_ret5, layer_top10_5d, layer_top50_5d, monotonic_5d, saved_result FROM research_history ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    return jsonify([{"id": r[0], "run_at": r[1], "factor_name": r[2], "start_date": r[3], "end_date": r[4], "total_days": r[5], "factor_desc": r[6], "ic_ret5": r[7], "top10_5d": r[8], "top50_5d": r[9], "monotonic_5d": r[10], "has_detail": bool(r[11])} for r in rows])


@api.route("/research/history", methods=["POST"])
def api_research_save():
    """保存测试结果"""
    import sqlite3
    from pathlib import Path
    from datetime import datetime
    body = request.get_json(force=True) or {}
    db_path = Path.home() / ".tradingagents/data/quant.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO research_history (run_at, factor_name, start_date, end_date, total_days, factor_desc, ic_ret3, ic_ret5, ic_ret10, ic_ret20, layer_top10_5d, layer_top20_5d, layer_top50_5d, monotonic_5d, saved_result) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.now().isoformat(), body.get("factor_name"),
         body.get("start_date"), body.get("end_date"), body.get("total_days"), body.get("factor_desc"),
         body.get("ic_ret3"), body.get("ic_ret5"), body.get("ic_ret10"), body.get("ic_ret20"),
         body.get("layer_top10_5d"), body.get("layer_top20_5d"), body.get("layer_top50_5d"),
         1 if body.get("monotonic_5d") else 0,
         json.dumps(body.get("detail"))),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@api.route("/research/run", methods=["POST"])
def api_research_run():
    """统一因子测试：IC + 分层 + 十分位"""
    import subprocess, json as j
    body = request.get_json(force=True) or {}
    factor = body.get("factor_name", "composite_short")
    sd = body.get("start_date", "2025-01-02")
    ed = body.get("end_date", "2026-06-12")
    script = f"""
import sys, json, numpy as np
sys.path.insert(0, '/Users/shinechow/Documents/code/a-stock-data')
from quant.factor_lib import QuantDB
db = QuantDB(); conn = db._connect()

horizons = ['ret_3d','ret_5d','ret_10d','ret_20d']
layers = [10,20,50]

# 一次性取所有日期的数据
dates = [r[0] for r in conn.execute(\"SELECT DISTINCT f.trade_date FROM factor_value f JOIN label_future_return l ON f.trade_date=l.trade_date AND f.code=l.code WHERE f.{factor} IS NOT NULL AND l.ret_5d IS NOT NULL AND f.trade_date BETWEEN '{sd}' AND '{ed}' ORDER BY f.trade_date\")]

# 预加载所有数据：{{date: {{horizon: [values sorted by factor DESC]}}}}
data_by_date = {{}}
for d in dates:
    rows = conn.execute(f\"SELECT f.{factor}, l.ret_3d, l.ret_5d, l.ret_10d, l.ret_20d FROM factor_value f JOIN label_future_return l ON f.trade_date=l.trade_date AND f.code=l.code WHERE f.trade_date=? AND f.{factor} IS NOT NULL\", (d,)).fetchall()
    if len(rows)<100: continue
    arr = [(float(r[0] or 0), float(r[1] or 0), float(r[2] or 0), float(r[3] or 0), float(r[4] or 0)) for r in rows]
    arr.sort(key=lambda x: x[0], reverse=True)  # sorted by factor DESC
    data_by_date[d] = arr

conn.close()

# IC: 用原始(未排序)数据算相关系数
ic_result = {{}}
for hi, h in enumerate(horizons):
    vals = []
    for d in dates:
        rows = data_by_date.get(d, [])
        if len(rows)<100: continue
        fv = np.array([r[0] for r in rows])
        lb = np.array([r[hi+1] for r in rows])
        ic=np.corrcoef(fv,lb)[0,1]
        if not np.isnan(ic): vals.append(float(ic))
    ic_result[h] = {{'ic': round(np.mean(vals),4) if vals else 0, 'days': len(vals)}}

# Layer + Quantile: 用已排序数据
layer_result = {{}}
q_result = []
for hi, h in enumerate(horizons):
    lr = {{n: [] for n in layers}}
    qr = {{i: [] for i in range(10)}}
    for d in dates:
        rows = data_by_date.get(d, [])
        if len(rows)<100: continue
        labels = [r[hi+1] for r in rows]
        for n in layers:
            top = labels[:n]
            if top: lr[n].append(float(np.mean(top)))
        # Quantile
        sz=len(labels)//10
        for qi in range(10):
            b=labels[qi*sz:(qi+1)*sz if qi<9 else len(labels)]
            if b: qr[qi].append(float(np.mean(b)))
    layer_result[h] = {{f'top{{n}}': {{'avg': round(float(np.mean(lr[n]))*100,2) if lr[n] else 0, 'days': len(lr[n])}} for n in layers}}
    for qi in range(10):
        arr=np.array(qr[qi]) if qr[qi] else np.array([])
        if len(arr)>0:
            q_result.append({{'horizon':h,'quantile':f'Q{{qi+1}}','avg_return':round(float(arr.mean())*100,2),'days':len(arr)}})

print('__JSON_START__')
print(json.dumps({{'factor': '{factor}', 'ic': ic_result, 'layers': layer_result, 'quantile': q_result}}, ensure_ascii=False))
"""
    proc = subprocess.run(["/opt/miniconda3/bin/python3", "-c", script], capture_output=True, text=True, timeout=120, cwd="/Users/shinechow/Documents/code/a-stock-data")
    if proc.returncode != 0:
        return jsonify({"error": True, "message": (proc.stderr or proc.stdout).strip()[:500]})
    out = proc.stdout.strip(); idx = out.find("__JSON_START__")
    if idx >= 0:
        return jsonify(json.loads(out[idx+14:].strip()))
    return jsonify({"error": True, "message": out[:200]})


@api.route("/research/ic-test", methods=["POST"])
def api_ic_test():
    """单因子 IC 测试"""
    import subprocess, json
    body = request.get_json(force=True) or {}
    factor = body.get("factor_name", "composite_short")
    start = body.get("start_date", "2026-05-13")
    end = body.get("end_date") or "2026-06-12"
    script = f"""
import sys, json, numpy as np
sys.path.insert(0, '/Users/shinechow/Documents/code/a-stock-data')
from quant.factor_lib import QuantDB
db = QuantDB(); conn = db._connect()
dates = [r[0] for r in conn.execute(\"SELECT DISTINCT f.trade_date FROM factor_value f JOIN label_future_return l ON f.trade_date=l.trade_date AND f.code=l.code WHERE f.{factor} IS NOT NULL ORDER BY f.trade_date\")]
horizons = ['ret_3d','ret_5d','ret_10d','ret_20d']
result = {{h: [] for h in horizons}}
for d in dates:
    rows = conn.execute(f\"SELECT f.{factor}, l.ret_3d, l.ret_5d, l.ret_10d, l.ret_20d FROM factor_value f JOIN label_future_return l ON f.trade_date=l.trade_date AND f.code=l.code WHERE f.trade_date=? AND f.{factor} IS NOT NULL\", (d,)).fetchall()
    if len(rows)<100: continue
    for hi, h in enumerate(horizons):
        fv=np.array([float(r[0] or 0) for r in rows]); lb=np.array([float(r[hi+1] or 0) for r in rows])
        ic=np.corrcoef(fv,lb)[0,1]
        if not np.isnan(ic): result[h].append(float(ic))
conn.close()
summary = {{h: {{'ic': round(np.mean(v),4) if v else 0, 'days': len(v)}} for h,v in result.items()}}
print('__JSON_START__'); print(json.dumps({{'factor': '{factor}', 'ic': summary}}, ensure_ascii=False))
"""
    proc = subprocess.run(["/opt/miniconda3/bin/python3", "-c", script], capture_output=True, text=True, timeout=30, cwd="/Users/shinechow/Documents/code/a-stock-data")
    out = proc.stdout.strip(); idx = out.find("__JSON_START__")
    return jsonify(json.loads(out[idx+14:].strip()) if idx>=0 else {"error":True})


@api.route("/research/layer-test", methods=["POST"])
def api_layer_test():
    """单因子分层测试"""
    import subprocess, json
    body = request.get_json(force=True) or {}
    factor = body.get("factor_name", "composite_short")
    script = f"""
import sys, json, numpy as np
sys.path.insert(0, '/Users/shinechow/Documents/code/a-stock-data')
from quant.factor_lib import QuantDB
db = QuantDB(); conn = db._connect()
horizons = ['ret_3d','ret_5d','ret_10d','ret_20d']
layers = [10,20,50]
result = {{}}
for h in horizons:
    dates = [r[0] for r in conn.execute(f\"SELECT DISTINCT s.trade_date FROM score_daily s JOIN label_future_return l ON s.trade_date=l.trade_date AND s.code=l.code WHERE l.{{h}} IS NOT NULL ORDER BY s.trade_date\").fetchall()]
    lr = {{n: [] for n in layers}}
    for d in dates:
        rows = conn.execute(f\"SELECT s.rank_market, l.{{h}} FROM score_daily s JOIN label_future_return l ON s.trade_date=l.trade_date AND s.code=l.code WHERE s.trade_date=? ORDER BY s.rank_market\", (d,)).fetchall()
        # Use factor value ranking instead
        rows2 = conn.execute(f\"SELECT l.{{h}} FROM factor_value f JOIN label_future_return l ON f.trade_date=l.trade_date AND f.code=l.code WHERE f.trade_date=? AND f.{factor} IS NOT NULL AND l.{{h}} IS NOT NULL ORDER BY f.{factor} DESC\", (d,)).fetchall()
        if len(rows2)<50: continue
        for n in layers:
            top = [r[0] for r in rows2[:n] if r[0] is not None]
            if top: lr[n].append(float(np.mean(top)))
    result[h] = {{f'top{{n}}': {{'avg': round(float(np.mean(lr[n]))*100,2) if lr[n] else 0, 'days': len(lr[n])}} for n in layers}}
conn.close()
print('__JSON_START__'); print(json.dumps({{'factor': '{factor}', 'layers': result}}, ensure_ascii=False))
"""
    proc = subprocess.run(["/opt/miniconda3/bin/python3", "-c", script], capture_output=True, text=True, timeout=30, cwd="/Users/shinechow/Documents/code/a-stock-data")
    out = proc.stdout.strip(); idx = out.find("__JSON_START__")
    return jsonify(json.loads(out[idx+14:].strip()) if idx>=0 else {"error":True})


@api.route("/research/stats")
def api_research_stats():
    """全量统计（读 DB 表）"""
    import subprocess, json
    script = r"""
import sys, json
sys.path.insert(0, '/Users/shinechow/Documents/code/a-stock-data')
from quant.reports.full_stats import ensure_stat_tables, query_stats
ensure_stat_tables()
result = query_stats()
if not result['ic']:
    # 首次运行，计算一次
    from quant.reports.full_stats import compute_all_stats
    compute_all_stats()
    result = query_stats()
print("__JSON_START__")
print(json.dumps(result, ensure_ascii=False))
"""
    proc = subprocess.run(
        ["/opt/miniconda3/bin/python3", "-c", script],
        capture_output=True, text=True, timeout=120,
        cwd="/Users/shinechow/Documents/code/a-stock-data",
    )
    if proc.returncode != 0:
        return jsonify({"error": True, "message": proc.stderr.strip()[:200]})
    out = proc.stdout.strip()
    idx = out.find("__JSON_START__")
    if idx >= 0:
        return jsonify(json.loads(out[idx + len("__JSON_START__"):].strip()))
    try:
        return jsonify(json.loads(out))
    except json.JSONDecodeError:
        return jsonify({"error": True, "message": out[:200]})


@api.route("/stocks/health")
def api_stocks_health():
    """数据健康检查（按天缓存，支持 ?date= 检查指定日期）"""
    from data.factors import get_data_health
    date = request.args.get("date") or None
    return jsonify(get_data_health(date))


@api.route("/factors/history")
def api_factors_history():
    """返回缓存历史列表（不含 rows）"""
    from data.factors import get_history
    return jsonify(get_history())


@api.route("/factors/result/<cache_key>", methods=["GET", "DELETE"])
def api_factors_result(cache_key):
    """读取或删除指定缓存结果"""
    from data.factors import get_cached_result, delete_cached_result
    if request.method == "DELETE":
        ok = delete_cached_result(cache_key)
        return jsonify({"ok": ok})
    result = get_cached_result(cache_key)
    if result is None:
        return jsonify({"error": True, "message": "缓存不存在"}), 404
    return jsonify(result)


@api.route("/factors/run", methods=["POST"])
def api_factors_run():
    """执行因子排名（优先读缓存）"""
    from data.factors import execute_factor
    body = request.get_json(force=True) or {}
    factor_name = body.get("factor_name", "ret_5d")
    date = body.get("date") or None
    top_n = int(body.get("top_n", 100))
    filters = body.get("filters", None)
    return jsonify(execute_factor(factor_name, date=date, top_n=top_n, filters=filters))


# ── API: Wiki / 读书笔记 ──────────────────────────────────

@api.route("/wiki/books")
def api_wiki_books():
    data = scan_wiki()
    return jsonify({"books": data["books"]})


@api.route("/wiki/papers")
def api_wiki_papers():
    data = scan_wiki()
    return jsonify({"papers": data["papers"]})


# ── API: 阅读进度 ──────────────────────────────────────

@api.route("/reading/status")
def api_reading_status():
    from data.reading import get_all
    return jsonify(get_all())


@api.route("/reading/mark", methods=["POST"])
def api_reading_mark():
    from data.reading import mark_as
    body = request.get_json(force=True) or {}
    path = body.get("path", "")
    status = body.get("status", "unread")
    if path:
        mark_as(path, status)
    return jsonify({"ok": True})


# ── 页面: Wiki 文件查看 ───────────────────────────────────

@pages.route("/wiki/view")
def wiki_view():
    rel = request.args.get("path", "")
    if not rel:
        return "Missing path", 400

    resolved_root = WIKI_ROOT.resolve()
    full = (WIKI_ROOT / rel).resolve()

    # 路径穿越保护
    if not str(full).startswith(str(resolved_root)):
        return "Forbidden", 403
    if not full.exists() or not full.is_file():
        return "Not found", 404

    try:
        content = full.read_text(encoding="utf-8")
    except (UnicodeDecodeError, IOError):
        return "无法读取文件（编码错误）", 500

    if full.suffix == ".html":
        # 记录阅读中
        from data.reading import mark_opened
        mark_opened(rel)
        return content

    # 记录阅读中
    from data.reading import mark_opened
    mark_opened(rel)

    # Markdown → HTML 渲染
    import markdown
    md = markdown.Markdown(extensions=["fenced_code", "tables"])
    body_html = md.convert(content)
    title = full.stem
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Microsoft YaHei", sans-serif;
    max-width: 800px; margin: 40px auto; padding: 0 24px;
    line-height: 1.9; color: #1d1d1f; -webkit-font-smoothing: antialiased;
  }}
  h1 {{ font-size: 28px; margin: 32px 0 24px; color: #0f172a; border-bottom: 2px solid #e2e8f0; padding-bottom: 12px; }}
  h2 {{ font-size: 22px; margin: 28px 0 14px; color: #1e293b; }}
  h3 {{ font-size: 18px; margin: 22px 0 10px; color: #334155; }}
  p {{ margin: 0 0 14px; }}
  ul, ol {{ margin: 0 0 14px; padding-left: 24px; }}
  li {{ margin-bottom: 4px; }}
  code {{ background: #f1f5f9; color: #e11d48; padding: 2px 6px; border-radius: 4px; font-family: "JetBrains Mono", monospace; font-size: 14px; }}
  pre {{ background: #1e293b; color: #e2e8f0; padding: 16px 20px; border-radius: 8px; overflow-x: auto; line-height: 1.6; font-family: "JetBrains Mono", monospace; font-size: 13px; }}
  pre code {{ background: none; color: inherit; padding: 0; font-size: inherit; }}
  blockquote {{ border-left: 4px solid #3b82f6; margin: 0 0 14px; padding: 8px 16px; background: #f8fafc; color: #475569; }}
  table {{ border-collapse: collapse; width: 100%; margin: 0 0 16px; }}
  th, td {{ border: 1px solid #e2e8f0; padding: 8px 12px; text-align: left; font-size: 14px; }}
  th {{ background: #f8fafc; font-weight: 600; }}
  a {{ color: #2563eb; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  strong {{ color: #0f172a; }}
  hr {{ border: none; border-top: 1px solid #e2e8f0; margin: 24px 0; }}
</style>
</head>
<body>
{body_html}
</body>
</html>"""


# ── API: 执行状态 & 手动触发 ───────────────────────────

@api.route("/execution-status")
def api_execution_status():
    today = date.today().isoformat()

    # 岗位执行状态：检查今天的日报文件（monitor.py 写入 searchjob/output）
    report_file = SEARCHJOB_DIR / "output" / f"{today}.md"
    jobs_done = report_file.exists()
    jobs_at = None
    if jobs_done:
        mtime = os.path.getmtime(report_file)
        jobs_at = datetime.fromtimestamp(mtime).isoformat()

    # A股执行状态：查 collection_log
    stocks_done = False
    stocks_at = None
    stocks_duration = None
    try:
        db_path = Path.home() / ".tradingagents/data/quant.db"
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            row = conn.execute(
                "SELECT started_at, completed_at, duration_sec FROM collection_log WHERE date=?",
                (today,),
            ).fetchone()
            conn.close()
            if row:
                stocks_done = True
                stocks_at = row[1] or row[0]  # completed_at or started_at
                stocks_duration = round(row[2], 1) if row[2] else None
    except Exception:
        pass

    return jsonify({
        "date": today,
        "jobs": {"done": jobs_done, "at": jobs_at},
        "stocks": {"done": stocks_done, "at": stocks_at, "duration_sec": stocks_duration},
    })


@api.route("/trigger/jobs", methods=["POST"])
def api_trigger_jobs():
    # 防重复触发
    if LOCK_FILE.exists():
        mtime = os.path.getmtime(LOCK_FILE)
        if datetime.now().timestamp() - mtime < 120:  # 2分钟内不重复触发
            return jsonify({"ok": False, "message": "正在执行中，请稍候..."})

    LOCK_FILE.write_text("running")
    try:
        subprocess.Popen(
            ["venv/bin/python3", "monitor.py", "--quiet"],
            cwd=str(SEARCHJOB_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        LOCK_FILE.unlink(missing_ok=True)
        return jsonify({"ok": False, "message": f"启动失败: {e}"}), 500

    return jsonify({"ok": True, "message": "已触发执行"})


@api.route("/trigger/validate-jobs", methods=["POST"])
def api_trigger_validate_jobs():
    """触发国聘岗位过期校验（后台线程执行）"""
    from data.iguopin_validator import validate_iguopin_jobs

    def _run():
        try:
            validate_iguopin_jobs(quiet=True)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "已触发过期岗位检查"})


@api.route("/trigger/stocks", methods=["POST"])
def api_trigger_stocks():
    if STOCK_LOCK_FILE.exists():
        mtime = os.path.getmtime(STOCK_LOCK_FILE)
        if datetime.now().timestamp() - mtime < 300:  # 5分钟内不重复触发
            return jsonify({"ok": False, "message": "正在执行中，请稍候..."})

    STOCK_LOCK_FILE.write_text("running")
    claude_bin = os.path.expanduser("~/.npm-global/bin/claude")
    try:
        subprocess.Popen(
            [claude_bin, "-p", "使用a-stock-data技能收集今天的A股收盘数据并写入quant.db"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        STOCK_LOCK_FILE.unlink(missing_ok=True)
        return jsonify({"ok": False, "message": f"启动失败: {e}"}), 500

    return jsonify({"ok": True, "message": "已触发执行（约需2-3分钟）"})


# ── API: 家庭资料 ──────────────────────────────────────────

@api.route("/family/documents")
def api_family_docs():
    return jsonify(family_get_all())


@api.route("/family/documents", methods=["POST"])
def api_family_add():
    body = request.get_json(force=True) or {}
    group = body.get("group", "").strip()
    item = body.get("item", "").strip()
    category = body.get("category", "").strip()
    file_path = body.get("file_path", "").strip()
    if not group:
        return jsonify({"ok": False, "message": "大类不能为空"}), 400
    if not item:
        return jsonify({"ok": False, "message": "子项不能为空"}), 400
    if not file_path:
        return jsonify({"ok": False, "message": "文件路径不能为空"}), 400
    doc = family_add(group, item, category, file_path)
    return jsonify({"ok": True, "doc": doc})


@api.route("/family/documents/<doc_id>", methods=["PUT"])
def api_family_update(doc_id):
    body = request.get_json(force=True) or {}
    doc = family_update(
        doc_id,
        group=body.get("group"),
        item=body.get("item"),
        category=body.get("category"),
        file_path=body.get("file_path"),
    )
    if doc is None:
        return jsonify({"ok": False, "message": "资料不存在"}), 404
    return jsonify({"ok": True, "doc": doc})


@api.route("/family/documents/<doc_id>", methods=["DELETE"])
def api_family_delete(doc_id):
    ok = family_delete(doc_id)
    if not ok:
        return jsonify({"ok": False, "message": "资料不存在"}), 404
    return jsonify({"ok": True})


@api.route("/family/groups")
def api_family_groups():
    return jsonify({"groups": family_get_groups()})


@api.route("/family/groups/<group>/items")
def api_family_items(group):
    return jsonify({"group": group, "items": family_get_items(group)})


@api.route("/family/groups/<group>", methods=["PUT"])
def api_family_rename_group(group):
    body = request.get_json(force=True) or {}
    new_name = body.get("name", "").strip()
    if not new_name:
        return jsonify({"ok": False, "message": "新名称不能为空"}), 400
    count = family_rename_group(group, new_name)
    return jsonify({"ok": True, "updated": count})


@api.route("/family/groups/<group>", methods=["DELETE"])
def api_family_delete_group(group):
    count = family_delete_group(group)
    return jsonify({"ok": True, "removed": count})


@api.route("/family/groups/<group>/items/<item>", methods=["PUT"])
def api_family_rename_item(group, item):
    body = request.get_json(force=True) or {}
    new_name = body.get("name", "").strip()
    if not new_name:
        return jsonify({"ok": False, "message": "新名称不能为空"}), 400
    count = family_rename_item(group, item, new_name)
    return jsonify({"ok": True, "updated": count})


@api.route("/family/groups/<group>/items/<item>", methods=["DELETE"])
def api_family_delete_item(group, item):
    count = family_delete_item(group, item)
    return jsonify({"ok": True, "removed": count})


@api.route("/family/config")
def api_family_config():
    """返回 family_docs.json 的原始内容（用于在线编辑）"""
    import json
    from data.family import STORE
    try:
        if STORE.exists():
            content = STORE.read_text(encoding="utf-8")
        else:
            content = json.dumps({"config": {"groups": ["人员","房产","车辆"]}, "documents": []}, ensure_ascii=False, indent=2)
        return jsonify({"ok": True, "content": content})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@api.route("/family/config", methods=["PUT"])
def api_family_config_save():
    """保存 family_docs.json 原始内容"""
    import json, os
    from data.family import STORE
    body = request.get_json(force=True) or {}
    raw = body.get("content", "")
    if not raw.strip():
        return jsonify({"ok": False, "message": "内容不能为空"}), 400
    try:
        parsed = json.loads(raw)
        # 支持新格式 {config, documents} 或旧格式 [...]
        if isinstance(parsed, list):
            parsed = {"config": {"groups": ["人员","房产","车辆"]}, "documents": parsed}
        if not isinstance(parsed, dict) or "documents" not in parsed:
            return jsonify({"ok": False, "message": "JSON 格式错误：需要 {config, documents} 结构"}), 400
        # 原子写入：先写 .tmp 再 rename
        tmp = STORE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STORE)
        return jsonify({"ok": True, "count": len(parsed.get("documents", []))})
    except json.JSONDecodeError as e:
        return jsonify({"ok": False, "message": f"JSON 格式错误: {e}"}), 400
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@api.route("/family/preview")
def api_family_preview():
    """返回文件预览信息（不传输文件内容，只返回元信息）"""
    rel = request.args.get("path", "")
    if not rel:
        return jsonify({"ok": False, "message": "缺少路径参数"}), 400
    info = get_file_info(rel)
    return jsonify({"ok": True, "info": info})


@api.route("/family/file")
def api_family_file():
    """直接提供文件内容（图片/PDF等）"""
    rel = request.args.get("path", "")
    if not rel:
        return "Missing path", 400

    p = Path(rel).expanduser()
    if not p.is_absolute():
        p = Path.home() / rel

    p = p.resolve()
    if not p.exists() or not p.is_file():
        abort(404)

    # 安全检查：只允许常见文档/图片类型
    allowed = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg",
               ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".md"}
    if p.suffix.lower() not in allowed:
        abort(403)

    # 图片直接展示，PDF 等其他格式作为附件
    image_types = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
    if p.suffix.lower() in image_types:
        return send_file(str(p), mimetype=f"image/{p.suffix.lstrip('.').replace('jpg', 'jpeg')}")

    return send_file(str(p), as_attachment=False)


@api.route("/family/markdown")
def api_family_markdown():
    """读取 markdown 文件，返回原始内容和渲染后的 HTML"""
    rel = request.args.get("path", "")
    if not rel:
        return jsonify({"ok": False, "message": "缺少路径参数"}), 400

    p = Path(rel).expanduser()
    if not p.is_absolute():
        p = Path.home() / rel
    p = p.resolve()

    if not p.exists() or not p.is_file():
        return jsonify({"ok": False, "message": "文件不存在"}), 404
    if p.suffix.lower() != ".md":
        return jsonify({"ok": False, "message": "仅支持 .md 文件"}), 400

    try:
        raw = p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, IOError):
        return jsonify({"ok": False, "message": "文件编码错误"}), 500

    import markdown
    md = markdown.Markdown(extensions=["fenced_code", "tables"])
    html = md.convert(raw)

    return jsonify({
        "ok": True,
        "title": p.stem,
        "raw": raw,
        "html": html,
    })


@api.route("/family/markdown", methods=["PUT"])
def api_family_markdown_save():
    """保存编辑后的 markdown 内容"""
    body = request.get_json(force=True) or {}
    rel = body.get("path", "").strip()
    content = body.get("content", "")

    if not rel:
        return jsonify({"ok": False, "message": "缺少路径参数"}), 400

    p = Path(rel).expanduser()
    if not p.is_absolute():
        p = Path.home() / rel
    p = p.resolve()

    if not p.exists() or not p.is_file():
        return jsonify({"ok": False, "message": "文件不存在"}), 404
    if p.suffix.lower() != ".md":
        return jsonify({"ok": False, "message": "仅支持 .md 文件"}), 400

    try:
        # 原子写入：先写 .tmp 再 rename
        tmp = p.with_suffix(".md.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, p)
    except (IOError, OSError) as e:
        return jsonify({"ok": False, "message": f"写入失败: {e}"}), 500

    return jsonify({"ok": True, "message": "已保存"})


# ── API: 密码保险库 ────────────────────────────────────────

import time as _time

VAULT_TIMEOUT = 300


def _vault_token():
    """返回 token：优先从 header，其次 session"""
    # 1. 从 X-Vault-Token header 取
    token = request.headers.get("X-Vault-Token", "")
    if token:
        return token
    # 2. 从 session 取
    token = session.get("vault_token", "")
    return token or None


@api.route("/vault/unlock", methods=["POST"])
def api_vault_unlock():
    body = request.get_json(force=True) or {}
    pwd = body.get("password", "")
    token = vault.unlock(pwd)
    if token:
        session["vault_token"] = token
        session.permanent = True
        return jsonify({"ok": True, "token": token, "timeout": VAULT_TIMEOUT})
    return jsonify({"ok": False, "message": "密码错误"}), 403


@api.route("/vault/check")
def api_vault_check():
    token = _vault_token()
    if not token:
        return jsonify({"unlocked": False, "remaining": 0})
    try:
        vault._get_key(token)  # 验证 token 是否有效
        return jsonify({"unlocked": True, "remaining": VAULT_TIMEOUT})
    except Exception:
        return jsonify({"unlocked": False, "remaining": 0})


@api.route("/vault/items")
def api_vault_items():
    token = _vault_token()
    if not token:
        return jsonify({"ok": False, "message": "未解锁"}), 401
    try:
        return jsonify(vault.get_all(token))
    except PermissionError:
        return jsonify({"ok": False, "message": "未解锁或已超时"}), 401


@api.route("/vault/items", methods=["POST"])
def api_vault_add():
    token = _vault_token()
    if not token:
        return jsonify({"ok": False, "message": "未解锁"}), 401
    body = request.get_json(force=True) or {}
    category = body.get("category", "").strip()
    name = body.get("name", "").strip()
    account = body.get("account", "").strip()
    password = body.get("password", "").strip()
    notes = body.get("notes", "").strip()
    if not name:
        return jsonify({"ok": False, "message": "名称不能为空"}), 400
    if not password:
        return jsonify({"ok": False, "message": "密码不能为空"}), 400
    try:
        doc = vault.add(token, category or "其他", name, account, password, notes or "")
        return jsonify({"ok": True, "item": doc})
    except PermissionError:
        return jsonify({"ok": False, "message": "未解锁或已超时"}), 401


@api.route("/vault/items/<item_id>", methods=["PUT"])
def api_vault_update(item_id):
    token = _vault_token()
    if not token:
        return jsonify({"ok": False, "message": "未解锁"}), 401
    body = request.get_json(force=True) or {}
    try:
        doc = vault.update(
            token, item_id,
            category=body.get("category"),
            name=body.get("name"),
            account=body.get("account"),
            password=body.get("password"),
            notes=body.get("notes"),
        )
        if doc is None:
            return jsonify({"ok": False, "message": "条目不存在"}), 404
        return jsonify({"ok": True, "item": doc})
    except PermissionError:
        return jsonify({"ok": False, "message": "未解锁或已超时"}), 401


@api.route("/vault/items/<item_id>", methods=["DELETE"])
def api_vault_delete(item_id):
    token = _vault_token()
    if not token:
        return jsonify({"ok": False, "message": "未解锁"}), 401
    try:
        if vault.delete(token, item_id):
            return jsonify({"ok": True})
        return jsonify({"ok": False, "message": "条目不存在"}), 404
    except PermissionError:
        return jsonify({"ok": False, "message": "未解锁或已超时"}), 401
