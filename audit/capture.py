"""Turns model saves and deletes into AuditLog rows.

Deliberately fail-open: a bug here must never take a sale, a payment or a login
down with it. Every write is wrapped in its own savepoint and any error is
logged (and reaches Sentry) instead of raised — the trade is a possible gap in
the history, never a failed business action.

Known limits, by design of the ORM rather than a choice made here:
  * QuerySet.update() and bulk_create() emit no signals, so those writes are
    not captured. Call sites that matter (price tiers) save row by row.
  * Many-to-many changes are not tracked (the project uses none on audited
    models; allowed_modules is a JSON field and is tracked normally).
"""
import logging
from datetime import date, datetime, time, timezone as dt_timezone
from decimal import Decimal
from uuid import UUID

from django.apps import apps
from django.conf import settings
from django.db import models, transaction
from django.db.models.signals import post_delete, post_save, pre_save

from . import context
from .models import AuditLog
from .registry import GLOBAL_EXCLUDE, MASK_FILE, MASK_PERSONAL, REGISTRY

logger = logging.getLogger(__name__)

MAX_TEXT = 300
SUMMARY_VALUE = 40

_tracked_cache = {}


def _enabled():
    return getattr(settings, 'AUDIT_ENABLED', True) and not context.is_suppressed()


def _tracked_fields(model, cfg):
    """The concrete fields this model's rows are tracked on, keyed by attname."""
    cached = _tracked_cache.get(model)
    if cached is None:
        cached = {
            f.attname: f for f in model._meta.concrete_fields
            if f.name not in GLOBAL_EXCLUDE and f.name not in cfg.exclude
        }
        _tracked_cache[model] = cached
    return cached


def _keys_for(model, cfg, update_fields):
    """Which tracked attnames a save could have changed."""
    tracked = _tracked_fields(model, cfg)
    if update_fields is None:
        return list(tracked)
    keys = []
    for name in update_fields:
        try:
            attname = model._meta.get_field(name).attname
        except Exception:
            continue
        if attname in tracked:
            keys.append(attname)
    return keys


def _normalise(field, value):
    """A JSON-safe, comparable form of a field value. Both sides of a diff go
    through this, so 100, '100' and Decimal('100.00') don't read as changes."""
    if value is None:
        return None
    if isinstance(field, models.FileField):
        return getattr(value, 'name', None) or None
    try:
        if isinstance(field, models.DecimalField):
            return f'{field.to_python(value):.{field.decimal_places}f}'
        if isinstance(field, models.JSONField):
            return value
        value = field.to_python(value)
    except Exception:
        pass
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt_timezone.utc)
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return '[binary]'
    return value


def _snapshot(instance, keys, cfg):
    fields = _tracked_fields(type(instance), cfg)
    deferred = instance.get_deferred_fields()
    return {
        key: _normalise(fields[key], getattr(instance, key))
        for key in keys if key not in deferred
    }


def _present(model, cfg, key, value):
    """What actually gets written for one field value."""
    if value in (None, ''):
        return value
    field = _tracked_fields(model, cfg)[key]
    if isinstance(field, models.FileField):
        return MASK_FILE
    if field.name in cfg.mask:
        return MASK_PERSONAL
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + '...'
    return value


def _present_all(model, cfg, snapshot, keys=None):
    return {k: _present(model, cfg, k, snapshot[k]) for k in (keys or snapshot)}


def _branch_id(instance, cfg):
    if not cfg.branch_path:
        return None
    obj = instance
    try:
        for part in cfg.branch_path.split('.'):
            obj = getattr(obj, part)
    except Exception:
        return None
    return obj if isinstance(obj, int) else None


def _record_repr(instance, cfg):
    if cfg.repr_field:
        value = getattr(instance, cfg.repr_field, '')
        if value:
            return str(value)[:255]
    return f'{cfg.label} #{instance.pk}'


def _short(value):
    text = str(value)
    return text if len(text) <= SUMMARY_VALUE else text[:SUMMARY_VALUE] + '...'


def _summary(cfg, action, repr_, model, changed_before=None, changed_after=None):
    verb = {'create': 'Created', 'update': 'Updated', 'delete': 'Deleted'}[action]
    text = f'{verb} {cfg.label} {repr_}'
    if action != 'update':
        return text[:500]
    parts = []
    for key in changed_after:
        name = _tracked_fields(model, cfg)[key].name
        before, after = changed_before[key], changed_after[key]
        if MASK_PERSONAL in (before, after) or MASK_FILE in (before, after):
            parts.append(f'{name} changed')
        else:
            parts.append(f'{name} {_short(before)} -> {_short(after)}')
    return f'{text}: {"; ".join(parts)}'[:500]


def _write(sender, cfg, instance, action, before, after, summary):
    who = context.current()
    entry = AuditLog(
        action=action,
        entity=sender._meta.label,
        record_id=str(instance.pk),
        record_repr=_record_repr(instance, cfg),
        branch_id=_branch_id(instance, cfg),
        summary=summary,
        before=before,
        after=after,
        **who,
    )
    try:
        # Its own savepoint: a failed insert must not poison the surrounding
        # business transaction.
        with transaction.atomic():
            entry.save(force_insert=True)
    except Exception:
        logger.exception('Audit log write failed for %s #%s', sender._meta.label, instance.pk)


def _config(sender):
    return REGISTRY.get(sender._meta.label)


# --- signal handlers ---------------------------------------------------------

def on_pre_save(sender, instance, raw=False, update_fields=None, **kwargs):
    instance.__dict__.pop('_audit_before', None)
    if raw or not _enabled() or instance._state.adding or instance.pk is None:
        return
    cfg = _config(sender)
    keys = _keys_for(sender, cfg, update_fields)
    if not keys:
        return
    try:
        row = sender._base_manager.filter(pk=instance.pk).first()
        if row is not None:
            instance._audit_before = _snapshot(row, keys, cfg)
    except Exception:
        logger.exception('Audit pre-save read failed for %s #%s', sender._meta.label, instance.pk)


def on_post_save(sender, instance, created, raw=False, update_fields=None, **kwargs):
    before = instance.__dict__.pop('_audit_before', None)
    if raw or not _enabled():
        return
    cfg = _config(sender)
    try:
        if created:
            keys = _keys_for(sender, cfg, None)
            after = _present_all(sender, cfg, _snapshot(instance, keys, cfg))
            _write(sender, cfg, instance, 'create', None, after,
                   _summary(cfg, 'create', _record_repr(instance, cfg), sender))
            return
        if before is None:
            return
        keys = [k for k in _keys_for(sender, cfg, update_fields) if k in before]
        after_all = _snapshot(instance, keys, cfg)
        changed = [k for k in keys if before[k] != after_all.get(k)]
        if not changed:
            return
        before_out = _present_all(sender, cfg, before, changed)
        after_out = _present_all(sender, cfg, after_all, changed)
        _write(sender, cfg, instance, 'update', before_out, after_out,
               _summary(cfg, 'update', _record_repr(instance, cfg), sender,
                        before_out, after_out))
    except Exception:
        logger.exception('Audit post-save failed for %s #%s', sender._meta.label, instance.pk)


def on_post_delete(sender, instance, **kwargs):
    if not _enabled():
        return
    cfg = _config(sender)
    try:
        keys = _keys_for(sender, cfg, None)
        before = _present_all(sender, cfg, _snapshot(instance, keys, cfg))
        _write(sender, cfg, instance, 'delete', before, None,
               _summary(cfg, 'delete', _record_repr(instance, cfg), sender))
    except Exception:
        logger.exception('Audit post-delete failed for %s #%s', sender._meta.label, instance.pk)


def connect():
    """Attach the handlers to every registered model (called from AppConfig.ready)."""
    for label in REGISTRY:
        model = apps.get_model(label)
        uid = f'audit:{label}'
        pre_save.connect(on_pre_save, sender=model, dispatch_uid=f'{uid}:pre_save')
        post_save.connect(on_post_save, sender=model, dispatch_uid=f'{uid}:post_save')
        post_delete.connect(on_post_delete, sender=model, dispatch_uid=f'{uid}:post_delete')
