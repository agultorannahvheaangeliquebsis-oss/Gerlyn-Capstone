"""Rebuild the store's records as a realistic, internally consistent history.

    python manage.py rebuild_store_records --yes

Replaces the product master list (30 products with pictures), all stock,
sales, billing, procurement and refund records with a day-by-day trading
history from 1 August 2026 up to the present moment. The four branches and the
owner / supplier / staff logins are kept; nothing is renamed.

This deletes existing transactional records, so it asks for --yes and refuses
to run against a non-local database unless --allow-remote is also given.
"""
import datetime as dt

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from core.store_data import history


class Command(BaseCommand):
    help = "Rebuild products, stock and trading history (Aug 2026 to now)."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true",
                            help="Confirm that existing records may be replaced.")
        parser.add_argument("--allow-remote", action="store_true",
                            help="Permit a database that is not on this machine.")
        parser.add_argument("--start", default="2026-08-01",
                            help="First trading day, YYYY-MM-DD (default 2026-08-01).")
        parser.add_argument("--scale", type=float, default=1.0,
                            help="Multiply daily customer traffic (use <1 for a quick run).")
        parser.add_argument("--seed", type=int, default=2026,
                            help="Random seed, so a rebuild is reproducible.")
        parser.add_argument("--no-images", action="store_true",
                            help="Skip generating product pictures.")
        parser.add_argument("--images-only", action="store_true",
                            help="Only regenerate the 30 product pictures; touch nothing else.")

    def handle(self, *args, **opts):
        if opts["images_only"]:
            history.refresh_images(self.stdout)
            return
        if not opts["yes"]:
            raise CommandError(
                "This replaces products, stock, sales, billing and procurement "
                "records. Re-run with --yes to continue.")
        host = (connection.settings_dict.get("HOST") or "").strip("[]").lower()
        if host not in {"", "localhost", "127.0.0.1", "::1"} and not opts["allow_remote"]:
            raise CommandError(
                f"Database host {host!r} is not local. Pass --allow-remote if "
                "you really mean to rebuild it.")
        try:
            start = dt.date.fromisoformat(opts["start"])
        except ValueError as exc:
            raise CommandError("--start must look like 2026-08-01") from exc

        end_dt = timezone.localtime(timezone.now()).replace(microsecond=0)
        if start > end_dt.date():
            raise CommandError("--start is in the future.")

        builder, checks = history.rebuild(
            self.stdout, start=start, end_dt=end_dt, scale=opts["scale"],
            seed=opts["seed"], images=not opts["no_images"])

        self.stdout.write("")
        failed = 0
        for label, ok, detail in checks:
            line = f"  [{'ok' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail and not ok else "")
            self.stdout.write(self.style.SUCCESS(line) if ok else self.style.ERROR(line))
            failed += not ok
        s = builder.stats
        self.stdout.write(
            f"\nBuilt through {end_dt:%Y-%m-%d %H:%M}: {s['walk_in']} walk-in sales, "
            f"{s['online']} online orders ({s['online_cancelled']} cancelled), "
            f"{s['po']} purchase orders ({s['deliveries']} receipts), "
            f"{s['restock_slips']} restock slips, {s['write_offs']} write-offs, {s['returns']} supplier returns, "
            f"{s['refunds']} refunds.")
        if failed:
            raise CommandError(f"{failed} consistency check(s) failed.")
