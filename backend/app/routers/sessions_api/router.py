import logging

from fastapi import APIRouter

__DEBUG_MARKER__ = "HOST_SESSIONS_2026_02_20"

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["sessions"])
