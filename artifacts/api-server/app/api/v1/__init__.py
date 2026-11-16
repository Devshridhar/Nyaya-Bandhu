"""Version 1 API router."""
from fastapi import APIRouter

from app.api.v1.cases import router as cases_router
from app.api.v1.whatsapp import router as whatsapp_router

router = APIRouter()
router.include_router(cases_router)
router.include_router(whatsapp_router)
