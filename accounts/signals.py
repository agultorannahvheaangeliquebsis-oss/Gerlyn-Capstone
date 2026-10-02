from django.contrib.auth.signals import user_logged_out
from django.db.models.signals import post_save
from django.dispatch import receiver

from core.module_permissions import role_permission_defaults

from .models import (
    User, OwnerProfile, BranchManagerProfile,
    StaffProfile, CustomerProfile, SupplierProfile, UserSession
)


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if not created:
        return  # only run when a NEW user is created, not on every save

    role = instance.role
    defaults = role_permission_defaults(role)
    # JSONField defaults cannot depend on another field, so apply the selected
    # role's matrix defaults immediately after creation. Keep the in-memory
    # object synchronized with the database update too.
    instance.allowed_modules = defaults['allowed_modules']
    instance.module_actions = defaults['module_actions']
    User.objects.filter(pk=instance.pk).update(
        allowed_modules=instance.allowed_modules,
        module_actions=instance.module_actions,
    )

    if role == User.Role.OWNER and not hasattr(instance, 'owner_profile'):
        OwnerProfile.objects.create(user=instance, owner_name=instance.username)

    elif role == User.Role.BRANCH_MANAGER and not hasattr(instance, 'manager_profile'):
        BranchManagerProfile.objects.create(user=instance, manager_name=instance.username)

    elif role == User.Role.STAFF and not hasattr(instance, 'staff_profile'):
        StaffProfile.objects.create(user=instance, staff_name=instance.username)
        from core.notifications import CATEGORY_STAFF, PRIORITY_LOW, notify

        # No required_module: a new hire's own roster entry is Administration-
        # tier (same as the role-change/lockout triggers), not a section of
        # the app a colleague's module grants would apply to.
        notify(
            CATEGORY_STAFF, PRIORITY_LOW,
            f'New staff account — {instance.username}',
            f'A Staff account for {instance.username} was added.',
        )

    elif role == User.Role.SUPPLIER and not hasattr(instance, 'supplier_profile'):
        SupplierProfile.objects.create(user=instance, supplier_name=instance.username)

    elif role == User.Role.CUSTOMER and not hasattr(instance, 'customer_profile'):
        CustomerProfile.objects.create(user=instance, first_name=instance.username, last_name='')


@receiver(user_logged_out)
def cleanup_session_tracking(sender, request, user, **kwargs):
    """Drops the UserSession row for whichever session is being logged out.
    Fires before logout() flushes the session, so session_key here is still
    the real one being destroyed — covers manual "Logout" and the
    idle-timeout logout alike, since both go through this same signal."""
    session_key = request.session.session_key
    if session_key:
        UserSession.objects.filter(session_key=session_key).delete()
