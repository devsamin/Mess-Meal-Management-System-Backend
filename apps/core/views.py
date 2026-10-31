from rest_framework import generics, mixins, viewsets
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated

from apps.core.models import ActivityLog, MessSettings
from apps.core.permissions import IsManager, is_manager
from apps.core.serializers import ActivityLogSerializer, MessSettingsSerializer


class MessSettingsView(generics.RetrieveUpdateAPIView):
    serializer_class = MessSettingsSerializer

    def get_permissions(self):
        return [IsAuthenticated()] if self.request.method in SAFE_METHODS else [IsManager()]

    def get_object(self):
        return MessSettings.load()


class ActivityLogViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = ActivityLogSerializer
    filterset_fields = {"account": ["exact"], "entity_type": ["exact"], "action": ["exact"]}

    def get_queryset(self):
        qs = ActivityLog.objects.select_related("actor")
        if getattr(self, "swagger_fake_view", False) or is_manager(self.request.user) or MessSettings.load().members_can_view_all:
            return qs
        return qs.filter(actor=self.request.user)
