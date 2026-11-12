import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from tendercheck.models import (
        STATUS_CANCELLED,
        STATUS_PENDING,
    )
    from tendercheck.scraper import TenderScraper
except ModuleNotFoundError:  # requests/beautifulsoup4 are optional for core tests
    TenderScraper = None

ROOT = Path(__file__).resolve().parents[1] / "sample_site"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


@unittest.skipIf(TenderScraper is None, "scraper dependencies are not installed")
class ScraperIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = partial(QuietHandler, directory=str(ROOT))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.list_url = f"http://127.0.0.1:{cls.server.server_port}/notices/page-1.html"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def check(self, project_no):
        return TenderScraper(project_no=project_no, list_url=self.list_url).run()

    def test_active_project_timeline_and_deduplication(self):
        result = self.check("GSB-2026-001")
        self.assertEqual(len(result.pages), 3)
        self.assertEqual(result.announcement_count, 3)
        self.assertEqual(result.duplicates_skipped, 2)
        self.assertEqual(result.deadline.strftime("%Y-%m-%d %H:%M"), "2026-11-27 17:00")

    def test_cancelled_project_ignores_later_correction(self):
        result = self.check("GSB-2026-002")
        self.assertEqual(result.status, STATUS_CANCELLED)
        self.assertEqual(len(result.ignored_after_cancel), 1)

    def test_same_minute_conflicting_corrections_are_pending(self):
        result = self.check("GSB-2026-003")
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertEqual(len(result.conflicts), 1)

    def test_missing_detail_is_pending(self):
        result = self.check("GSB-2026-004")
        self.assertEqual(result.status, STATUS_PENDING)
        self.assertTrue(any("详情页抓取失败" in issue for issue in result.issues))


if __name__ == "__main__":
    unittest.main()
