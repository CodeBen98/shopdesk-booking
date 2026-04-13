import logging
import os
from typing import Any, Optional

import httpx
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

logger = logging.getLogger(__name__)

SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or os.getenv("NEXT_PUBLIC_SUPABASE_URL", "")).strip().strip('"').strip("'")
SUPABASE_SERVICE_KEY = (os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")).strip().strip('"').strip("'")
SENTRY_DSN = os.getenv('SENTRY_DSN', '').strip() or os.getenv('BOOKING_SENTRY_DSN', '').strip()
SENTRY_ENVIRONMENT = os.getenv('SENTRY_ENVIRONMENT', '').strip() or os.getenv('RAILWAY_ENVIRONMENT', '').strip() or 'development'


def init_monitoring() -> None:
    if not SENTRY_DSN:
        return

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        send_default_pii=False,
        traces_sample_rate=1.0 if SENTRY_ENVIRONMENT == 'development' else 0.1,
        integrations=[StarletteIntegration(), FastApiIntegration()],
    )


async def record_monitoring_event(
    *,
    source: str,
    event_type: str,
    message: str,
    severity: str = 'warning',
    shop_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{SUPABASE_URL}/rest/v1/monitoring_events",
                json={
                    'shop_id': shop_id,
                    'source': source,
                    'event_type': event_type,
                    'severity': severity,
                    'message': message,
                    'payload': payload or None,
                },
                headers={
                    'apikey': SUPABASE_SERVICE_KEY,
                    'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
                    'Content-Type': 'application/json',
                },
                timeout=10,
            )
    except Exception as err:
        logger.error(f"record_monitoring_event failed: {err}")


async def capture_and_record(
    *,
    error: Exception | str | Any,
    source: str,
    event_type: str,
    message: str,
    severity: str = 'warning',
    shop_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    if SENTRY_DSN:
        sentry_sdk.capture_exception(error)

    await record_monitoring_event(
        source=source,
        event_type=event_type,
        message=message,
        severity=severity,
        shop_id=shop_id,
        payload={
            **(payload or {}),
            'error': str(error),
        },
    )
