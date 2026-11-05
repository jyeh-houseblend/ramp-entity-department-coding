# Ramp multi-entity department auto-coding

Auto-codes the **NetSuite Department** on Ramp card transactions, except where
the cardholder's home entity differs from the entity the card spends against.

| Cardholder entity vs card entity | Action |
|---|---|
| Same | Code the employee's default department (Ramp department → NetSuite option) |
| Different | Leave the department blank for the employee / reviewer (logged as an exception) |
| Either unknown | Leave blank (exception) |

The script never overwrites a department that is already set, and it does not
touch split transactions.

## How entities are determined

- **User entity:** `GET /users/{user_id}` → `entity_id` (cardholder from `transaction.card_holder.user_id`).
- **Card entity:** `transaction.entity_id`, which Ramp derives from the card's fund.
  If that is missing, the script falls back to `GET /funds/{fund_id}` → `entity_id`.
  Ramp's current physical/virtual card endpoints don't return `entity_id`, and
  the legacy `/cards/{id}` API is deprecated, so the card itself isn't queried.

Both lookups are cached in memory (`CACHE_TTL_SECONDS`).

## How the department is chosen (entity match only)

1. The employee's Ramp `department_id` → Ramp department name (`GET /departments`).
2. `DEPARTMENT_MAP_FILE` override, if the department id or name is listed
   (`null` = never auto-code that department). See `department_map.example.json`.
3. Otherwise a name match against the active NetSuite Department options: an
   exact match first, then a unique match on the leaf of a hierarchical name
   (`G&A : Operations` matches `Operations`).
4. If there is no match or more than one, the transaction is reported as
   `no_department_mapping` and left alone. The script never guesses.

The department is written with `POST /developer/v1/accounting/codings` (Ramp
`ramp_id`s, `object_type: TRANSACTION`). After each write the transaction is
read back to confirm two things: the department is set, and no other coded
field was dropped. If a field was dropped, the run stops.

## Setup

```powershell
python -m pip install -r requirements.txt
copy .env.example .env      # fill in credentials
python -m scripts.discover_fields            # find the Department field's ramp_id
python -m scripts.discover_fields --options  # preview Ramp dept -> NetSuite option mapping
```

Put the Department field's `ramp_id` in `NETSUITE_DEPARTMENT_TRACKING_CATEGORY_ID`.

## Running

```powershell
python -m scripts.sync_transactions --dry-run --since 7d --report preview.csv
python -m scripts.sync_transactions --since 2026-10-01
python -m scripts.sync_transactions --transaction-id <uuid> --dry-run
python -m scripts.sync_transactions --watch     # every POLL_INTERVAL_MINUTES
```

By default it pulls `CLEARED` transactions with no sync commits (not yet sent
to NetSuite) from the last `LOOKBACK_DAYS`. Use `--state ALL` or
`--include-synced` to widen the pull.

`--report` writes one CSV row per transaction. Filter on `mismatch_*` to get
the reviewer's exception list.

### Outcomes

| Outcome | Meaning | Recorded in state.db (not re-evaluated) |
|---|---|---|
| `applied` / `would_apply` | Department coded (or would be, in a dry run) | yes / no |
| `mismatch_left_blank` | Entity mismatch, department blank: needs manual selection | yes |
| `mismatch_has_department` | Entity mismatch, but something already filled a department: **review it** | yes |
| `already_coded` | Entities match, department already set | yes |
| `entity_unknown`, `user_has_no_department`, `no_department_mapping`, `split_transaction`, `error` | Not coded; retried on the next run | no |

Use `--reprocess` to ignore `state.db`. Dry runs never write to `state.db`.

### Resilience

- The OAuth token is cached and refreshed 60s before expiry. A 401 triggers
  one forced refresh.
- 429 and 5xx responses and network errors are retried with exponential
  backoff and jitter (honouring `Retry-After`), up to `MAX_RETRIES`.

## Tests

```powershell
python -m pytest
```

The tests serve a fake Ramp API through `httpx.MockTransport`, so they
exercise the real client: auth, retries, pagination, and the match, mismatch,
fallback, dry-run, state and field-loss paths.

## Before running in production

- **Ramp default coding.** If Ramp is set to default the Department from the
  employee, mismatched transactions will arrive already coded and show up as
  `mismatch_has_department`. This script flags those but does not clear them.
  To get a true blank, turn off that default in Ramp.
- **Coding endpoint.** Run a sandbox write first and check that
  `POST /accounting/codings` keeps the transaction's other coded fields. The
  post-write check will stop the run if it doesn't.

## Webhook server (real time)

`server/` runs the same `TransactionProcessor` as an HTTP service, so a
transaction is coded seconds after it clears instead of on the next poll.
Same settings, same rules, same `DRY_RUN` switch. The CLI stays available
for backfills and one-off checks.

| Endpoint | Purpose |
|---|---|
| `GET /health` | `{"status": "healthy"}` |
| `POST /webhooks/ramp` | Ramp events. Only `transactions.cleared` is processed |

How an event is handled:

1. `webhooks.verification` (subscription setup) is answered automatically:
   the server finds the one `pending_verification` subscription and echoes
   the challenge. This uses a separate `webhooks:read webhooks:write` token,
   so the main token's scopes don't change.
2. Every other event must carry a valid `X-Ramp-Signature` (HMAC-SHA256 of the
   raw body with `RAMP_WEBHOOK_SECRET`), or it gets `401`. If the secret is
   blank, events are accepted unverified and a warning is logged at startup.
   That is meant only for the gap between registering and setting the secret.
3. The server returns `202` immediately. A background task fetches the
   transaction and runs it through the processor. Transactions are processed
   one at a time.
4. Repeat deliveries of the same event id are dropped in memory. The state
   store still blocks a second write.
5. If a write is detected to have dropped other coded fields, the server stops
   writing entirely until it is restarted, the same way the CLI aborts its run.

### Run locally

```powershell
python -m pip install -r requirements.txt
uvicorn server.app:create_app --factory --host 0.0.0.0 --port 8000
```

To receive real events locally, expose the port with a tunnel (e.g.
`ngrok http 8000`) and register the tunnel URL as below.

### Deploy (Render)

`render.yaml` defines a free web service that starts with
`uvicorn server.app:create_app --factory --host 0.0.0.0 --port $PORT`.

1. Deploy with `DRY_RUN=true` and `RAMP_WEBHOOK_SECRET` blank.
2. Register the subscription. Ramp sends the verification challenge right away:
   ```powershell
   python -m scripts.register_webhook https://<service>.onrender.com/webhooks/ramp
   ```
   The registering credentials need `webhooks:read`, `webhooks:write` and
   `transactions:read`.
3. Put the printed secret in `RAMP_WEBHOOK_SECRET` and redeploy.
4. Check the `would_apply` / `mismatch_*` lines in the logs, then set
   `DRY_RUN=false`.

Notes:

- **state.db is not persistent** on Render's free tier; it resets on every
  deploy or restart. That's safe, because the processor never overwrites a
  department that is already set. After a reset, though, `mismatch_*`
  transactions can be logged again.
- **Ramp doesn't redeliver events that got a 2xx.** Anything that cleared
  while the service was down or failing has to be caught up with the CLI, e.g.
  `python -m scripts.sync_transactions --since 7d`.
- **Free Render services sleep when idle.** The first event after a quiet
  period also wakes the service, which takes a few seconds.
