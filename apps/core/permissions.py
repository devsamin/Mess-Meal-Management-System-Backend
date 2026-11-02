from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.core.models import MessSettings


def is_manager(user) -> bool:
    return bool(user and user.is_authenticated and getattr(user, "is_manager", False))


def own_member(user):
    return getattr(user, "member", None) if user and user.is_authenticated else None


def can_view_all(request) -> bool:
    """Managers, or everyone when the mess has transparency turned on."""
    return is_manager(request.user) or MessSettings.for_request(request).members_can_view_all


class IsManager(BasePermission):
    message = "Only a manager can do this."

    def has_permission(self, request, view):
        return is_manager(request.user)


class IsManagerOrReadOnly(BasePermission):
    message = "Only a manager can change this."

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        return request.method in SAFE_METHODS or is_manager(request.user)


class MemberVisibilityMixin:
    """Restrict list/detail querysets to the user's own rows when transparency is off.

    Subclasses set ``member_lookup`` to the path from the model to ``Member``.
    """

    member_lookup = "member"

    def restrict_to_own(self, queryset):
        user = self.request.user
        if getattr(self, "swagger_fake_view", False) or can_view_all(self.request):
            return queryset
        member = own_member(user)
        if member is None:
            return queryset.none()
        return queryset.filter(**{self.member_lookup: member})
