"""Built-in translation models: which ones this server offers, and downloading them ahead (#1191).

The operator chooses the offered models with ``LOCAL_TRANSLATION_MODELS``. Only Admins can start a
download, and only of an offered model, so an event owner's request never puts a model on the server.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status

from portal.auth import require_admin, require_admin_csrf
from portal.config import settings
from portal.routers.admin import templates
from portal.tasks import track_task
from portal.translations.metrics import translation_metrics
from portal.translations.providers.local import get_download_progress, loaded_model_ids, trigger_download
from portal.translations.registry import LOCAL_TRANSLATION_MODELS
from portal.translations.worker import SCHEDULERS
from portal.utils import safe_redirect

router = APIRouter()

PAGE_PATH = "/admin/translation-models/"


@dataclass(frozen=True)
class ModelRow:
    """One model on the page. Size, languages and licence are unknown for an operator's own build."""

    id: str
    offered: bool
    status: str
    languages: int | None = None
    approx_ram_mb: int | None = None
    licence: str | None = None


def model_rows() -> list[ModelRow]:
    """The offered models in the operator's order, then the registry models that are not offered."""
    offered = settings.local_translation_model_list
    rows = []
    for model_id in (*offered, *(m for m in LOCAL_TRANSLATION_MODELS if m not in offered)):
        entry = LOCAL_TRANSLATION_MODELS.get(model_id)
        rows.append(
            ModelRow(
                id=model_id,
                offered=model_id in offered,
                status=get_download_progress(model_id).get("status", ""),
                languages=len(entry.languages) if entry else None,
                approx_ram_mb=entry.approx_ram_mb if entry else None,
                licence=entry.licence if entry else None,
            )
        )
    return rows


@dataclass(frozen=True)
class EngineRow:
    provider: str
    running: int
    capacity: int
    waiting: int


@dataclass(frozen=True)
class LoadedModelRow:
    id: str
    approx_ram_mb: int | None


def engine_rows() -> list[EngineRow]:
    return [
        EngineRow(provider, scheduler.running, scheduler.capacity, scheduler.waiting())
        for provider, scheduler in sorted(SCHEDULERS.items())
    ]


def loaded_model_rows() -> list[LoadedModelRow]:
    rows = []
    for model_id in loaded_model_ids():
        entry = LOCAL_TRANSLATION_MODELS.get(model_id)
        rows.append(LoadedModelRow(model_id, entry.approx_ram_mb if entry else None))
    return rows


@router.get(PAGE_PATH, dependencies=[Depends(require_admin)])
async def translation_models_page(request: Request):
    loaded = loaded_model_rows()
    return templates.TemplateResponse(
        request,
        "admin/translation_models.html",
        {
            "models": model_rows(),
            "engines": engine_rows(),
            "model_stats": translation_metrics.by_model(),
            "loaded_models": loaded,
            "loaded_ram_mb": sum(row.approx_ram_mb or 0 for row in loaded),
            "window_minutes": round(translation_metrics.window / 60),
            "metrics_complete": translation_metrics.complete,
            "max_outcomes": translation_metrics.max_outcomes,
            "lag_budget": settings.translation_max_lag_seconds or None,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post(f"{PAGE_PATH}download", dependencies=[Depends(require_admin), Depends(require_admin_csrf)])
async def download_translation_model(model: str = Form(..., max_length=100)):
    if not settings.local_translation_allows(model):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That model is not offered on this server.")
    track_task(asyncio.create_task(asyncio.to_thread(trigger_download, model)))
    return safe_redirect(PAGE_PATH)
