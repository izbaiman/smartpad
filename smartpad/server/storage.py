"""Document history. Nothing is written unless the user explicitly taps "Save"."""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from .config import DOCS_DIR

_ID_RE = re.compile(r"^[a-f0-9]{32}$")


_NS_RE = re.compile(r"^[a-f0-9]{16,32}$")


def _dir(ns: str | None) -> Path:
    """Each computer gets its own history. None = the server computer (top-level folder)."""
    if ns is None:
        return DOCS_DIR
    if not _NS_RE.match(ns):
        raise KeyError(ns)
    d = DOCS_DIR / ns
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(doc_id: str, ns: str | None = None) -> Path:
    if not _ID_RE.match(doc_id or ""):
        raise KeyError(doc_id)
    return _dir(ns) / f"{doc_id}.json"


def save(doc: dict, ns: str | None = None) -> dict:
    doc_id = doc.get("id") if _ID_RE.match(str(doc.get("id") or "")) else uuid.uuid4().hex
    now = time.time()
    existing = {}
    try:
        existing = get(doc_id, ns)
    except KeyError:
        pass
    record = {
        "id": doc_id,
        "title": (doc.get("title") or "Untitled").strip()[:200],
        "style": doc.get("style") or "simple",
        "raw_text": doc.get("raw_text") or "",
        "markdown": doc.get("markdown") or "",
        "image": doc.get("image") if doc.get("save_image") else None,
        "created": existing.get("created", now),
        "updated": now,
    }
    _path(doc_id, ns).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return record


def get(doc_id: str, ns: str | None = None) -> dict:
    p = _path(doc_id, ns)
    if not p.exists():
        raise KeyError(doc_id)
    return json.loads(p.read_text(encoding="utf-8"))


def list_docs(ns: str | None = None) -> list[dict]:
    out = []
    for p in _dir(ns).glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({k: d.get(k) for k in ("id", "title", "style", "created", "updated")} | {"has_image": bool(d.get("image"))})
    return sorted(out, key=lambda d: d["updated"] or 0, reverse=True)


def delete(doc_id: str, ns: str | None = None) -> None:
    p = _path(doc_id, ns)
    if not p.exists():
        raise KeyError(doc_id)
    p.unlink()
