import asyncio
from core.runtime import ensure_supported_python

ensure_supported_python()

import logging
import socket
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routers import (
    analytics,
    auth,
    contact_requests,
    flyers,
    guest_location,
    lists,
    municipalities,
    notifications,
    ops,
    offers,
    purchases,
    push,
    supermarkets,
    users,
)
from core.config import settings
from core.logging_setup import configure_logging
from core.request_timing import RequestTimingMiddleware
from services.flyer_cleanup import FlyerCleanupService
from services.extraction_startup_recovery import ExtractionStartupRecoveryService
from services.notification_jobs import NotificationJobWorker
from services.purchased_items_cleanup import PurchasedItemsCleanupService

configure_logging()

logger = logging.getLogger(__name__)


def _resume_processing_flyer(flyer_id: str) -> None:
    from core.database import get_supabase
    from services.extraction.service import ExtractionService

    sb = get_supabase()
    sb.table("flyers").update({"status": "processing", "error_message": None}).eq("id", flyer_id).execute()
    ExtractionService().run(flyer_id)


async def _drain_notification_jobs() -> None:
    await asyncio.to_thread(NotificationJobWorker().run_pending)


def _frontend_origin() -> str:
    value = getattr(settings, "frontend_url", "http://localhost:3000")
    return value if isinstance(value, str) and value else "http://localhost:3000"


def _cors_extra_origins() -> list[str]:
    value = getattr(settings, "cors_extra_origins", "")
    if not isinstance(value, str) or not value.strip():
        return []
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def _dev_extra_origins(frontend_port: int = 3000) -> list[str]:
    extras = [f"http://127.0.0.1:{frontend_port}"]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
        if not lan_ip.startswith("127."):
            extras.append(f"http://{lan_ip}:{frontend_port}")
    except Exception:
        pass
    return extras


def _loopback_host_variants(hostname: str) -> list[str]:
    if hostname == "localhost":
        return ["localhost", "127.0.0.1"]
    if hostname == "127.0.0.1":
        return ["127.0.0.1", "localhost"]
    return [hostname]


def _with_hostname(origin: str, hostname: str) -> str:
    parsed = urlsplit(origin)
    netloc = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def _dev_allow_origins() -> list[str]:
    frontend_origin = _frontend_origin()
    origins = ["http://localhost:3000", "http://127.0.0.1:3000", frontend_origin]
    parsed = urlsplit(frontend_origin)
    if parsed.hostname:
        for host in _loopback_host_variants(parsed.hostname):
            origins.append(_with_hostname(frontend_origin, host))
    origins.extend(_dev_extra_origins())
    origins.extend(_cors_extra_origins())
    return list(dict.fromkeys(origins))


def _allow_origins() -> list[str]:
    if getattr(settings, "environment", "development") == "production":
        return list(dict.fromkeys([_frontend_origin(), *_cors_extra_origins()]))
    return _dev_allow_origins()


@asynccontextmanager
async def lifespan(app: FastAPI):
    for flyer_id in ExtractionStartupRecoveryService().run():
        asyncio.create_task(asyncio.to_thread(_resume_processing_flyer, flyer_id))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        FlyerCleanupService().run,
        CronTrigger(hour=0, minute=0, timezone="Europe/Rome"),
        id="flyer_cleanup",
        replace_existing=True,
        misfire_grace_time=None,
    )
    scheduler.add_job(
        PurchasedItemsCleanupService().run,
        CronTrigger(hour=0, minute=0, timezone="Europe/Rome"),
        id="purchased_items_cleanup",
        replace_existing=True,
        misfire_grace_time=None,
    )
    scheduler.add_job(
        _drain_notification_jobs,
        IntervalTrigger(minutes=1),
        id="notification_jobs",
        replace_existing=True,
        misfire_grace_time=30,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Schedulers started: daily maintenance and notification jobs")
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(
    title="GiroSpesa API",
    description="Backend API for GiroSpesa — grocery offers from flyers.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=600,
)
app.add_middleware(RequestTimingMiddleware)

app.include_router(auth.router)
app.include_router(guest_location.router, prefix="/guest-location", tags=["guest-location"])
app.include_router(municipalities.router, prefix="/municipalities", tags=["municipalities"])
app.include_router(flyers.router, prefix="/flyers", tags=["flyers"])
app.include_router(supermarkets.router, prefix="/supermarkets", tags=["supermarkets"])
app.include_router(lists.router, prefix="/lists", tags=["lists"])
app.include_router(notifications.router, prefix="/notifications", tags=["notifications"])

app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(analytics.router, prefix="/analytics", tags=["analytics"])
app.include_router(push.router, prefix="/push", tags=["push"])
app.include_router(contact_requests.router, prefix="/contact-requests", tags=["contact-requests"])
app.include_router(purchases.router, prefix="/purchases", tags=["purchases"])
app.include_router(ops.router, prefix="/ops", tags=["ops"])
app.include_router(offers.router, prefix="/offers", tags=["offers"])


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
