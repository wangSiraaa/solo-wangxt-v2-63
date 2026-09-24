"""Domain services - every state transition lives here, views stay thin."""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.contrib.gis.geos import Point
from django.db import transaction

from .clock import Clock
from .models import (CleaningContract, DuplicateCandidate, Event, PenaltyUnit,
                     Photo, Rectification, RoadGrid)
from .phash import hamming_distance, looks_similar


class DomainError(Exception):
    """Raised for business-rule violations; mapped to HTTP 409/400."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# geometry / lookup helpers
# ---------------------------------------------------------------------------

def point_from(lon: float, lat: float) -> Point:
    return Point(lon, lat, srid=4326)


def find_grid(location: Point) -> RoadGrid:
    grid = RoadGrid.objects.filter(area__contains=location).first()
    if grid is None:
        raise DomainError("拍摄位置不在任何已登记的道路网格内", status=400)
    return grid


def distance_meters(a: Point, b: Point) -> float:
    """Great-circle distance via PostGIS geography cast (meters)."""
    with _cursor() as cur:
        cur.execute(
            "SELECT ST_Distance(ST_GeogFromText(%s), ST_GeogFromText(%s))",
            [a.ewkt, b.ewkt],
        )
        return float(cur.fetchone()[0])


def _cursor():
    from django.db import connection

    return connection.cursor()


# ---------------------------------------------------------------------------
# photo ingestion + duplicate candidates
# ---------------------------------------------------------------------------

@transaction.atomic
def register_photo(*, image, phash: str, taken_at, location: Point,
                   category: str, kind: str = Photo.Kind.PROBLEM,
                   note: str = "", event: Event | None = None,
                   rectification_for: Event | None = None) -> Photo:
    """Store a photo and wire it into the event graph.

    pHash similarity is computed here but only *suggests* duplicates; the
    event linkage below follows location + time + human decisions.
    """
    grid = find_grid(location)

    if rectification_for is not None:
        if rectification_for.is_void:
            raise DomainError("不能为已合并作废的事件上传整改照片")
        event = rectification_for
        kind = Photo.Kind.RECTIFY

    if event is None:
        event = _resolve_event_for(category, grid, location, taken_at)

    photo = Photo.objects.create(
        image=image, phash=phash, taken_at=taken_at, location=location,
        grid=grid, event=event, kind=kind, note=note,
    )
    _spawn_candidates(photo)
    return photo


def _resolve_event_for(category: str, grid: RoadGrid, location: Point,
                       taken_at) -> Event:
    """Location + time decide which event a photo belongs to.

    * An OPEN event on the same grid, same category, within the spatial and
      temporal window absorbs the photo (same problem, possibly new angle).
    * A RECTIFIED event never absorbs: the problem came back, so a NEW event
      is opened (recurrence = new deduction).
    * Otherwise a fresh event is created and its base penalty unit issued.
    """
    window = timedelta(hours=settings.PHOTO_SAME_EVENT_HOURS)
    for candidate in Event.objects.filter(
        grid=grid, category=category, status=Event.Status.OPEN,
        merged_into__isnull=True,
        opened_at__gte=taken_at - window,
        opened_at__lte=taken_at + window,
    ):
        if distance_meters(candidate.location, location) <= settings.PHOTO_SAME_SPOT_METERS:
            return candidate
    return open_event(category=category, grid=grid, location=location,
                      occurred_at=taken_at)


@transaction.atomic
def open_event(*, category: str, grid: RoadGrid, location: Point,
               occurred_at) -> Event:
    event = Event.objects.create(
        category=category, location=location, grid=grid,
        opened_at=occurred_at,
    )
    _issue_base_penalty(event)
    return event


def _issue_base_penalty(event: Event) -> PenaltyUnit:
    contract = CleaningContract.liable_for(event.grid, event.opened_at)
    return PenaltyUnit.objects.create(
        event=event,
        level=PenaltyUnit.Level.BASE,
        reason=f"发现{event.category}问题",
        base_points=settings.BASE_POINTS.get(event.category,
                                             settings.DEFAULT_BASE_POINTS),
        rule_version="v1",
        liable_contract=contract,
        contractor=contract.contractor if contract else None,
    )


def _spawn_candidates(photo: Photo) -> None:
    """pHash look-alikes become candidates. Being look-alikes across two
    different locations is exactly what we must NOT merge - the candidate row
    records that suspicion for a human, nothing more."""
    others = (
        Photo.objects.exclude(pk=photo.pk)
        .exclude(phash="")
        .filter(taken_at__gte=photo.taken_at - timedelta(days=30),
                taken_at__lte=photo.taken_at + timedelta(days=30))
        .only("id", "phash", "taken_at", "location")
    )
    for other in others:
        if not looks_similar(photo.phash, other.phash):
            continue
        dist = distance_meters(photo.location, other.location)
        gap = abs((photo.taken_at - other.taken_at).total_seconds())
        DuplicateCandidate.objects.get_or_create(
            photo=photo, similar_photo=other,
            defaults=dict(
                hamming_distance=hamming_distance(photo.phash, other.phash),
                distance_meters=dist,
                time_gap_seconds=gap,
                within_spatial_window=dist <= settings.PHOTO_SAME_SPOT_METERS,
                within_temporal_window=gap <= settings.PHOTO_SAME_EVENT_HOURS * 3600,
            ),
        )


# ---------------------------------------------------------------------------
# human adjudication of candidates
# ---------------------------------------------------------------------------

@transaction.atomic
def decide_candidate(candidate: DuplicateCandidate, decision: str,
                     decided_by: str) -> DuplicateCandidate:
    if candidate.decision != DuplicateCandidate.Decision.PENDING:
        raise DomainError("该疑似重复候选已判定，判定结果不可更改")
    if decision == DuplicateCandidate.Decision.SAME_EVENT:
        if not candidate.is_same_spot_repeat:
            raise DomainError(
                "位置或时间超出同一现场窗口，不能判定为同一事件；"
                "相似照片不合并不同地点")
        _merge_events(candidate.photo.event, candidate.similar_photo.event)
    candidate.decision = decision
    candidate.decided_by = decided_by
    candidate.decided_at = Clock.now()
    candidate.save(update_fields=["decision", "decided_by", "decided_at"])
    return candidate


@transaction.atomic
def _merge_events(loser: Event, winner: Event) -> None:
    """Human-confirmed merge: the loser event is voided, its photos move to
    the winner and its penalty units are annulled (never double-counted)."""
    if loser.pk == winner.pk:
        return
    if loser.is_void or winner.is_void:
        raise DomainError("已合并的事件不能再次参与合并")
    if loser.status == Event.Status.RECTIFIED or winner.status == Event.Status.RECTIFIED:
        raise DomainError("已整改结案的事件不参与合并；整改后复发应作为新事件")
    Photo.objects.filter(event=loser).update(event=winner)
    for unit in PenaltyUnit.objects.filter(event=loser):
        if unit.is_locked:
            raise DomainError("待合并事件存在已锁定的处罚单元，需先追加更正撤销")
        unit.delete()  # provisional only - locked units are immutable
    loser.merged_into = winner
    loser.save(update_fields=["merged_into"])


# ---------------------------------------------------------------------------
# rectification callbacks (append-only)
# ---------------------------------------------------------------------------

@transaction.atomic
def record_rectification(event: Event, *, callback_at, note: str = "",
                         evidence: Photo | None = None) -> Rectification:
    if event.is_void:
        raise DomainError("事件已合并作废，不能登记整改")
    first = not event.rectifications.exists()
    rect = Rectification.objects.create(
        event=event, callback_at=callback_at, effective=first,
        evidence=evidence, note=note,
    )
    if first:
        event.status = Event.Status.RECTIFIED
        event.rectified_at = callback_at
        event.save(update_fields=["status", "rectified_at"])
    # Later callbacks are appended with effective=False - the event stays
    # rectified exactly once, no state is rewritten.
    return rect


# ---------------------------------------------------------------------------
# escalation - driven by the injectable clock
# ---------------------------------------------------------------------------

@transaction.atomic
def run_escalation(now=None) -> list[PenaltyUnit]:
    """Issue escalation penalty units for events still open past the ladder.

    Fully deterministic w.r.t. ``Clock``; safe to re-run (unique constraint
    per event+level makes it idempotent).
    """
    now = now or Clock.now()
    issued: list[PenaltyUnit] = []
    open_events = Event.objects.filter(
        status=Event.Status.OPEN, merged_into__isnull=True
    ).select_related("grid")
    for event in open_events:
        contract = CleaningContract.liable_for(event.grid, event.opened_at)
        age_hours = (now - event.opened_at).total_seconds() / 3600.0
        for idx, threshold in enumerate(settings.ESCALATION_LADDER_HOURS, start=1):
            if age_hours < threshold:
                break
            unit, created = PenaltyUnit.objects.get_or_create(
                event=event, level=idx,
                defaults=dict(
                    reason=f"逾期未整改超过{threshold}小时",
                    base_points=0,
                    escalation_points=settings.ESCALATION_POINTS_BY_LEVEL[idx],
                    rule_version="v1",
                    liable_contract=contract,
                    contractor=contract.contractor if contract else None,
                ),
            )
            if created:
                issued.append(unit)
    return issued


# ---------------------------------------------------------------------------
# review lock + append-only corrections
# ---------------------------------------------------------------------------

@transaction.atomic
def review_penalty(unit: PenaltyUnit, reviewer: str) -> PenaltyUnit:
    if unit.event.is_void:
        raise DomainError("事件已合并作废，其处罚单元不可复核")
    if unit.is_locked:
        raise DomainError("处罚单元已锁定，不能重复复核")
    unit.lock(reviewer)
    return unit
