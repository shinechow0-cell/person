"""岗位状态管理 — 本地 JSON 存储，支持标记「不考虑」等状态"""
import json
from datetime import datetime
from pathlib import Path

STORE = Path(__file__).resolve().parent / "job_status.json"


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
    return _load()


def get(job_id: str) -> str:
    """返回状态，无记录返回空字符串"""
    data = _load()
    return data.get(job_id, {}).get("status", "")


def set_status(job_id: str, status: str):
    data = _load()
    if status:
        data[job_id] = {"status": status, "updated_at": datetime.now().isoformat()}
    else:
        data.pop(job_id, None)
    _save(data)
