"""Cross-run asset library — list/read/write/delete + file-serving endpoints.

The corresponding per-run inject/promote endpoints live in server.py because
they need `{run_id}` boundary validation (handled by middleware there) and
tight coordination with the runs cluster.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

import library as library_mod

router = APIRouter(tags=["library"])


@router.get("/api/library")
def api_library_list(kind: Optional[str] = None, tag: Optional[str] = None):
    try:
        result = library_mod.list_items(kind=kind)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if tag:
        tag_lower = tag.strip().lower()
        result = {
            k: [item for item in items if tag_lower in [t.lower() for t in (item.get("tags") or [])]]
            for k, items in result.items()
        }
    return result


@router.get("/api/library/{kind}/{slug}")
def api_library_get(kind: str, slug: str):
    try:
        return library_mod.get_item(kind, slug)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/library/{kind}")
async def api_library_save(
    kind: str,
    name: str = Form(...),
    description: str = Form(""),
    tags: str = Form(""),
    slug: Optional[str] = Form(None),
    files: Optional[list[UploadFile]] = File(None),
):
    """Create (or update-by-slug) a library item."""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    from pathlib import Path as _Path
    import imgutils

    file_tuples: list[tuple[str, bytes]] = []
    for up in files or []:
        if not up or not up.filename:
            continue
        data = await up.read()
        if data:
            if imgutils.is_heic(data):
                data, _ = imgutils.convert_heic_to_jpeg(data)
                file_tuples.append((_Path(up.filename).stem + ".jpg", data))
            else:
                file_tuples.append((up.filename, data))
    try:
        meta = library_mod.save_item(
            kind, name=name, description=description, tags=tag_list,
            files=file_tuples, slug=slug,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return meta


@router.post("/api/library/turnaround")
async def api_library_turnaround(
    name: str = Form(...),
    description: str = Form(...),
    tags: str = Form(""),
    kind: str = Form("characters"),
):
    """Create a library item and generate a multi-angle sheet via Nano Banana.
    Supports kind='characters' (5-angle portrait turnaround),
    kind='locations' (5-angle environment sheet), and
    kind='props' (5-angle product sheet: hero, 3/4, top-down, detail,
    in-context). Returns the slug immediately; generation runs in the
    background. Poll GET /api/library/{kind}/{slug} — meta.turnaround
    shows live status (`generating` → `ready` | `failed`) and progress
    (`generated_count` / `planned_count`)."""
    import turnaround as turnaround_mod

    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    try:
        slug = await turnaround_mod.generate_turnaround(
            name=name, description=description, tags=tag_list, kind=kind,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"slug": slug, "kind": kind}


@router.delete("/api/library/{kind}/{slug}")
def api_library_delete(kind: str, slug: str):
    try:
        library_mod.delete_item(kind, slug)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@router.get("/library-assets/{path:path}")
def api_library_file(path: str):
    """Serve a library file (for <img src=...> inline display)."""
    base = library_mod.LIBRARY_ROOT.resolve()
    target = (base / path).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(target)
