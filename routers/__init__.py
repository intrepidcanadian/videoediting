"""FastAPI sub-routers. Kept as a package so we can grow independent concerns
(library, configuration, runs) without the main `server.py` ballooning.

Usage from server.py:
    from routers import library_router, misc_router
    app.include_router(library_router)
    app.include_router(misc_router)

Endpoints that take `{run_id}` still live in server.py to keep the `_bg` task
tracker and boundary middleware coordinated with the big runs cluster.
"""

from .library import router as library_router
from .misc import router as misc_router
from .playground import router as playground_router

__all__ = ["library_router", "misc_router", "playground_router"]
