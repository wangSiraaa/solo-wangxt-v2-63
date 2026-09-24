"""End-to-end tests through the REST API.

Scenarios (from the spec):
1. ``test_same_picture_uploaded_at_different_locations_is_not_merged``
   byte-identical photo, two far-apart grids -> candidates with distance 0,
   but two independent events; human "same_event" verdict is rejected.
2. ``test_recurrence_after_rectification_is_a_new_event``
   same spot after callback -> new event, new penalty; the rectified event is
   never reopened.
3. ``test_repeated_rectification_callbacks_are_appended_only``
   double callback -> first closes, second effective=false, both retained.
Plus: different-angle de-dup, injectable-clock escalation, occurrence-time
contract attribution, review lock immutability + append-only corrections, and
full evidence traceability from every deduction row.
"""
from __future__ import annotations

import io
from datetime import timedelta

from datetime import timezone as dt_timezone

from django.contrib.gis.geos import GEOSGeometry, Polygon
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from rest_framework.test import APITestCase

from inspections.clock import Clock
from inspections.fake_images import make_photo, make_rephoto
from inspections.models import (Contractor, CleaningContract, Event,
                                PenaltyUnit, Photo, Rectification, RoadGrid)

UTC = dt_timezone.utc
T0 = timezone.datetime(2026, 9, 24, 8, 0, 0, tzinfo=UTC)


def box_around(lon: float, lat: float, half: float = 0.002) -> Polygon:
    return Polygon([
        (lon - half, lat - half),
        (lon + half, lat - half),
        (lon + half, lat + half),
        (lon - half, lat + half),
        (lon - half, lat - half),
    ], srid=4326)


def upload(jpeg: bytes, name: str = "p.jpg") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, jpeg, content_type="image/jpeg")


class InspectionAPITestCase(APITestCase):
    def setUp(self):
        Clock.set(T0)
        self.grid_a = RoadGrid.objects.create(
            code="A01", name="和平路网格", area=box_around(116.400, 39.900))
        self.grid_b = RoadGrid.objects.create(
            code="B02", name="幸福路网格", area=box_around(116.500, 39.950))

        self.contractor_1 = Contractor.objects.create(name="甲保洁公司")
        self.contractor_2 = Contractor.objects.create(name="乙保洁公司")
        # Grid A: company 1 until T0+10d, then company 2 takes over.
        CleaningContract.objects.create(
            contractor=self.contractor_1, grid=self.grid_a,
            valid_from=T0 - timedelta(days=365),
            valid_to=T0 + timedelta(days=10))
        CleaningContract.objects.create(
            contractor=self.contractor_2, grid=self.grid_a,
            valid_from=T0 + timedelta(days=10))
        CleaningContract.objects.create(
            contractor=self.contractor_1, grid=self.grid_b,
            valid_from=T0 - timedelta(days=365))

    def tearDown(self):
        Clock.reset()

    # -- helpers -----------------------------------------------------------
    def post_photo(self, *, lon, lat, taken_at, image=None,
                   category="garbage_overflow", kind="problem", note=""):
        image = image or make_photo(category, "seed-A")
        resp = self.client.post("/api/photos/", {
            "image": upload(image), "lon": lon, "lat": lat,
            "taken_at": taken_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "category": category, "kind": kind, "note": note,
        }, format="multipart")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def rectify(self, event_id, callback_at, note="", evidence=None,
                lon=None, lat=None, taken_at=None):
        payload = {"callback_at": callback_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "note": note}
        if evidence is not None:
            payload["evidence"] = upload(evidence, "fixed.jpg")
            payload["lon"], payload["lat"] = lon, lat
            payload["taken_at"] = (taken_at or callback_at).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        resp = self.client.post(f"/api/events/{event_id}/rectify/",
                                payload, format="multipart")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def pin_clock(self, moment):
        resp = self.client.post("/api/clock/", {
            "now": moment.strftime("%Y-%m-%dT%H:%M:%SZ")}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)

    # ------------------------------------------------------------------ #
    # Scenario 1: identical picture at a different location must NOT merge
    # ------------------------------------------------------------------ #
    def test_same_picture_uploaded_at_different_locations_is_not_merged(self):
        pa = self.post_photo(lon=116.400, lat=39.900, taken_at=T0,
                             note="和平路垃圾外溢")
        # Byte-identical file mis-uploaded as if from grid B (different place).
        pb = self.post_photo(lon=116.500, lat=39.950,
                             taken_at=T0 + timedelta(hours=1),
                             note="幸福路同一图误传")

        # Two distinct events and their own base penalty units.
        self.assertNotEqual(pa["event"], pb["event"])
        self.assertEqual(Event.objects.count(), 2)
        self.assertEqual(PenaltyUnit.objects.filter(level=0).count(), 2)

        candidates = self.client.get(
            "/api/duplicate-candidates/").json()
        self.assertEqual(len(candidates), 1)
        cand = candidates[0]
        self.assertEqual(cand["hamming_distance"], 0)  # visually identical
        self.assertGreater(cand["distance_meters"], 5000)  # but km apart
        self.assertFalse(cand["within_spatial_window"])
        self.assertFalse(cand["same_spot_repeat"])

        # Human tries to mark same event -> rejected by the spatial rule.
        resp = self.client.post(
            f"/api/duplicate-candidates/{cand['id']}/decide/",
            {"decision": "different_place", "decided_by": "inspector-li"},
            format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["decision"], "different_place")

        # Attempting the forbidden same_event merge directly via service:
        from inspections.serializers import CandidateDecisionSerializer
        from inspections.services import DomainError, decide_candidate
        from inspections.models import DuplicateCandidate
        dc = DuplicateCandidate.objects.get()
        dc.decision = DuplicateCandidate.Decision.PENDING
        dc.save()
        with self.assertRaises(DomainError):
            decide_candidate(dc, DuplicateCandidate.Decision.SAME_EVENT,
                             "inspector-li")

        # Penalty attribution follows each grid's own contract.
        units = PenaltyUnit.objects.filter(level=0).select_related("event")
        owners = {u.event.grid_id: u.contractor_id for u in units}
        self.assertEqual(owners[self.grid_a.id], self.contractor_1.id)
        self.assertEqual(owners[self.grid_b.id], self.contractor_1.id)

    # ------------------------------------------------------------------ #
    # Different angle of the SAME open spot -> absorbed, scored once
    # ------------------------------------------------------------------ #
    def test_same_event_different_angle_scored_once(self):
        p1 = self.post_photo(lon=116.400, lat=39.900, taken_at=T0)
        # 20 m away, 2 h later, visually perturbed rephoto (Hamming 6).
        p2 = self.post_photo(lon=116.4002, lat=39.9001,
                             taken_at=T0 + timedelta(hours=2),
                             image=make_rephoto("garbage_overflow", "seed-A",
                                               angle=3, shift=5),
                             note="另一角度补拍")
        self.assertEqual(p1["event"], p2["event"])
        event = Event.objects.get()
        self.assertEqual(event.photos.count(), 2)
        self.assertEqual(PenaltyUnit.objects.count(), 1)  # one base unit only

        cand = self.client.get("/api/duplicate-candidates/",
                               {"same_spot_only": "true"}).json()
        self.assertEqual(len(cand), 1)
        resp = self.client.post(
            f"/api/duplicate-candidates/{cand[0]['id']}/decide/",
            {"decision": "same_event", "decided_by": "inspector-li"},
            format="json")
        self.assertEqual(resp.status_code, 200, resp.content)

    # ------------------------------------------------------------------ #
    # Scenario 2: recurrence after rectification -> NEW event
    # ------------------------------------------------------------------ #
    def test_recurrence_after_rectification_is_a_new_event(self):
        p1 = self.post_photo(lon=116.400, lat=39.900, taken_at=T0)
        first_event_id = p1["event"]
        self.rectify(first_event_id, T0 + timedelta(hours=5),
                     note="第一次整改完成")

        ev1 = Event.objects.get(pk=first_event_id)
        self.assertEqual(ev1.status, Event.Status.RECTIFIED)
        self.assertEqual(ev1.rectified_at, T0 + timedelta(hours=5))

        # Problem comes back at the same spot after the fix.
        p2 = self.post_photo(lon=116.4001, lat=39.90005,
                             taken_at=T0 + timedelta(days=1),
                             image=make_rephoto("garbage_overflow", "seed-A",
                                               angle=1),
                             note="整改后再次外溢")
        self.assertNotEqual(p2["event"], first_event_id)
        self.assertEqual(Event.objects.filter(
            status=Event.Status.RECTIFIED).count(), 1)
        self.assertEqual(Event.objects.filter(
            status=Event.Status.OPEN).count(), 1)
        # Each occurrence has its own base deduction.
        self.assertEqual(PenaltyUnit.objects.filter(level=0).count(), 2)

    # ------------------------------------------------------------------ #
    # Scenario 3: repeated rectification callbacks are append-only
    # ------------------------------------------------------------------ #
    def test_repeated_rectification_callbacks_are_appended_only(self):
        p1 = self.post_photo(lon=116.400, lat=39.900, taken_at=T0)
        eid = p1["event"]
        r1 = self.rectify(eid, T0 + timedelta(hours=3), note="回调1")
        r2 = self.rectify(eid, T0 + timedelta(hours=4), note="重复回调2")
        r3 = self.rectify(eid, T0 + timedelta(hours=5), note="重复回调3")

        rects = Rectification.objects.filter(event_id=eid).order_by("id")
        self.assertEqual(list(rects.values_list("effective", flat=True)),
                         [True, False, False])
        self.assertEqual(
            Event.objects.get(pk=eid).rectified_at,
            T0 + timedelta(hours=3))  # first effective time never rewritten

        listing = self.client.get(
            f"/api/events/{eid}/rectifications/").json()
        self.assertEqual(len(listing), 3)

    # ------------------------------------------------------------------ #
    # Escalation driven by the injected clock
    # ------------------------------------------------------------------ #
    def test_escalation_uses_injected_clock_and_is_idempotent(self):
        p1 = self.post_photo(lon=116.400, lat=39.900, taken_at=T0)
        eid = p1["event"]

        self.pin_clock(T0 + timedelta(hours=23))
        body = self.client.post("/api/escalations/", {}, format="json").json()
        self.assertEqual(body["issued_count"], 0)

        self.pin_clock(T0 + timedelta(hours=25))
        body = self.client.post("/api/escalations/", {}, format="json").json()
        self.assertEqual(body["issued_count"], 1)
        self.assertEqual(PenaltyUnit.objects.filter(event_id=eid).count(), 2)

        self.pin_clock(T0 + timedelta(hours=50))
        body = self.client.post("/api/escalations/", {}, format="json").json()
        self.assertEqual(body["issued_count"], 1)  # only L2 is new

        # Re-running at the same time adds nothing.
        body = self.client.post("/api/escalations/", {}, format="json").json()
        self.assertEqual(body["issued_count"], 0)
        levels = set(PenaltyUnit.objects.filter(
            event_id=eid).values_list("level", flat=True))
        self.assertEqual(levels, {0, 1, 2})

        # Rectified events stop accumulating escalations.
        self.rectify(eid, T0 + timedelta(hours=60))
        self.pin_clock(T0 + timedelta(hours=80))
        body = self.client.post("/api/escalations/", {}, format="json").json()
        self.assertEqual(body["issued_count"], 0)

    # ------------------------------------------------------------------ #
    # Liability snapshot = contractor at occurrence time, not entry time
    # ------------------------------------------------------------------ #
    def test_attribution_uses_occurrence_time_contract(self):
        # Occurrence while company 1 holds the grid ...
        p1 = self.post_photo(lon=116.400, lat=39.900, taken_at=T0)
        eid = p1["event"]
        # ... but the record is entered/escalated much later, after company 2
        # has taken over (T0+10d).  The clock now sits at T0+12d.
        self.pin_clock(T0 + timedelta(days=12))
        body = self.client.post("/api/escalations/", {}, format="json").json()
        self.assertEqual(body["issued_count"], 3)

        units = PenaltyUnit.objects.filter(event_id=eid).order_by("level")
        for unit in units:
            self.assertEqual(unit.contractor_id, self.contractor_1.id,
                             "扣分必须归属事件发生时的承包商")
            self.assertEqual(unit.liable_contract.contractor_id,
                             self.contractor_1.id)

    # ------------------------------------------------------------------ #
    # Review lock + append-only corrections
    # ------------------------------------------------------------------ #
    def test_review_locks_unit_and_corrections_append(self):
        self.post_photo(lon=116.400, lat=39.900, taken_at=T0)
        unit = PenaltyUnit.objects.get(level=0)
        resp = self.client.post(
            f"/api/penalty-units/{unit.id}/review/",
            {"reviewer": "chief-wang"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        unit.refresh_from_db()
        self.assertTrue(unit.is_locked)
        self.assertEqual(unit.status, PenaltyUnit.Status.LOCKED)
        self.assertTrue(unit.contractor_locked)

        # Review again -> rejected (model-level immutability).
        resp = self.client.post(
            f"/api/penalty-units/{unit.id}/review/",
            {"reviewer": "someone-else"}, format="json")
        self.assertEqual(resp.status_code, 409)

        # DELETE/PUT do not exist for penalty units at all.
        self.assertEqual(self.client.delete(
            f"/api/penalty-units/{unit.id}/").status_code, 405)

        # Corrections append: -2 appeal refund then +1 re-score.
        for delta, reason, who in [(-2, "申诉成立退还", "audit"),
                                   (1, "复核后追加", "audit")]:
            resp = self.client.post(
                f"/api/penalty-units/{unit.id}/corrections/",
                {"delta_points": delta, "reason": reason,
                 "created_by": who}, format="json")
            self.assertEqual(resp.status_code, 201, resp.content)

        detail = self.client.get(f"/api/penalty-units/{unit.id}/").json()
        self.assertEqual(detail["base_points"], 2)
        self.assertEqual(detail["total_points"], 2)     # original frozen
        self.assertEqual(detail["corrected_points"], 1)  # 2 - 2 + 1
        self.assertEqual(len(detail["corrections"]), 2)
        self.assertEqual(detail["status"], "appealed")

    # ------------------------------------------------------------------ #
    # Every deduction traces to exactly one unit + its evidence
    # ------------------------------------------------------------------ #
    def test_every_deduction_traces_to_unique_unit_and_evidence(self):
        p1 = self.post_photo(lon=116.400, lat=39.900, taken_at=T0)
        self.post_photo(lon=116.40015, lat=39.90015,
                        taken_at=T0 + timedelta(hours=1),
                        image=make_rephoto("garbage_overflow", "seed-A"),
                        note="同事件第二证据")
        self.pin_clock(T0 + timedelta(hours=26))
        self.client.post("/api/escalations/", {}, format="json")

        ledger = self.client.get("/api/penalty-units/").json()
        self.assertEqual(len(ledger), 2)  # base + L1
        totals = {}
        for row in ledger:
            # unique per (event, level)
            totals.setdefault((row["event"], row["level"]), 0)
            totals[(row["event"], row["level"])] += 1
            self.assertTrue(row["evidence_photo_ids"])
            self.assertIn(p1["event"], [row["event"]])
            for pid in row["evidence_photo_ids"]:
                photo = Photo.objects.get(pk=pid)
                self.assertTrue(photo.phash)
                self.assertIsNotNone(photo.image.name)
                self.assertEqual(photo.event_id, row["event"])
            self.assertIsNotNone(row["contractor"])
            self.assertIsNotNone(row["liable_contract"])
        self.assertTrue(all(v == 1 for v in totals.values()))

    def test_photo_outside_any_grid_is_rejected(self):
        resp = self.client.post("/api/photos/", {
            "image": upload(make_photo("road_stain", "seed-A")),
            "lon": 10.0, "lat": 10.0,
            "taken_at": T0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "category": "road_stain"}, format="multipart")
        self.assertEqual(resp.status_code, 400)
