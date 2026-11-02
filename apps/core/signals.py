"""Delete cached responses whenever the data behind them changes.

Rows that belong to one month invalidate that month's tag. Rows that can change what *every*
month's report shows (member names, categories, settings, user roles/names) invalidate everything.
Writes that bypass signals (``bulk_create``/``bulk_update``/``QuerySet.update``) must call
``cache.invalidate_on_commit`` themselves; see ``MealRecordViewSet._apply_entries``.
"""

from django.apps import apps
from django.conf import settings
from django.db.models.signals import post_delete, post_save

from apps.core.cache import ALL, cache, month_tag

MONTH_MODELS = ["months.MonthlyAccount", "months.MonthMember", "meals.MealRecord", "finance.Expense",
                "finance.Deposit", "finance.ExtraBill", "core.ActivityLog"]
GLOBAL_MODELS = ["members.Member", "finance.ExpenseCategory", "core.MessSettings"]

# Fields a login or password change touches; they never appear in cached responses.
QUIET_USER_FIELDS = frozenset({"last_login", "password"})


def month_changed(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    month_id = instance.pk if sender._meta.label == "months.MonthlyAccount" else getattr(instance, "account_id", None)
    if month_id is not None:
        cache.invalidate_on_commit(month_tag(month_id))


def everything_changed(sender, instance, **kwargs):
    if kwargs.get("raw"):
        return
    cache.invalidate_on_commit(ALL)


def user_changed(sender, instance, **kwargs):
    fields = kwargs.get("update_fields")
    if fields is not None and set(fields) <= QUIET_USER_FIELDS:
        return  # login / password change: nothing cached depends on it
    everything_changed(sender, instance, **kwargs)


def connect() -> None:
    wiring = [(label, month_changed) for label in MONTH_MODELS]
    wiring += [(label, everything_changed) for label in GLOBAL_MODELS]
    wiring.append((settings.AUTH_USER_MODEL, user_changed))
    for label, handler in wiring:
        model = apps.get_model(label)
        post_save.connect(handler, sender=model, dispatch_uid=f"cache-save-{label}")
        post_delete.connect(handler, sender=model, dispatch_uid=f"cache-delete-{label}")
