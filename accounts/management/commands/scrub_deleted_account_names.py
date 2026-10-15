"""One-off: blank the person-name columns that earlier hard deletes left behind.

hard_delete_personal_data used to blank only first_name/last_name, which
CustomerProfile has but Owner/Manager/Staff/Supplier profiles do not — so a
hard-deleted staff member's owner_name/manager_name/staff_name (and a
supplier's contact_person) stayed on the row. The service now clears them;
this command applies the same rule to accounts that were already deleted.

Safe to re-run: it only touches accounts with deleted_at set, and only
columns that are still non-empty. It reports ids and column names only —
never the names it is erasing.

    python manage.py scrub_deleted_account_names --dry-run
    python manage.py scrub_deleted_account_names
"""
from django.core.management.base import BaseCommand

from accounts.models import User
from accounts.services import PROFILE_PERSON_NAME_FIELDS


class Command(BaseCommand):
    help = (
        "Blank leftover person-name columns (owner_name, manager_name, "
        "staff_name, contact_person) on accounts whose personal data was "
        "already hard-deleted."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='List what would be blanked without changing anything.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        scrubbed = 0

        deleted = User.objects.filter(deleted_at__isnull=False).order_by('pk')
        for user in deleted:
            profile = user.get_role_profile()
            if profile is None:
                continue
            fields = [
                attr for attr in PROFILE_PERSON_NAME_FIELDS
                if hasattr(profile, attr) and getattr(profile, attr)
            ]
            if not fields:
                continue

            scrubbed += 1
            self.stdout.write(
                f"{'would blank' if dry_run else 'blanked'} user {user.pk} "
                f"({user.role}): {', '.join(fields)}"
            )
            if dry_run:
                continue
            for attr in fields:
                setattr(profile, attr, '')
            profile.save(update_fields=fields)

        verb = 'would be scrubbed' if dry_run else 'scrubbed'
        self.stdout.write(self.style.SUCCESS(
            f'{scrubbed} hard-deleted account(s) {verb}.'
        ))
