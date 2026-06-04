"""家庭资料数据模块 — JSON 文件存储
文件结构:
{
  "config": {
    "groups": ["人员", "房产", "车辆"],
    "categories": ["身份证", "户口本", ...]
  },
  "documents": [
    {"id": "...", "group": "人员", "item": "邹新龙", "name": "...", ...}
  ]
}
三层展示: 大类(group) → 子项(item) → 分类(category) → 文档
"""
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

STORE = Path(__file__).resolve().parent / "family_docs.json"

# 仅在新文件初始化时使用
DEFAULT_GROUPS = ["人员", "房产", "车辆"]


def _init_config() -> dict:
    return {"groups": list(DEFAULT_GROUPS)}


def _load_full() -> tuple[dict, bool]:
    """返回 ({config: {...}, documents: [...]}, is_new)
    is_new=True 表示文件不存在或为空，刚用默认值初始化"""
    if not STORE.exists():
        data = {"config": _init_config(), "documents": []}
        _save(data)
        return data, True
    try:
        raw = STORE.read_text().strip()
        if not raw:
            data = {"config": _init_config(), "documents": []}
            _save(data)
            return data, True
        data = json.loads(raw)
    except (json.JSONDecodeError, IOError):
        data = {"config": _init_config(), "documents": []}
        _save(data)
        return data, True

    # 兼容旧格式：纯数组
    if isinstance(data, list):
        data = {"config": _init_config(), "documents": data}
        _save(data)
        return data, True
    # 新格式
    changed = False
    if "config" not in data:
        data["config"] = _init_config()
        changed = True
    if "documents" not in data:
        data["documents"] = []
        changed = True
    if changed:
        _save(data)
    return data, False


def _save(data: dict):
    """原子写入：先写 .tmp 再 rename，避免进程崩溃导致数据清空。"""
    tmp = STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, STORE)  # 同文件系统上原子操作


def _load() -> dict:
    """内部使用，只返回数据字典"""
    data, _ = _load_full()
    return data


def get_config() -> dict:
    return _load()["config"]


def get_all() -> dict:
    """返回三层分组: 大类 → 子项 → 分类 → 文档 + 配置信息"""
    data, is_new = _load_full()
    docs = data["documents"]
    config = data["config"]

    # 三层分组
    by_group: dict[str, dict[str, dict[str, list[dict]]]] = {}
    all_categories: set[str] = set()

    for doc in docs:
        g = doc.get("group", "其他")
        it = doc.get("item", "默认")
        cat = doc.get("category", "其他")
        all_categories.add(cat)

        if g not in by_group:
            by_group[g] = {}
        if it not in by_group[g]:
            by_group[g][it] = {}
        if cat not in by_group[g][it]:
            by_group[g][it][cat] = []
        by_group[g][it][cat].append(doc)

    # 对每个大类 → 子项 → 分类排序
    group_order = config.get("groups", [])
    for g in by_group:
        ordered_items: dict[str, dict[str, list[dict]]] = {}
        for it in by_group[g]:
            ordered_items[it] = by_group[g][it]
        by_group[g] = ordered_items

    # 大类列表（按 config 顺序，追加文档中有但 config 无的）
    groups = list(group_order)
    for g in by_group:
        if g not in groups:
            groups.append(g)

    items_by_group: dict[str, list[str]] = {}
    for g in groups:
        if g in by_group:
            items_by_group[g] = list(by_group[g].keys())

    return {
        "config": config,
        "groups": groups,
        "items_by_group": items_by_group,
        "documents": by_group,
        "total": len(docs),
        "categories": sorted(all_categories),
        "initialized": is_new,                # 文件刚被创建
        "is_empty": len(docs) == 0,           # 无任何资料（持续提醒）
        "is_default": is_new or len(docs) == 0,  # 综合：默认空状态
    }


def get_groups() -> list[str]:
    return _load()["config"].get("groups", [])


def get_items(group: str) -> list[str]:
    docs = _load()["documents"]
    seen: list[str] = []
    for d in docs:
        if d.get("group") == group:
            it = d.get("item", "默认")
            if it not in seen:
                seen.append(it)
    return seen


def get_by_id(doc_id: str) -> dict | None:
    for d in _load()["documents"]:
        if d.get("id") == doc_id:
            return d
    return None


def add(group: str, item: str, category: str, file_path: str) -> dict:
    data = _load()
    # name 自动取文件名（不含扩展名）
    fname = Path(file_path).stem
    doc = {
        "id": uuid.uuid4().hex[:12],
        "group": group.strip(),
        "item": item.strip(),
        "name": fname or category.strip() or "未命名",
        "category": category.strip(),
        "file_path": file_path.strip(),
        "created_at": datetime.now().isoformat(),
    }
    data["documents"].append(doc)
    _save(data)
    return doc


def update(doc_id: str, group: str = None, item: str = None,
           category: str = None, file_path: str = None) -> dict | None:
    data = _load()
    for d in data["documents"]:
        if d.get("id") == doc_id:
            if group is not None:
                d["group"] = group.strip()
            if item is not None:
                d["item"] = item.strip()
            if category is not None:
                d["category"] = category.strip()
            if file_path is not None:
                d["file_path"] = file_path.strip()
            _save(data)
            return d
    return None


def delete(doc_id: str) -> bool:
    data = _load()
    new_docs = [d for d in data["documents"] if d.get("id") != doc_id]
    if len(new_docs) == len(data["documents"]):
        return False
    data["documents"] = new_docs
    _save(data)
    return True


def delete_item(group: str, item: str) -> int:
    data = _load()
    new_docs = [d for d in data["documents"] if not (d.get("group") == group and d.get("item") == item)]
    removed = len(data["documents"]) - len(new_docs)
    data["documents"] = new_docs
    _save(data)
    return removed


def delete_group(group: str) -> int:
    data = _load()
    new_docs = [d for d in data["documents"] if d.get("group") != group]
    removed = len(data["documents"]) - len(new_docs)
    data["documents"] = new_docs
    _save(data)
    return removed


def rename_group(old_name: str, new_name: str) -> int:
    data = _load()
    count = 0
    for d in data["documents"]:
        if d.get("group") == old_name:
            d["group"] = new_name.strip()
            count += 1
    if count > 0:
        _save(data)
    return count


def rename_item(group: str, old_item: str, new_item: str) -> int:
    data = _load()
    count = 0
    for d in data["documents"]:
        if d.get("group") == group and d.get("item") == old_item:
            d["item"] = new_item.strip()
            count += 1
    if count > 0:
        _save(data)
    return count


def get_file_info(rel_path: str) -> dict | None:
    p = Path(rel_path).expanduser()
    if not p.is_absolute():
        p = Path.home() / rel_path
    if not p.exists() or not p.is_file():
        return {"path": str(p), "exists": False, "suffix": "", "size": 0, "is_image": False, "is_pdf": False, "is_md": False}
    suffix = p.suffix.lower()
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
    return {
        "path": str(p), "exists": True, "suffix": suffix,
        "size": p.stat().st_size, "is_image": suffix in image_exts, "is_pdf": suffix == ".pdf",
        "is_md": suffix == ".md",
    }
