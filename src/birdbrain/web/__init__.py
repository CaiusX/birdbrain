"""Web layer.

``create_app`` is resolved lazily (PEP 562) rather than imported here. The eager
import was not free: ``birdbrain.web.app`` ends with a module-level
``app = create_app()`` for ``uvicorn birdbrain.web.app:app``, so *importing the
package at all* built the whole central dashboard — a second ``Database()`` with
its migration and ``ANALYZE``, the operator account, the species linkifier, the
sources/sites TOML load, and a bid for the media-sweeper lock.

That happened on capture units, which import ``birdbrain.web.tbb_app``: Python
runs this ``__init__`` first, so every ``tbb-web`` start quietly constructed
central's app before serving a single unit page. It is also what left a stray
``.media-sweeper.lock`` in a unit's data directory.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # for type checkers only — never imported at runtime
    from birdbrain.web.app import create_app

__all__ = ["create_app"]


def __getattr__(name: str):
    """Resolve ``birdbrain.web.create_app`` on first access (PEP 562)."""
    if name == "create_app":
        from birdbrain.web.app import create_app  # noqa: PLC0415

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
