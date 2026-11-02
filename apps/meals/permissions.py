from datetime import date, timedelta

from django.utils import timezone

from apps.core.models import MessSettings
from apps.core.permissions import is_manager, own_member


def meal_edit_denial(user, member, day: date, account, settings: MessSettings | None = None) -> str | None:
    """Return why ``user`` may not change ``member``'s meal on ``day`` (None = allowed).

    Pass ``settings`` when checking many rows so the settings row is read once.
    """
    if account.is_closed:
        return f"{account.label} is closed."
    if is_manager(user):
        return None
    settings = settings or MessSettings.load()
    if not settings.allow_member_meal_entry:
        return "Meal entry by members is turned off. Ask the manager."
    own = own_member(user)
    if own is None or own.pk != getattr(member, "pk", member):
        return "You can only enter your own meals."
    earliest = timezone.localdate() - timedelta(days=settings.member_meal_edit_days)
    if day < earliest:
        return "This date is locked. Ask the manager to change past meals."
    return None
