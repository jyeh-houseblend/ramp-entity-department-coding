"""CLI entrypoint.

    python -m scripts.sync_transactions --dry-run --since 2026-10-01
    python -m scripts.sync_transactions --since 7d --report exceptions.csv
    python -m scripts.sync_transactions --transaction-id <uuid> --dry-run
    python -m scripts.sync_transactions --watch          # poll every POLL_INTERVAL_MINUTES
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.auth import RampAuth, RampAuthError  # noqa: E402
from client.ramp_api import RampClient  # noqa: E402
from config.settings import Settings, load_settings  # noqa: E402
from services.department_resolver import ConfigurationError, DepartmentResolver  # noqa: E402
from services.state_store import StateStore  # noqa: E402
from services.transaction_processor import RunSummary, TransactionProcessor  # noqa: E402

logger = logging.getLogger("sync_transactions")


def parse_since(value: str) -> str:
    """'YYYY-MM-DD' or 'Nd' (N days ago) -> ISO datetime string for from_date."""
    m = re.fullmatch(r"(\d+)d", value.strip())
    if m:
        day = date.today() - timedelta(days=int(m.group(1)))
    else:
        try:
            day = date.fromisoformat(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD or Nd, got {value!r}")
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc).isoformat()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="Print intended actions; write nothing to Ramp")
    p.add_argument("--since", type=parse_since, help="YYYY-MM-DD or Nd (default: LOOKBACK_DAYS)")
    p.add_argument("--until", type=parse_since, help="YYYY-MM-DD or Nd upper bound")
    p.add_argument("--state", default="CLEARED", help="Transaction state filter (default CLEARED; ALL for any)")
    p.add_argument("--include-synced", action="store_true",
                   help="Also consider transactions already synced to NetSuite")
    p.add_argument("--transaction-id", help="Process a single transaction")
    p.add_argument("--reprocess", action="store_true", help="Ignore the processed-transaction state store")
    p.add_argument("--report", type=Path, help="Write a CSV of every result to this path")
    p.add_argument("--watch", action="store_true", help="Run continuously every POLL_INTERVAL_MINUTES")
    p.add_argument("--env-file", default=".env")
    return p


def build_processor(settings: Settings, dry_run: bool) -> tuple[TransactionProcessor, RampClient, StateStore]:
    auth = RampAuth(settings.client_id, settings.client_secret, settings.base_url, settings.scopes)
    client = RampClient(settings.base_url, auth, max_retries=settings.max_retries)
    resolver = DepartmentResolver(
        client, field_id=settings.department_field_id, field_name=settings.department_field_name,
        map_file=settings.department_map_file, cache_ttl_seconds=settings.cache_ttl_seconds)
    state = StateStore(settings.state_db_path)
    processor = TransactionProcessor(client, resolver, state, dry_run=dry_run,
                                     cache_ttl_seconds=settings.cache_ttl_seconds)
    resolver.field()  # fail fast on a bad field configuration
    return processor, client, state


def run_once(args, settings: Settings, processor: TransactionProcessor, client: RampClient) -> RunSummary:
    if args.transaction_id:
        txns = [client.get_transaction(args.transaction_id)]
        return processor.run(txns, reprocess=True)
    since = args.since or parse_since(f"{settings.lookback_days}d")
    txns = client.list_transactions(
        from_date=since, to_date=args.until,
        state=None if args.state.upper() == "ALL" else args.state.upper(),
        has_no_sync_commits=None if args.include_synced else True)
    return processor.run(txns, reprocess=args.reprocess)


def write_report(path: Path, summary: RunSummary) -> None:
    rows = [asdict(r) | {"outcome": r.outcome.value} for r in summary.results]
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Report written to %s", path)


def print_summary(summary: RunSummary, dry_run: bool) -> None:
    counts = summary.counts()
    total = len(summary.results)
    print(f"\n{'DRY RUN - ' if dry_run else ''}Processed {total} transaction(s)")
    for outcome, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {outcome:<28} {n}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.env_file)
    dry_run = args.dry_run or settings.dry_run
    logging.basicConfig(level=settings.log_level,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logger.info("Ramp environment=%s dry_run=%s", settings.environment, dry_run)

    try:
        processor, client, state = build_processor(settings, dry_run)
    except (ConfigurationError, RampAuthError) as exc:
        logger.error("%s", exc)
        return 2

    exit_code = 0
    try:
        while True:
            summary = run_once(args, settings, processor, client)
            print_summary(summary, dry_run)
            if args.report:
                write_report(args.report, summary)
            if any(r.outcome.value == "error" for r in summary.results):
                exit_code = 1
            if not args.watch or args.transaction_id:
                break
            logger.info("Sleeping %d minute(s)", settings.poll_interval_minutes)
            time.sleep(settings.poll_interval_minutes * 60)
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        state.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
