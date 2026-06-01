"""Flask 路由 — 页面 + JSON API"""
import os
import sqlite3
import subprocess
from datetime import date, datetime
from pathlib import Path

from flask import Blueprint, render_template, jsonify, request, send_file, abort

from data.jobs import get_jobs_by_date, get_job_dates, get_job_summary
from data.stocks import (
    get_available_dates, get_latest_date,
    get_market_summary, get_sector_rankings, get_northbound,
    get_dragon_tiger, get_hot_stocks, get_collection_status,
)
from data.wiki import scan_wiki, get_file_content, WIKI_ROOT
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
    valid = {"jobs": "社招监控", "stocks": "A股市场", "wiki": "读书笔记", "family": "家庭资料"}
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
               ".pdf", ".doc", ".docx", ".xls", ".xlsx"}
    if p.suffix.lower() not in allowed:
        abort(403)

    # 图片直接展示，PDF 等其他格式作为附件
    image_types = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
    if p.suffix.lower() in image_types:
        return send_file(str(p), mimetype=f"image/{p.suffix.lstrip('.').replace('jpg', 'jpeg')}")

    return send_file(str(p), as_attachment=False)
