"""URL routing and OpenAPI schema endpoints (no UI)."""
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView
from rest_framework.routers import DefaultRouter

from .views import (ClockViewSet, ContractorViewSet, ContractViewSet,
                    DuplicateCandidateViewSet, EscalationView,
                    EventViewSet, PenaltyUnitViewSet, PhotoViewSet,
                    RoadGridViewSet)

router = DefaultRouter()
router.register("contractors", ContractorViewSet)
router.register("contracts", ContractViewSet)
router.register("grids", RoadGridViewSet)
router.register("photos", PhotoViewSet)
router.register("events", EventViewSet, basename="event")
router.register("duplicate-candidates", DuplicateCandidateViewSet,
                basename="duplicatecandidate")
router.register("penalty-units", PenaltyUnitViewSet, basename="penaltyunit")
router.register("escalations", EscalationView, basename="escalation")
router.register("clock", ClockViewSet, basename="clock")

urlpatterns = [
    path("api/", include(router.urls)),
    path("api/schema/", SpectacularAPIView.as_view(), name="openapi-schema"),
]
