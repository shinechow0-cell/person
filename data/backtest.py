"""回测桥接层 — subprocess 调用系统 Python 运行回测"""
import json
import subprocess
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional

_SYS_PYTHON = "/opt/miniconda3/bin/python3"
_ASTOCK_PATH = "/Users/shinechow/Documents/code/a-stock-data"
_CACHE_DIR = Path(__file__).resolve().parent / "backtest_cache"


def list_backtest_history() -> list[dict]:
    """列出回测缓存历史"""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    history = []
    for f in sorted(_CACHE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            history.append({
                "cache_key": f.stem,
                "saved_at": data.get("saved_at", ""),
                "type": "layer" if "exit_modes" in data else "backtest",
                "total_return": (data.get("summary") or {}).get("total_return", ""),
                "total_days": (data.get("summary") or {}).get("total_days", data.get("signal_days", 0)),
            })
        except (json.JSONDecodeError, KeyError):
            pass
    return history


def get_backtest_result(cache_key: str) -> Optional[dict]:
    """读取指定缓存结果"""
    cache_file = _CACHE_DIR / f"{cache_key}.json"
    if not cache_file.exists():
        return None
    try:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, KeyError):
        return None


def run_layer_analysis(
    start_date: str = "2026-05-13",
    end_date: Optional[str] = None,
    factor_name: str = "composite_short",
) -> dict:
    """分层收益分析"""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(["layer", start_date, end_date, factor_name], sort_keys=True)
    key = hashlib.md5(payload.encode()).hexdigest()[:12]
    cache_file = _CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass

    script = r"""
import sys, json
sys.path.insert(0, '{astock}')
from quant.backtest import run_layer_analysis
args = json.loads(sys.stdin.read())
result = run_layer_analysis(**args)
print("__JSON_START__")
print(json.dumps(result, ensure_ascii=False))
"""
    proc = subprocess.run(
        [_SYS_PYTHON, "-c", script.format(astock=_ASTOCK_PATH)],
        input=json.dumps({"start_date": start_date, "end_date": end_date, "factor_name": factor_name}),
        capture_output=True, text=True, timeout=600,
        cwd=_ASTOCK_PATH,
    )
    if proc.returncode != 0:
        return {"error": True, "message": proc.stderr.strip() or "执行失败"}
    out = proc.stdout.strip()
    idx = out.find("__JSON_START__")
    if idx >= 0:
        try:
            result = json.loads(out[idx + len("__JSON_START__"):].strip())
        except json.JSONDecodeError:
            return {"error": True, "message": out[:200]}
    else:
        try:
            result = json.loads(out)
        except json.JSONDecodeError:
            return {"error": True, "message": out[:200]}
    result["saved_at"] = datetime.now().isoformat(timespec="seconds")
    cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def _run_report_script(report_type: str) -> dict:
    """运行报告脚本"""
    script = r"""
import sys, json
sys.path.insert(0, '{astock}')
from quant.reports.{module} import run_{report}
result = run_{report}()
print("__JSON_START__")
print(json.dumps(result, ensure_ascii=False))
""".format(astock=_ASTOCK_PATH, module="layer_report" if report_type == "layer" else "quantile_report", report=report_type + "_report")
    proc = subprocess.run(
        [_SYS_PYTHON, "-c", script],
        capture_output=True, text=True, timeout=60,
        cwd=_ASTOCK_PATH,
    )
    if proc.returncode != 0:
        return {"error": True, "message": proc.stderr.strip() or "执行失败"}
    out = proc.stdout.strip()
    idx = out.find("__JSON_START__")
    if idx >= 0:
        try:
            return json.loads(out[idx + len("__JSON_START__"):].strip())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"error": True, "message": out[:200]}


def run_backtest(
    start_date: str = "2026-05-13",
    end_date: Optional[str] = None,
    factor_name: str = "composite_short",
    top_n: int = 10,
) -> dict:
    """执行回测，优先读缓存"""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([start_date, end_date, factor_name, top_n], sort_keys=True)
    key = hashlib.md5(payload.encode()).hexdigest()[:12]
    cache_file = _CACHE_DIR / f"{key}.json"

    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass

    script = r"""
import sys, json
sys.path.insert(0, '{astock}')
from quant.backtest import run_backtest
args = json.loads(sys.stdin.read())
result = run_backtest(**args)
print("__JSON_START__")
print(json.dumps(result, ensure_ascii=False))
"""

    proc = subprocess.run(
        [_SYS_PYTHON, "-c", script.format(astock=_ASTOCK_PATH)],
        input=json.dumps({"start_date": start_date, "end_date": end_date, "factor_name": factor_name, "top_n": top_n}),
        capture_output=True, text=True, timeout=600,
        cwd=_ASTOCK_PATH,
    )
    if proc.returncode != 0:
        return {"error": True, "message": proc.stderr.strip() or "执行失败"}

    out = proc.stdout.strip()
    marker = "__JSON_START__"
    idx = out.find(marker)
    if idx >= 0:
        try:
            result = json.loads(out[idx + len(marker):].strip())
        except json.JSONDecodeError:
            return {"error": True, "message": out[:200]}
    else:
        try:
            result = json.loads(out)
        except json.JSONDecodeError:
            return {"error": True, "message": out[:200]}

    result["saved_at"] = datetime.now().isoformat(timespec="seconds")
    cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result
