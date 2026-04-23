from .router import router

# Register route handlers on import.
from . import routes_chat, routes_core, routes_report  # noqa: F401

__all__ = ["router"]
