"""Flask 路由 — 页面 + JSON API"""
import os
import sqlite3
import subprocess
from datetime import date, datetime
from pathlib import Path

from flask import Blueprint, render_template, jsonify, request

from data.jobs import get_jobs_by_date, get_job_dates, get_job_summary
from data.stocks import (
    get_available_dates, get_latest_date,
    get_market_summary, get_sector_rankings, get_northbound,
    get_dragon_tiger, get_hot_stocks, get_collection_status,
)
from data.wiki import scan_wiki, get_file_content, WIKI_ROOT

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
    valid = {"jobs": "社招监控", "stocks": "A股市场", "wiki": "读书笔记"}
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
    target = request.args.get("date", date.today().isoformat())
    jobs = get_jobs_by_date(target)
    return jsonify({"date": target, "count": len(jobs), "jobs": jobs})


@api.route("/jobs/dates")
def api_job_dates():
    return jsonify({"dates": get_job_dates()})


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
    return jsonify({"date": target, "summary": summary, "collection": status})


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


# ── API: Wiki / 读书笔记 ──────────────────────────────────

@api.route("/wiki/books")
def api_wiki_books():
    data = scan_wiki()
    return jsonify({"books": data["books"]})


@api.route("/wiki/concepts")
def api_wiki_concepts():
    data = scan_wiki()
    return jsonify({"concepts": data["concepts"]})


@api.route("/wiki/papers")
def api_wiki_papers():
    data = scan_wiki()
    return jsonify({"papers": data["papers"]})


@api.route("/wiki/huatai")
def api_wiki_huatai():
    data = scan_wiki()
    return jsonify({
        "huatai_series": data["huatai_series"],
        "huatai_notes_html": data["huatai_notes_html"],
    })


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
        return content

    # Markdown → 最小 HTML 包装
    escaped = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    title = full.stem
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
    max-width: 800px; margin: 40px auto; padding: 0 24px;
    line-height: 1.8; color: #1d1d1f; background: #fff;
    font-size: 16px;
  }}
  h1 {{ font-size: 24px; margin-bottom: 24px; color: #0071e3; }}
  pre {{
    white-space: pre-wrap; word-wrap: break-word;
    font-family: inherit; font-size: 15px; line-height: 1.8;
    background: none; padding: 0; border: none;
  }}
</style>
</head>
<body>
<h1>{title}</h1>
<pre>{escaped}</pre>
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
