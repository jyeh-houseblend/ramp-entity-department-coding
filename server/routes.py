"""Webhook routes and the single-transaction processing they dispatch.

Ramp event payloads carry only the object id, so each event re-fetches the
transaction and runs it through the same TransactionProcessor the CLI uses:
same entity comparison, department resolution, never-overwrite rule,
post-write field-loss check and state-store dedupe.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict

from fastapi import APIRouter, BackgroundTasks, Request, Response

from client.auth import RampAuthError
from client.ramp_api import RampAPIError, RampClient
from services.transaction_processor import Outcome, Result, TransactionProcessor

from .security import verify_signature

logger = logging.getLogger("department_webhook")

# The CLI only codes CLEARED transactions (final amount and merchant); the
# webhook matches that. Run scripts.sync_transactions to backfill anything
# missed while the service was down.
TRANSACTION_EVENTS = frozenset({"transactions.cleared"})
VERIFICATION_EVENT = "webhooks.verification"

_SEEN_MAX = 2048


class WebhookService:
    """Holds the processor and the state shared across requests."""

    def __init__(self, processor: TransactionProcessor, client: RampClient,
                 webhook_client: RampClient | None, webhook_secret: str):
        self.processor = processor
        self.client = client
        # Separate client whose token carries webhooks:* scopes, so the main
        # token never needs them. None disables the verification handshake.
        self.webhook_client = webhook_client
        self.webhook_secret = webhook_secret
        # One transaction at a time: volume is low, and it rules out two
        # deliveries of the same transaction racing each other into a write.
        self._lock = threading.Lock()
        # Separate, never-held-long lock: seen_before() runs on the event loop.
        self._seen_lock = threading.Lock()
        self._seen: OrderedDict[str, None] = OrderedDict()
        # Set when a coding write is detected to have dropped other fields.
        # Mirrors the CLI aborting its run: no further writes until restart.
        self.halted = False

    # ---------------------------------------------------------------- dedupe

    def seen_before(self, event_id: str | None) -> bool:
        """Best-effort in-memory dedupe of Ramp retries (same event id).
        The state store is the durable guard against double writes."""
        if not event_id:
            return False
        with self._seen_lock:
            if event_id in self._seen:
                return True
            self._seen[event_id] = None
            while len(self._seen) > _SEEN_MAX:
                self._seen.popitem(last=False)
        return False

    # ------------------------------------------------------------ processing

    def process_transaction_id(self, transaction_id: str) -> Result | None:
        with self._lock:
            if self.halted:
                logger.critical("txn %s: not processed - service halted after a field-loss "
                                "detection. Investigate, then restart the service.",
                                transaction_id)
                return None
            try:
                txn = self.client.get_transaction(transaction_id)
            except (RampAPIError, RampAuthError) as exc:
                logger.error("txn %s: could not fetch transaction: %s", transaction_id, exc)
                return None
            result = self.processor.process_transaction(txn)
            if result.outcome is Outcome.ERROR and result.detail.startswith("coding write dropped"):
                self.halted = True
                logger.critical("Halting all further writes after field-loss detection on %s",
                                transaction_id)
            return result

    def run_safely(self, transaction_id: str) -> None:
        try:
            self.process_transaction_id(transaction_id)
        except Exception:
            logger.exception("txn %s: unexpected error while processing", transaction_id)

    # ---------------------------------------------------------- verification

    def handle_verification(self, event: dict) -> None:
        """Confirm a new subscription: find the one pending verification and
        echo the challenge back. The payload doesn't name the subscription."""
        challenge = event.get("challenge")
        if not challenge:
            logger.warning("Verification event without a challenge; ignoring")
            return
        if self.webhook_client is None:
            logger.error("Verification challenge received but webhook management is disabled")
            return
        try:
            pending = [s for s in self.webhook_client.list_webhooks()
                       if s.get("status") == "pending_verification"]
            if len(pending) != 1:
                logger.error("Expected exactly one pending webhook subscription, found %d",
                             len(pending))
                return
            self.webhook_client.verify_webhook(pending[0]["id"], challenge)
            logger.info("Webhook subscription %s verified", pending[0]["id"])
        except (RampAPIError, RampAuthError) as exc:
            logger.error("Webhook verification failed: %s", exc)


router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "healthy"}


@router.post("/webhooks/ramp")
async def ramp_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
    service: WebhookService = request.app.state.webhook_service
    raw_body = await request.body()

    try:
        event = json.loads(raw_body)
    except ValueError:
        return Response(status_code=400)
    if not isinstance(event, dict):
        return Response(status_code=400)
    event_type = event.get("type")

    # The handshake is exempt from signature checks: the subscription's secret
    # is only issued by the create call, so it can't be configured yet. It is
    # self-limiting - it only confirms a subscription already pending.
    if event_type == VERIFICATION_EVENT:
        background_tasks.add_task(service.handle_verification, event)
        return Response(status_code=200)

    if service.webhook_secret:
        if not verify_signature(raw_body, request.headers.get("X-Ramp-Signature"),
                                service.webhook_secret):
            logger.warning("Rejected webhook %s: signature mismatch", event.get("id"))
            return Response(status_code=401)

    if event_type not in TRANSACTION_EVENTS:
        logger.info("Ignoring event type=%s", event_type)
        return Response(status_code=200)

    if service.seen_before(event.get("id")):
        logger.info("Duplicate delivery of event %s; ignoring", event.get("id"))
        return Response(status_code=200)

    transaction_id = (event.get("object") or {}).get("id")
    if not transaction_id:
        logger.warning("%s event %s has no object.id", event_type, event.get("id"))
        return Response(status_code=200)

    # Acknowledge now; Ramp retries slow responses and processing makes
    # several API calls.
    background_tasks.add_task(service.run_safely, transaction_id)
    return Response(status_code=202)
