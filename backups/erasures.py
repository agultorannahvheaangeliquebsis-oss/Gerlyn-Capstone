"""Re-applying erasures after a restore.

Restoring an old backup brings back accounts whose personal data has since been
erased on request. Every backup therefore also stores the list of erased account
ids; after a restore, the newest list is applied to the restored database so
nobody who asked to be forgotten reappears.
"""
from accounts.models import User
from accounts.services import hard_delete_personal_data


def reapply(ledger, dry_run=False):
    """Erase every account in `ledger` ({'erased_accounts': [{'id': ...}, ...]})
    that the current database still holds with its personal data.

    Returns {'reapplied': [ids], 'already_erased': n, 'not_present': n}. Safe to
    run repeatedly.
    """
    reapplied, already, missing = [], 0, 0
    for entry in ledger.get('erased_accounts', []):
        user = User.objects.filter(pk=entry['id']).first()
        if user is None:
            missing += 1
        elif user.deleted_at:
            already += 1
        else:
            if not dry_run:
                hard_delete_personal_data(user, performed_by=None)
            reapplied.append(user.pk)
    return {'reapplied': reapplied, 'already_erased': already, 'not_present': missing}
