from .router import router

from . import routes_chat, routes_core, routes_report

_route_modules = (routes_chat, routes_core, routes_report)

__all__ = ["router"]
