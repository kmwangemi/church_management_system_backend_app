"""
Church Management System — Database Seeder

Idempotent: safe to run as often as you like.
  - Permissions are created if missing and their descriptions updated.
  - System roles are created if missing and linked to their permissions (links are only ever added).
  - The old CHURCH_ADMIN role is renamed to ADMIN (same id, so existing links survive).
  - Plans are created for every ChurchPlan value.
  - The super admin is created if no super admin exists; an existing account with the configured
    email is promoted (User.platform_role = SUPER_ADMIN). Its password is never changed or printed.
  - Nothing is ever deleted. Rows from the old seed that nothing uses any more are listed as "legacy".

Run:
    python -m scripts.run            (from backend root)
"""

import asyncio
import os
import sys
from collections import Counter

# Ensure backend directory is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.core.seed import seed_permissions, seed_plans, seed_roles, seed_superuser
from app.db.session import AsyncSessionLocal, engine
from scripts.seed_data import DEFAULT_SUPERUSER

# ── Colour helpers ────────────────────────────────────────────────────────────


def green(s):
    return f"\033[92m{s}\033[0m"


def yellow(s):
    return f"\033[93m{s}\033[0m"


def cyan(s):
    return f"\033[96m{s}\033[0m"


def bold(s):
    return f"\033[1m{s}\033[0m"


def red(s):
    return f"\033[91m{s}\033[0m"


_SYMBOL = {
    "created": green("+"),
    "updated": yellow("~"),
    "renamed": yellow("~"),
    "exists": cyan("·"),
    "legacy": yellow("!"),
    "warning": yellow("!"),
    "skipped": yellow("!"),
}
_NOTE = {
    "legacy": "legacy: nothing uses this any more, left in place",
    "exists": "already exists",
}


class Reporter:
    """Prints one line per item and keeps counts for the step summaries."""

    def __init__(self):
        self.counts: Counter = Counter()

    def __call__(self, kind: str, action: str, name: str) -> None:
        self.counts[(kind, action)] += 1
        if kind == "link":
            print(f"    {_SYMBOL[action]} linked -> {name}")
        else:
            note = _NOTE.get(action, action)
            print(f"  {_SYMBOL[action]} {name:<35} ({note})")

    def summary(self, kind: str) -> str:
        c = lambda a: self.counts[(kind, a)]
        return (
            f"  → {green(f'{c('created')} created')}, {yellow(f'{c('updated') + c('renamed')} updated')}, "
            f"{c('exists')} unchanged"
            + (f", {yellow(f'{c('legacy')} legacy')}" if c("legacy") else "")
        )


# ── Main ──────────────────────────────────────────────────────────────────────


async def run_seed() -> None:
    print(bold("\n══════════════════════════════════════════"))
    print(bold("  Church Management System — Seeder"))
    print(bold("══════════════════════════════════════════"))
    report = Reporter()
    try:
        async with AsyncSessionLocal() as db:
            try:
                print(f"\n{bold('[ 1/4 ] Permissions')}")
                perm_ids = await seed_permissions(db, report)
                print(report.summary("permission"))
                print(f"\n{bold('[ 2/4 ] Roles & permission assignments')}")
                await seed_roles(db, perm_ids, report)
                print(report.summary("role"))
                print(
                    f"  → {green(str(report.counts[('link', 'created')]) + ' permission links added')}"
                )
                print(f"\n{bold('[ 3/4 ] Plans')}")
                await seed_plans(db, report)
                print(report.summary("plan"))
                print(f"\n{bold('[ 4/4 ] Platform super admin')}")
                await seed_superuser(
                    db,
                    email=DEFAULT_SUPERUSER["email"],
                    password=DEFAULT_SUPERUSER["password"],
                    display_name=DEFAULT_SUPERUSER["display_name"],
                    report=report,
                )
                print(f"  password: taken from SUPERADMIN_PASSWORD (not shown)")
                print(f"\n{green(bold('✓ Seeding complete.'))}\n")
            except Exception as e:
                await db.rollback()
                print(f"\n{red(f'✗ Seeding failed: {e}')}\n")
                raise
    finally:
        await engine.dispose()  # close pooled connections cleanly so the script exits quietly


if __name__ == "__main__":
    asyncio.run(run_seed())