"""国聘岗位过期校验 — 调用 iguopin 详情 API 检测已下架岗位"""
import json
import re
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

# 确保 data 模块可导入
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

SEEN_FILE = BASE_DIR / "data/seen_jobs.json"

IGUOPIN_DETAIL_API = "https://gp-api.iguopin.com/api/jobs/v1/info"
IGUOPIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.iguopin.com/",
    "Origin": "https://www.iguopin.com",
}

# 校验间隔（秒），避免触发限流
CHECK_INTERVAL = 0.3

# 请求超时（秒）
REQUEST_TIMEOUT = 15


def extract_iguopin_id(link: str) -> str | None:
    """从 iguopin 链接中提取职位 ID"""
    m = re.search(r"/job/detail/(\d+)", link)
    return m.group(1) if m else None


def _fetch_json(url: str) -> tuple[int, dict | None]:
    """使用 urllib 获取 JSON 响应，返回 (http_status, parsed_json_or_None)"""
    req = Request(url, headers=IGUOPIN_HEADERS)
    try:
        with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except URLError:
        return 0, None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0, None


def validate_iguopin_jobs(quiet: bool = False) -> dict:
    """
    校验所有 iguopin 岗位是否仍然有效。

    对每个未检查过的 iguopin 岗位调用详情 API：
    - code == 2001  → 岗位已被删除 → 标记"已下架"
    - code == 200 但 is_apply == false → 岗位已过期 → 标记"已下架"
    - code == 200 且 is_apply == true → 岗位有效 → 不标记

    已标记"已下架"的岗位跳过，避免重复 API 调用。
    网络错误/解析失败的岗位跳过（保守策略）。

    返回: {"checked": int, "expired": int, "total_iguopin": int, "errors": int}
    """
    # 延迟导入，避免循环依赖
    from data.job_status import get_all, set_status

    # 加载岗位数据
    if not SEEN_FILE.exists():
        return {"checked": 0, "expired": 0, "total_iguopin": 0, "errors": 0}

    try:
        raw = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError) as e:
        if not quiet:
            print(f"[iguopin_validator] 读取 seen_jobs.json 失败: {e}")
        return {"checked": 0, "expired": 0, "total_iguopin": 0, "errors": 0}

    # 筛选 iguopin 岗位
    iguopin_jobs: dict[str, dict] = {}
    for job_id, job in raw.items():
        link = job.get("link", "")
        if "iguopin.com/job/detail/" in link:
            iguopin_jobs[job_id] = job

    if not iguopin_jobs:
        return {"checked": 0, "expired": 0, "total_iguopin": 0, "errors": 0}

    # 加载已有状态（跳过已标记下架的）
    existing_statuses = get_all()

    # 找出需要校验的岗位
    to_check: list[tuple[str, str, str]] = []  # (job_id, iguopin_job_id, title)
    for job_id, job in iguopin_jobs.items():
        current_status = existing_statuses.get(job_id, {}).get("status", "")
        if current_status == "已下架":
            continue  # 已确认下架，跳过
        iguopin_id = extract_iguopin_id(job.get("link", ""))
        if not iguopin_id:
            continue  # 无法解析 ID，跳过
        to_check.append((job_id, iguopin_id, job.get("title", "")))

    if not to_check:
        return {
            "checked": 0,
            "expired": 0,
            "total_iguopin": len(iguopin_jobs),
            "errors": 0,
        }

    # 逐个校验
    checked = 0
    expired = 0
    errors = 0
    total = len(to_check)

    for i, (job_id, iguopin_id, title) in enumerate(to_check):
        http_status, data = _fetch_json(
            f"{IGUOPIN_DETAIL_API}?id={iguopin_id}"
        )

        if http_status != 200 or data is None:
            if not quiet:
                print(
                    f"[iguopin_validator] HTTP {http_status or 'error'} "
                    f"校验 {iguopin_id} ({title})"
                )
            errors += 1
            if i < total - 1:
                time.sleep(CHECK_INTERVAL)
            continue

        code = data.get("code")

        if code == 2001:
            # 岗位已被删除
            set_status(job_id, "已下架")
            expired += 1
            if not quiet:
                print(
                    f"[iguopin_validator] 已下架: {title} "
                    f"({iguopin_id}) — 数据不存在"
                )
        elif code == 200:
            job_data = data.get("data", {})
            is_apply = job_data.get("is_apply", True)
            if not is_apply:
                # 岗位存在但已停止接受申请
                set_status(job_id, "已下架")
                expired += 1
                if not quiet:
                    print(
                        f"[iguopin_validator] 已下架: {title} "
                        f"({iguopin_id}) — 已停止申请"
                    )
            # else: 岗位有效，不标记
        else:
            # 其他未知响应码，跳过
            if not quiet:
                print(
                    f"[iguopin_validator] 未知响应 code={code} "
                    f"校验 {iguopin_id} ({title})"
                )
            errors += 1

        checked += 1

        # 间隔等待
        if i < total - 1:
            time.sleep(CHECK_INTERVAL)

    result = {
        "checked": checked,
        "expired": expired,
        "total_iguopin": len(iguopin_jobs),
        "errors": errors,
    }

    if not quiet:
        print(
            f"[iguopin_validator] 完成: 校验 {checked}/{len(to_check)}, "
            f"下架 {expired}, 错误 {errors}, "
            f"iguopin总数 {len(iguopin_jobs)}"
        )

    return result


if __name__ == "__main__":
    # 直接运行: python3 data/iguopin_validator.py
    result = validate_iguopin_jobs(quiet=False)
    print(json.dumps(result, ensure_ascii=False, indent=2))
