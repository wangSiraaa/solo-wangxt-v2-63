"""Domain models for street sanitation inspection.

Core identity rules implemented here
------------------------------------
* ``Photo``        : evidence carrier, each with its own pHash + GPS + time.
* ``DuplicateCandidate`` : pHash pair that *looks* the same - a candidate only.
* ``Event``        : the deduplicated "one field problem". Recurrence after
                     rectification is a NEW event, never a reopened one.
* ``PenaltyUnit``  : the unique cell every deduction row points at. It is
                     attributed to whichever contract was liable **when the
                     event occurred** (``opened_at``), not when it was entered.
* ``PenaltyCorrection`` : append-only corrections; a reviewed (locked) unit
                     may never be edited or deleted.
"""
from __future__ import annotations

from django.contrib.gis.db import models as gis
from django.db import models

from .clock import Clock


class Contractor(models.Model):
    name = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(default=Clock.now)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return self.name


class CleaningContract(models.Model):
    """A contractor's liability interval over a set of road grids.

    Liability is evaluated at a *point in time* (event occurrence), hence the
    half-open ``[valid_from, valid_to)`` window; ``valid_to=None`` means open
    ended.
    """

    contractor = models.ForeignKey(Contractor, on_delete=models.PROTECT,
                                   related_name="contracts")
    grid = models.ForeignKey("RoadGrid", on_delete=models.PROTECT,
                             related_name="contracts")
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=Clock.now)

    class Meta:
        ordering = ["grid_id", "valid_from"]
        indexes = [models.Index(fields=["grid", "valid_from"])]

    def is_active_on(self, moment) -> bool:
        if moment < self.valid_from:
            return False
        return self.valid_to is None or moment < self.valid_to

    @classmethod
    def liable_for(cls, grid, moment):
        """The contract/contractor liable on ``grid`` at ``moment``."""
        return (
            cls.objects.filter(grid=grid, valid_from__lte=moment)
            .filter(models.Q(valid_to__isnull=True) | models.Q(valid_to__gt=moment))
            .select_related("contractor")
            .first()
        )


class RoadGrid(gis.Model):
    code = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    area = gis.PolygonField(srid=4326)
    created_at = models.DateTimeField(default=Clock.now)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class Event(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "待整改"
        RECTIFIED = "rectified", "已整改"

    category = models.CharField(max_length=64)
    location = gis.PointField(srid=4326)
    grid = models.ForeignKey(RoadGrid, on_delete=models.PROTECT,
                             related_name="events")
    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.OPEN)
    opened_at = models.DateTimeField()          # occurrence time (from photo)
    rectified_at = models.DateTimeField(null=True, blank=True)
    # Merged-in duplicate events point at their survivor; the penalty chain
    # only ever follows surviving events, so one problem is scored once.
    merged_into = models.ForeignKey("self", null=True, blank=True,
                                    on_delete=models.PROTECT,
                                    related_name="merged_duplicates")
    created_at = models.DateTimeField(default=Clock.now)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["opened_at"]),
            models.Index(fields=["merged_into"]),
        ]

    @property
    def is_void(self) -> bool:
        return self.merged_into_id is not None

    def surviving(self) -> "Event":
        return self.merged_into if self.merged_into_id else self


class Photo(models.Model):
    class Kind(models.TextChoices):
        PROBLEM = "problem", "问题照片"
        RECTIFY = "rectify", "整改照片"

    image = models.ImageField(upload_to="photos/%Y/%m/%d/")
    phash = models.CharField(max_length=64, db_index=True)
    taken_at = models.DateTimeField()           # when the shutter fired
    location = gis.PointField(srid=4326)        # where it was actually taken
    grid = models.ForeignKey(RoadGrid, on_delete=models.PROTECT,
                             related_name="photos")
    event = models.ForeignKey(Event, on_delete=models.PROTECT,
                              related_name="photos")
    kind = models.CharField(max_length=16, choices=Kind.choices,
                            default=Kind.PROBLEM)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=Clock.now)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["taken_at"])]


class DuplicateCandidate(models.Model):
    """A pHash-similar photo pair - suspicion, never a verdict."""

    class Decision(models.TextChoices):
        PENDING = "pending", "待人工判定"
        SAME_EVENT = "same_event", "确认同一事件(不同角度)"
        DIFFERENT_PLACE = "different_place", "相似但不同地点"

    photo = models.ForeignKey(Photo, on_delete=models.PROTECT,
                              related_name="candidate_pairs")
    similar_photo = models.ForeignKey(Photo, on_delete=models.PROTECT,
                                      related_name="candidate_pairs_reverse")
    hamming_distance = models.PositiveSmallIntegerField()
    distance_meters = models.FloatField()
    time_gap_seconds = models.FloatField()
    within_spatial_window = models.BooleanField()
    within_temporal_window = models.BooleanField()
    decision = models.CharField(max_length=20, choices=Decision.choices,
                                default=Decision.PENDING)
    decided_by = models.CharField(max_length=100, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=Clock.now)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["photo", "similar_photo"],
                name="uniq_candidate_photo_pair",
            )
        ]

    @property
    def is_same_spot_repeat(self) -> bool:
        return self.within_spatial_window and self.within_temporal_window


class Rectification(models.Model):
    """Rectification callback. Multiple callbacks are APPENDED - the first
    successful one closes the event, later ones just document repeated calls.
    """

    event = models.ForeignKey(Event, on_delete=models.PROTECT,
                              related_name="rectifications")
    callback_at = models.DateTimeField()
    effective = models.BooleanField()          # True only for the first
    evidence = models.ForeignKey(Photo, null=True, blank=True,
                                 on_delete=models.PROTECT,
                                 related_name="rectification_evidence")
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=Clock.now)

    class Meta:
        ordering = ["id"]


class PenaltyUnit(models.Model):
    """The unique, auditable cell behind every deduction.

    One unit per (surviving event, escalation level). Locked via ``reviewed_at``
    once a reviewer approves it; afterwards nothing may be mutated, corrections
    can only be appended as ``PenaltyCorrection`` rows.
    """

    class Level(models.IntegerChoices):
        BASE = 0, "基础扣分"
        L1 = 1, "逾期一级(24h)"
        L2 = 2, "逾期二级(48h)"
        L3 = 3, "逾期三级(72h)"

    class Status(models.TextChoices):
        PROVISIONAL = "provisional", "待复核"
        LOCKED = "locked", "复核锁定"
        APPEALED = "appealed", "申诉更正"

    event = models.ForeignKey(Event, on_delete=models.PROTECT,
                              related_name="penalty_units")
    level = models.PositiveSmallIntegerField(choices=Level.choices)
    reason = models.CharField(max_length=300)
    base_points = models.IntegerField()
    escalation_points = models.IntegerField(default=0)
    # Snapshot of the penalty RULE VERSION that produced this unit, so later
    # rule changes never silently rewrite already-scored deductions.
    rule_version = models.CharField(max_length=32, default="v1")
    # Attribution snapshot - fixed at creation from the contract that was
    # liable at ``event.opened_at`` (occurrence, not data entry time).
    liable_contract = models.ForeignKey(CleaningContract, null=True,
                                        on_delete=models.PROTECT,
                                        related_name="penalty_units")
    contractor = models.ForeignKey(Contractor, null=True,
                                   on_delete=models.PROTECT,
                                   related_name="penalty_units")
    contractor_locked = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices,
                              default=Status.PROVISIONAL)
    issued_at = models.DateTimeField(default=Clock.now)
    reviewed_by = models.CharField(max_length=100, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "level"],
                name="uniq_penalty_event_level",
            )
        ]

    @property
    def points(self) -> int:
        return self.base_points + self.escalation_points

    @property
    def is_locked(self) -> bool:
        return self.reviewed_at is not None

    def lock(self, reviewer: str) -> None:
        if self.is_locked:
            raise ValueError("处罚单元已锁定")
        self.reviewed_by = reviewer
        self.reviewed_at = Clock.now()
        self.contractor_locked = True
        self.status = self.Status.LOCKED
        self.save(update_fields=["reviewed_by", "reviewed_at",
                                 "contractor_locked", "status"])


class PenaltyCorrection(models.Model):
    """Append-only correction / appeal adjustment for a (usually locked) unit."""

    unit = models.ForeignKey(PenaltyUnit, on_delete=models.PROTECT,
                             related_name="corrections")
    delta_points = models.IntegerField(
        help_text="正数追加扣分，负数退还/撤销扣分，最终扣分=原值+所有更正之和")
    reason = models.CharField(max_length=500)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(default=Clock.now)

    class Meta:
        ordering = ["id"]
