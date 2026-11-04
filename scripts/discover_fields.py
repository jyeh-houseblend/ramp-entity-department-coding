"""Lists Ramp accounting fields (and, with --options, a field's options and how
each Ramp department would map) so you can pin
NETSUITE_DEPARTMENT_TRACKING_CATEGORY_ID. Read-only.

    python -m scripts.discover_fields
    python -m scripts.discover_fields --options
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.auth import RampAuth  # noqa: E402
from client.ramp_api import RampClient  # noqa: E402
from config.settings import load_settings  # noqa: E402
from services.department_resolver import DepartmentResolver  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--options", action="store_true",
                        help="Show department options and the Ramp department -> option mapping")
    args = parser.parse_args()

    s = load_settings()
    client = RampClient(s.base_url, RampAuth(s.client_id, s.client_secret, s.base_url, s.scopes))

    print(f"{'ramp_id':<38} {'id / external_id':<20} name")
    for f in client.list_accounting_fields():
        print(f"{f.ramp_uuid or '':<38} {f.erp_id or '':<20} {f.display_name or f.name}")

    if args.options:
        resolver = DepartmentResolver(client, field_id=s.department_field_id,
                                      field_name=s.department_field_name,
                                      map_file=s.department_map_file)
        print(f"\nDepartment field: {resolver.field().display_name or resolver.field().name}")
        print(f"{len(resolver.options())} active options\n")
        print(f"{'Ramp department':<40} -> NetSuite option")
        for dept_id, name in sorted(resolver.ramp_departments().items(), key=lambda kv: kv[1]):
            option = resolver.resolve(dept_id)
            print(f"{name[:40]:<40} -> {option.label if option else '** UNMAPPED **'}")


if __name__ == "__main__":
    main()
