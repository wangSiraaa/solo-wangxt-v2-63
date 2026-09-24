"""DRF serializers. GeoJSON fields are hand-rolled so the API does not need
the extra djangorestframework-gis package."""
from __future__ import annotations

import json

from django.contrib.gis import geos
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers


def _geojson_schema(geom_type: str):
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": [geom_type]},
            "coordinates": {"type": "array",
                            "description": "GeoJSON 坐标，SRID 4326"},
        },
        "required": ["type", "coordinates"],
    }


POINT_SCHEMA = _geojson_schema("Point")
POLYGON_SCHEMA = _geojson_schema("Polygon")

from .models import (CleaningContract, Contractor, DuplicateCandidate, Event,
                     PenaltyCorrection, PenaltyUnit, Photo, Rectification,
                     RoadGrid)


# ---------------------------------------------------------------------------
# geometry fields
# ---------------------------------------------------------------------------

class _GeoJSONField(serializers.Field):
    geom_type = "Geometry"

    def to_representation(self, value):
        if value is None:
            return None
        return json.loads(value.geojson)

    def to_internal_value(self, data):
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict) or data.get("type") != self.geom_type:
            raise serializers.ValidationError(
                f"需要 GeoJSON {self.geom_type} 对象")
        geom = geos.GEOSGeometry(json.dumps(data))
        geom.srid = 4326
        return geom


@extend_schema_field(POINT_SCHEMA)
class PointField(_GeoJSONField):
    geom_type = "Point"


@extend_schema_field(POLYGON_SCHEMA)
class PolygonField(_GeoJSONField):
    geom_type = "Polygon"


# ---------------------------------------------------------------------------
# reference data
# ---------------------------------------------------------------------------

class ContractorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Contractor
        fields = ["id", "name", "created_at"]
        read_only_fields = ["created_at"]


class ContractSerializer(serializers.ModelSerializer):
    contractor_name = serializers.CharField(source="contractor.name",
                                            read_only=True)
    grid_code = serializers.CharField(source="grid.code", read_only=True)

    class Meta:
        model = CleaningContract
        fields = ["id", "contractor", "contractor_name", "grid", "grid_code",
                  "valid_from", "valid_to", "created_at"]
        read_only_fields = ["created_at"]

    def validate(self, attrs):
        valid_from = attrs.get("valid_from")
        valid_to = attrs.get("valid_to")
        if valid_from and valid_to and valid_to <= valid_from:
            raise serializers.ValidationError("valid_to 必须晚于 valid_from")
        return attrs


class RoadGridSerializer(serializers.ModelSerializer):
    area = PolygonField()

    class Meta:
        model = RoadGrid
        fields = ["id", "code", "name", "area", "created_at"]
        read_only_fields = ["created_at"]


# ---------------------------------------------------------------------------
# photos / events / candidates
# ---------------------------------------------------------------------------

class PhotoUploadSerializer(serializers.Serializer):
    image = serializers.ImageField(required=True)
    lon = serializers.FloatField(min_value=-180, max_value=180)
    lat = serializers.FloatField(min_value=-90, max_value=90)
    taken_at = serializers.DateTimeField()
    category = serializers.CharField(max_length=64, required=False,
                                     default="garbage_overflow")
    kind = serializers.ChoiceField(choices=Photo.Kind.choices,
                                   default=Photo.Kind.PROBLEM)
    note = serializers.CharField(max_length=500, required=False,
                                 allow_blank=True, default="")
    # Explicit linkage bypasses auto-resolution (e.g. attach rectify photo).
    event = serializers.PrimaryKeyRelatedField(
        queryset=Event.objects.all(), required=False)


class PhotoSerializer(serializers.ModelSerializer):
    location = PointField(read_only=True)
    grid_code = serializers.CharField(source="grid.code", read_only=True)

    class Meta:
        model = Photo
        fields = ["id", "image", "phash", "taken_at", "location", "grid",
                  "grid_code", "event", "kind", "note", "created_at"]


class EventSerializer(serializers.ModelSerializer):
    location = PointField(read_only=True)
    grid_code = serializers.CharField(source="grid.code", read_only=True)
    photo_ids = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)
    merged_into = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Event
        fields = ["id", "category", "location", "grid", "grid_code",
                  "status", "status_display", "opened_at", "rectified_at",
                  "merged_into", "photo_ids", "created_at"]

    def get_photo_ids(self, obj) -> list[int]:
        return list(obj.photos.values_list("id", flat=True))


class DuplicateCandidateSerializer(serializers.ModelSerializer):
    photo = PhotoSerializer(read_only=True)
    similar_photo = PhotoSerializer(read_only=True)
    decision_display = serializers.CharField(source="get_decision_display",
                                             read_only=True)
    same_spot_repeat = serializers.BooleanField(source="is_same_spot_repeat",
                                                read_only=True)

    class Meta:
        model = DuplicateCandidate
        fields = ["id", "photo", "similar_photo", "hamming_distance",
                  "distance_meters", "time_gap_seconds",
                  "within_spatial_window", "within_temporal_window",
                  "same_spot_repeat", "decision", "decision_display",
                  "decided_by", "decided_at", "created_at"]
        read_only_fields = fields


class CandidateDecisionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=DuplicateCandidate.Decision.choices)
    decided_by = serializers.CharField(max_length=100, default="reviewer")


# ---------------------------------------------------------------------------
# rectification / penalties
# ---------------------------------------------------------------------------

class RectificationCreateSerializer(serializers.Serializer):
    callback_at = serializers.DateTimeField(required=False)
    note = serializers.CharField(max_length=500, required=False,
                                 allow_blank=True, default="")
    evidence = serializers.ImageField(required=False)
    lon = serializers.FloatField(required=False)
    lat = serializers.FloatField(required=False)
    taken_at = serializers.DateTimeField(required=False)


class RectificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Rectification
        fields = ["id", "event", "callback_at", "effective", "evidence",
                  "note", "created_at"]
        read_only_fields = fields


class PenaltyCorrectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PenaltyCorrection
        fields = ["id", "unit", "delta_points", "reason", "created_by",
                  "created_at"]
        read_only_fields = ["id", "unit", "created_at"]


class PenaltyUnitSerializer(serializers.ModelSerializer):
    total_points = serializers.IntegerField(source="points", read_only=True)
    level_display = serializers.CharField(source="get_level_display",
                                          read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)
    corrections = PenaltyCorrectionSerializer(many=True, read_only=True)
    corrected_points = serializers.SerializerMethodField()
    evidence_photo_ids = serializers.SerializerMethodField()
    event_merged_void = serializers.BooleanField(
        source="event.is_void", read_only=True)

    class Meta:
        model = PenaltyUnit
        fields = ["id", "event", "level", "level_display", "reason",
                  "base_points", "escalation_points", "total_points",
                  "rule_version", "liable_contract", "contractor",
                  "contractor_locked", "status", "status_display",
                  "issued_at", "reviewed_by", "reviewed_at",
                  "corrections", "corrected_points",
                  "evidence_photo_ids", "event_merged_void"]
        read_only_fields = fields

    def get_corrected_points(self, obj) -> int:
        return obj.points + sum(c.delta_points for c in obj.corrections.all())

    def get_evidence_photo_ids(self, obj) -> list[int]:
        return list(obj.event.photos.values_list("id", flat=True))


class ReviewSerializer(serializers.Serializer):
    reviewer = serializers.CharField(max_length=100, default="reviewer")


class ClockSetSerializer(serializers.Serializer):
    now = serializers.DateTimeField(required=False)
    reset = serializers.BooleanField(required=False, default=False)
