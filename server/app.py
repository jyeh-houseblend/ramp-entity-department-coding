"""FastAPI webhook server.

    uvicorn server.app:create_app --factory --host 0.0.0.0 --port 8000

Builds the same TransactionProcessor as the CLI (scripts.sync_transactions)
from the same settings, so DRY_RUN, the department field, the map file and
the state store all behave identically.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from client.auth import RampAuth
from client.ramp_api import RampClient
from config.settings import Settings, load_settings

from .routes import WebhookService, router

logger = logging.getLogger("department_webhook")

# Only the verification handshake needs these; kept off the main token so the
# Ramp app's coding scopes stay exactly what the CLI documents.
WEBHOOK_SCOPES = "webhooks:read webhooks:write"


def build_service(settings: Settings) -> WebhookService:
    from scripts.sync_transactions import build_processor

    processor, client, _state = build_processor(settings, settings.dry_run)
    webhook_auth = RampAuth(settings.client_id, settings.client_secret,
                            settings.base_url, WEBHOOK_SCOPES)
    webhook_client = RampClient(settings.base_url, webhook_auth,
                                max_retries=settings.max_retries)
    return WebhookService(processor, client, webhook_client, settings.webhook_secret)


def create_app(service: WebhookService | None = None) -> FastAPI:
    if service is None:
        settings = load_settings()
        logging.basicConfig(level=settings.log_level,
                            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        logger.info("Ramp environment=%s dry_run=%s", settings.environment, settings.dry_run)
        service = build_service(settings)

    if not service.webhook_secret:
        logger.warning("RAMP_WEBHOOK_SECRET is not set - incoming webhooks are NOT "
                       "signature-verified. Set it once scripts/register_webhook.py has run.")

    app = FastAPI(title="Ramp department coding webhook")
    app.state.webhook_service = service
    app.include_router(router)
    return app
