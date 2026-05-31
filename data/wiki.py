"""Wiki / 读书笔记数据模块 — 扫描本地 Obsidian 目录"""
import os
import re
from pathlib import Path

DEFAULT_WIKI_ROOT = str(Path.home() / "Documents/mywiki/wiki")
WIKI_ROOT = Path(os.environ.get("WIKI_PATH", DEFAULT_WIKI_ROOT))

# 要忽略的文件/目录
IGNORE = {".DS_Store", ".obsidian", ".trash", ".git", "__pycache__"}


def _sort_key_numeric(name: str) -> tuple:
    """按文件名中的数字前缀排序，无数字则按字符串"""
    m = re.match(r"^(\d+)", Path(name).stem)
    if m:
        return (0, int(m.group(1)), name)
    return (1, 0, name)


def scan_wiki() -> dict:
    """扫描 wiki 目录，返回所有分类的结构化数据"""
    if not WIKI_ROOT.exists():
        return {"books": [], "concepts": [], "papers": [], "huatai_series": [], "huatai_notes_html": None}

    books = _scan_books()
    concepts = _scan_concepts()
    papers = _scan_papers()
    huatai_series, huatai_notes = _scan_huatai()

    return {
        "books": books,
        "concepts": concepts,
        "papers": papers,
        "huatai_series": huatai_series,
        "huatai_notes_html": huatai_notes,
    }


def _scan_books() -> list[dict]:
    """扫描 书籍/ 目录：每个子目录是一本书，含章节 md + 阅读笔记 HTML"""
    books_dir = WIKI_ROOT / "书籍"
    if not books_dir.is_dir():
        return []

    books = []
    for book_dir in sorted(books_dir.iterdir(), key=lambda d: d.name):
        if not book_dir.is_dir() or book_dir.name in IGNORE:
            continue

        chapters = []
        notes_html = None

        for f in sorted(book_dir.iterdir(), key=lambda x: _sort_key_numeric(x.name)):
            if f.name in IGNORE:
                continue
            if f.suffix == ".md":
                chapters.append({
                    "title": f.stem,
                    "file": str(f.relative_to(WIKI_ROOT)),
                })
            elif f.suffix == ".html":
                notes_html = str(f.relative_to(WIKI_ROOT))

        if chapters or notes_html:
            books.append({
                "name": book_dir.name,
                "chapters": chapters,
                "notes_html": notes_html,
            })

    return books


def _scan_concepts() -> list[dict]:
    """扫描 概念/ 目录：每个 md 文件是一个概念"""
    concepts_dir = WIKI_ROOT / "概念"
    if not concepts_dir.is_dir():
        return []

    concepts = []
    for f in sorted(concepts_dir.iterdir(), key=lambda x: x.name):
        if f.name in IGNORE or f.suffix != ".md":
            continue
        # 文件名格式: 概念-XXX.md → 提取 XXX
        name = f.stem
        if name.startswith("概念-"):
            name = name[3:]
        concepts.append({
            "name": name,
            "file": str(f.relative_to(WIKI_ROOT)),
        })

    return concepts


def _scan_papers() -> list[dict]:
    """扫描 论文/ 目录：每个 html 文件是一篇论文"""
    papers_dir = WIKI_ROOT / "论文"
    if not papers_dir.is_dir():
        return []

    papers = []
    for f in sorted(papers_dir.iterdir(), key=lambda x: x.name):
        if f.name in IGNORE:
            continue
        if f.suffix == ".html":
            papers.append({
                "name": f.stem,
                "file": str(f.relative_to(WIKI_ROOT)),
            })
        elif f.suffix == ".md":
            papers.append({
                "name": f.stem,
                "file": str(f.relative_to(WIKI_ROOT)),
            })

    return papers


def _scan_huatai() -> tuple[list[dict], str | None]:
    """扫描 华泰金工AI系列/ 目录：系列 md 文件 + 阅读笔记 HTML"""
    huatai_dir = WIKI_ROOT / "华泰金工AI系列"
    if not huatai_dir.is_dir():
        return [], None

    series = []
    notes_html = None

    for f in sorted(huatai_dir.iterdir(), key=lambda x: _sort_key_numeric(x.name)):
        if f.name in IGNORE:
            continue
        if f.suffix == ".md":
            series.append({
                "name": f.stem,
                "file": str(f.relative_to(WIKI_ROOT)),
            })
        elif f.suffix == ".html":
            notes_html = str(f.relative_to(WIKI_ROOT))

    return series, notes_html


def get_file_content(rel_path: str) -> tuple[str, str] | None:
    """读取 wiki 文件内容

    Returns:
        (content, content_type) 或 None（文件不存在）
        content_type: "html" | "markdown"
    """
    full = (WIKI_ROOT / rel_path).resolve()
    # 安全检查：确保在 WIKI_ROOT 内
    resolved_root = WIKI_ROOT.resolve()
    if not str(full).startswith(str(resolved_root)):
        return None
    if not full.exists() or not full.is_file():
        return None
    try:
        content = full.read_text(encoding="utf-8")
    except (UnicodeDecodeError, IOError):
        return None
    content_type = "html" if full.suffix == ".html" else "markdown"
    return content, content_type
