from apps.core.models import ActivityLog


def log_activity(actor, action: str, instance=None, summary: str = "", account=None, entity_type: str | None = None):
    """Record a human-readable audit trail entry (shown on the dashboard)."""
    if account is None and instance is not None:
        account = getattr(instance, "account", None)
    if account is not None and account.__class__.__name__ != "MonthlyAccount":
        account = None
    ActivityLog.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        account=account,
        action=action,
        entity_type=entity_type or (instance.__class__.__name__.lower() if instance is not None else ""),
        entity_id=getattr(instance, "pk", None),
        summary=summary[:255],
    )
