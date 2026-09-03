"""网页控制台静态页查找：优先使用独立 static/index.html，缺失时回退内嵌。"""

from __future__ import annotations

from pathlib import Path

_FALLBACK_INDEX_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>Agent Hub 控制台</title></head>
<body><p>静态页缺失：请检查 orchestrator/console/static/index.html</p></body></html>
"""


def index_html() -> str:
    path = Path(__file__).resolve().parent / "static" / "index.html"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return _FALLBACK_INDEX_HTML
