"""Typed dashboard↔backend contract.

``schema.py`` is the single source of truth for every shape in
``docs/rebuild/API.md``; ``schema.json`` is generated from it by
``python -m master_backend.app.api.export_schema`` and the dashboard generates its
TypeScript types from that file. Nothing here talks to the running app.
"""

from .schema import (
    ClientMessage,
    ServerMessage,
)

__all__ = ["ClientMessage", "ServerMessage"]