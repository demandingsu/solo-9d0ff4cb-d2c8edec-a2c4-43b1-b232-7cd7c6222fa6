import unittest
from datetime import datetime

from tendercheck.models import (
    Announcement,
    KIND_CANCELLATION,
    KIND_CORRECTION,
    KIND_ORIGINAL,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_PENDING,
)
from tendercheck.parsing import BEIJING_TZ, parse_datetime
from tendercheck.resolution import resolve_check


NOW = datetime(2026, 11, 11, 12, 0, tzinfo=BEIJING_TZ)


def notice(title, kind, publish, deadline=None, fetch_ok=True):
    return Announcement(
        url=f"http://example.test/{title}",
        title=title,
        kind=kind,
        publish_time=parse_datetime(publish),
        deadline=parse_datetime(deadline) if deadline else None,
        fetch_ok=fetch_ok,
    )


def resolve(announcements):
    return resolve_check(
        project_no="GSB-2026-001",
        list_url="http://example.test/list",
        announcements=announcements,
        issues=[],
        pages=["http://example.test/list"],
        duplicates_skipped=0,
        detail_count=len(announcements),
        now=NOW,
    )


class ResolutionTests(unittest.TestCase):
    def test_latest_correction_sets_deadline(self):
        result = resolve([
            notice("原公告", KIND_ORIGINAL, "2026-10-01 09:00", "2026-11-20 17:00"),
            notice("更正公告", KIND_CORRECTION, "2026-10-02 09:00", "2026-11-27 17:00"),
        ])
        self.assertEqual(result.status, STATUS_ACTIVE)
        self.assertEqual(result.deadline.strftime("%Y-%m-%d %H:%M"), "2026-11-27 17:00")

    def test_correction_after_cancellation_does_not_restore(self):
        result = resolve([
            notice("原公告", KIND_ORIGINAL, "2026-10-01 09:00", "2026-11-20 17:00"),
            notice("取消公告", KIND_CANCELLATION, "2026-10-30 16:00"),
            notice("延期更正", KIND_CORRECTION, "2026-11-05 10:00", "2026-12-05 09:30"),
        ])
        self.assertEqual(result.status, STATUS_CANCELLED)
        self.assertEqual(len(result.ignored_after_cancel), 1)
        self.assertIsNone(result.deadline)

    def test_same_time_conflicting_corrections_are_pending(self):
        result = resolve([
            notice("原公告", KIND_ORIGINAL, "2026-10-01 09:00", "2026-11-15 17:00"),
            notice("更正A", KIND_CORRECTION, "2026-10-25 16:00", "2026-11-22 17:00"),
            notice("更正B", KIND_CORRECTION, "2026-10-25 16:00", "2026-11-23 17:00"),
        ])
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertTrue(result.conflicts)
        self.assertIsNone(result.deadline)

    def test_failed_detail_is_pending(self):
        result = resolve([
            notice("原公告", KIND_ORIGINAL, "2026-10-01 09:00", "2026-11-20 17:00"),
            notice("更正公告", KIND_CORRECTION, "2026-10-02 09:00", fetch_ok=False),
        ])
        self.assertEqual(result.status, STATUS_PENDING)


if __name__ == "__main__":
    unittest.main()
