"""API views. All business rules live in ``services``; views do I/O only."""
from __future__ import annotations

from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .clock import Clock
from .models import (CleaningContract, Contractor, DuplicateCandidate, Event,
                     PenaltyCorrection, PenaltyUnit, Photo, Rectification,
                     RoadGrid)
from .phash import phash_from_bytes
from . import services
from .serializers import (CandidateDecisionSerializer,
                          ClockSetSerializer, ContractSerializer,
                          ContractorSerializer, DuplicateCandidateSerializer,
                          EventSerializer, PenaltyCorrectionSerializer,
                          PenaltyUnitSerializer, PhotoSerializer,
                          PhotoUploadSerializer, RectificationCreateSerializer,
                          RectificationSerializer, ReviewSerializer,
                          RoadGridSerializer)
from .services import DomainError


def _domain_error(exc: DomainError) -> Response:
    return Response({"detail": str(exc)}, status=exc.status)


class ContractorViewSet(viewsets.ModelViewSet):
    queryset = Contractor.objects.all()
    serializer_class = ContractorSerializer


class ContractViewSet(viewsets.ModelViewSet):
    queryset = CleaningContract.objects.select_related(
        "contractor", "grid").all()
    serializer_class = ContractSerializer


class RoadGridViewSet(viewsets.ModelViewSet):
    queryset = RoadGrid.objects.all()
    serializer_class = RoadGridSerializer


class PhotoViewSet(viewsets.ModelViewSet):
    http_method_names = ["get", "post", "head", "options"]
    queryset = Photo.objects.select_related("grid", "event").all()
    serializer_class = PhotoSerializer

    @transaction.atomic
    @extend_schema(request=PhotoUploadSerializer, responses=PhotoSerializer)
    def create(self, request, *args, **kwargs):
        serializer = PhotoUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        image_file = data["image"]
        image_bytes = image_file.read()
        image_file.seek(0)
        location = services.point_from(data["lon"], data["lat"])
        try:
            photo = services.register_photo(
                image=image_file,
                phash=phash_from_bytes(image_bytes),
                taken_at=data["taken_at"],
                location=location,
                category=data["category"],
                kind=data["kind"],
                note=data["note"],
                event=data.get("event"),
            )
        except DomainError as exc:
            return _domain_error(exc)
        out = PhotoSerializer(photo, context=self.get_serializer_context())
        return Response(out.data, status=status.HTTP_201_CREATED)


class EventViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Event.objects.select_related("grid").all()
    serializer_class = EventSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        for field in ("status", "category", "grid"):
            value = self.request.query_params.get(field)
            if value:
                qs = qs.filter(**{field: value})
        return qs

    @action(detail=True, methods=["post"], url_path="rectify")
    @extend_schema(request=RectificationCreateSerializer,
                   responses=RectificationSerializer)
    def rectify(self, request, pk=None):
        """Record a rectification callback. Callbacks are append-only; only
        the first closes the event, repeated callbacks stay effective=false."""
        event = self.get_object()
        serializer = RectificationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        evidence = None
        if "evidence" in data and data["evidence"]:
            image_file = data["evidence"]
            raw = image_file.read()
            image_file.seek(0)
            try:
                evidence = services.register_photo(
                    image=image_file,
                    phash=phash_from_bytes(raw),
                    taken_at=data.get("taken_at")
                    or data.get("callback_at") or Clock.now(),
                    location=services.point_from(data["lon"], data["lat"]),
                    category=event.category,
                    kind=Photo.Kind.RECTIFY,
                    rectification_for=event,
                )
            except DomainError as exc:
                return _domain_error(exc)

        try:
            rect = services.record_rectification(
                event,
                callback_at=data.get("callback_at") or Clock.now(),
                note=data.get("note", ""),
                evidence=evidence,
            )
        except DomainError as exc:
            return _domain_error(exc)
        return Response(RectificationSerializer(rect).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="rectifications")
    def rectifications(self, request, pk=None):
        event = self.get_object()
        qs = event.rectifications.all()
        return Response(RectificationSerializer(qs, many=True).data)

    @action(detail=True, methods=["get"], url_path="penalty-units")
    def penalty_units(self, request, pk=None):
        event = self.get_object()
        qs = event.penalty_units.prefetch_related("corrections").all()
        return Response(PenaltyUnitSerializer(qs, many=True).data)


class DuplicateCandidateViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = DuplicateCandidate.objects.select_related(
        "photo", "similar_photo", "photo__event", "similar_photo__event"
    ).all()
    serializer_class = DuplicateCandidateSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        decision = self.request.query_params.get("decision")
        if decision:
            qs = qs.filter(decision=decision)
        same_spot = self.request.query_params.get("same_spot_only")
        if same_spot in ("1", "true"):
            qs = qs.filter(within_spatial_window=True,
                           within_temporal_window=True)
        return qs

    @action(detail=True, methods=["post"], url_path="decide")
    @extend_schema(request=CandidateDecisionSerializer,
                   responses=DuplicateCandidateSerializer)
    def decide(self, request, pk=None):
        """Human verdict: same_event merges; different_place keeps them apart
        even if the photos are visually identical."""
        candidate = self.get_object()
        serializer = CandidateDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.decide_candidate(
                candidate,
                serializer.validated_data["decision"],
                serializer.validated_data["decided_by"],
            )
        except DomainError as exc:
            return _domain_error(exc)
        return Response(DuplicateCandidateSerializer(candidate).data)


class PenaltyUnitViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = PenaltyUnit.objects.select_related(
        "event", "contractor", "liable_contract__contractor"
    ).prefetch_related("corrections").all()
    serializer_class = PenaltyUnitSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        contractor = self.request.query_params.get("contractor")
        if contractor:
            qs = qs.filter(contractor_id=contractor)
        return qs

    @action(detail=True, methods=["post"], url_path="review")
    @extend_schema(request=ReviewSerializer, responses=PenaltyUnitSerializer)
    def review(self, request, pk=None):
        unit = self.get_object()
        serializer = ReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.review_penalty(unit, serializer.validated_data["reviewer"])
        except DomainError as exc:
            return _domain_error(exc)
        return Response(PenaltyUnitSerializer(unit).data)

    @action(detail=True, methods=["post"], url_path="corrections")
    @extend_schema(request=PenaltyCorrectionSerializer,
                   responses=PenaltyCorrectionSerializer)
    def add_correction(self, request, pk=None):
        """Append a correction. The unit itself is never rewritten even after
        locking; the corrected total = original + sum(corrections)."""
        unit = self.get_object()
        serializer = PenaltyCorrectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        correction = PenaltyCorrection.objects.create(
            unit=unit,
            delta_points=serializer.validated_data["delta_points"],
            reason=serializer.validated_data["reason"],
            created_by=serializer.validated_data["created_by"],
        )
        if unit.is_locked:
            unit.status = PenaltyUnit.Status.APPEALED
            unit.save(update_fields=["status"])
        return Response(PenaltyCorrectionSerializer(correction).data,
                        status=status.HTTP_201_CREATED)


class EscalationView(viewsets.ViewSet):
    """Run the SLA escalation sweep against the (injectable) clock."""

    @extend_schema(
        request=None,
        responses={200: OpenApiTypes.OBJECT},
        description="按当前(可注入)时钟执行逾期升级扫描；幂等，可重复运行。")
    def list(self, request):
        units = services.run_escalation()
        return Response({
            "clock_now": Clock.now(),
            "issued_count": len(units),
            "units": PenaltyUnitSerializer(units, many=True).data,
        })

    @extend_schema(
        request=None,
        responses={201: OpenApiTypes.OBJECT},
        description="同 GET：触发一次逾期升级扫描。")
    def create(self, request):
        units = services.run_escalation()
        return Response({
            "clock_now": Clock.now(),
            "issued_count": len(units),
            "units": PenaltyUnitSerializer(units, many=True).data,
        }, status=status.HTTP_201_CREATED)


class ClockViewSet(viewsets.ViewSet):
    """Test seam: pin / reset the clock that drives SLA escalation."""

    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    def list(self, request):
        return Response({"now": Clock.now(), "pinned": Clock._override is not None})

    @extend_schema(request=ClockSetSerializer,
                   responses=OpenApiTypes.OBJECT)
    def create(self, request):
        serializer = ClockSetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data.get("reset"):
            Clock.reset()
        elif serializer.validated_data.get("now"):
            Clock.set(serializer.validated_data["now"])
        else:
            Clock.reset()
        return Response({"now": Clock.now(), "pinned": Clock._override is not None})
