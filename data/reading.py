"""阅读进度管理 — 本地 JSON 存储"""
import json
from datetime import datetime
from pathlib import Path

STORE = Path(__file__).resolve().parent / "reading_status.json"


def _load() -> dict:
    if not STORE.exists():
        return {}
    try:
        return json.loads(STORE.read_text())
    except (json.JSONDecodeError, IOError):
        return {}


def _save(data: dict):
    STORE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def get_all() -> dict:
    """返回全部阅读状态 {path: {status, opened_at, finished_at}}"""
    return _load()


def mark_as(path: str, status: str):
    """手动标记状态：unread / finished"""
    data = _load()
    entry = data.get(path, {"status": "unread"})
    entry["status"] = status
    if status == "finished":
        entry["finished_at"] = datetime.now().isoformat()
    elif status == "unread":
        entry.pop("finished_at", None)
        entry.pop("opened_at", None)
    data[path] = entry
    _save(data)


def mark_opened(path: str):
    """记录已打开（自动设为阅读中）"""
    data = _load()
    entry = data.get(path, {"status": "unread"})
    if entry["status"] == "unread":
        entry["status"] = "reading"
    entry["opened_at"] = datetime.now().isoformat()
    data[path] = entry
    _save(data)
