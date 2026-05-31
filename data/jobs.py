"""岗位数据读取模块 — 从 seen_jobs.json 和 output/*.md 读取"""
import json
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SEEN_FILE = BASE_DIR / "data/seen_jobs.json"
OUTPUT_DIR = BASE_DIR / "output"
SEARCHJOB_OUTPUT = Path.home() / "Documents/searchjob/output"


def load_all_jobs() -> list[dict]:
    """加载所有已见岗位"""
    if not SEEN_FILE.exists():
        return []
    try:
        raw = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError):
        return []

    jobs = []
    for job_id, j in raw.items():
        jobs.append({
            "id": job_id,
            "title": j.get("title", ""),
            "link": j.get("link", ""),
            "date": j.get("date", ""),
            "source": j.get("source", ""),
            "score": j.get("score", 0),
            "tag": j.get("tag", "📋 一般岗位"),
            "emails": j.get("emails", []),
            "deadline": j.get("deadline", ""),
            "first_seen": j.get("first_seen", ""),
        })
    # 按 first_seen 降序，同天内按 score 降序
    jobs.sort(key=lambda j: (j["first_seen"], j["score"]), reverse=True)
    return jobs


def get_jobs_by_date(target_date: str = None) -> list[dict]:
    """获取指定日期的新增岗位"""
    if target_date is None:
        target_date = date.today().isoformat()
    all_jobs = load_all_jobs()
    return [j for j in all_jobs if j["first_seen"] == target_date]


def get_job_dates() -> list[str]:
    """获取有数据的日期列表"""
    jobs = load_all_jobs()
    dates = sorted(set(j["first_seen"] for j in jobs), reverse=True)
    return dates


def get_job_summary() -> dict:
    """获取岗位概览：最新日期、新增数、各级别数量"""
    jobs = load_all_jobs()
    if not jobs:
        return {"latest_date": None, "total": 0, "by_tag": {}, "new_today": 0}

    dates = sorted(set(j["first_seen"] for j in jobs), reverse=True)
    latest_date = dates[0]
    today_jobs = [j for j in jobs if j["first_seen"] == latest_date]

    by_tag = {}
    for j in today_jobs:
        tag = j.get("tag", "📋 一般岗位")
        by_tag[tag] = by_tag.get(tag, 0) + 1

    return {
        "latest_date": latest_date,
        "total": len(jobs),
        "new_today": len(today_jobs),
        "by_tag": by_tag,
        "available_dates": dates,
    }


def get_daily_report(target_date: str) -> str | None:
    """获取指定日期的 Markdown 日报内容（monitor.py 写入 searchjob/output）"""
    report_path = SEARCHJOB_OUTPUT / f"{target_date}.md"
    if report_path.exists():
        return report_path.read_text(encoding="utf-8")
    return None
