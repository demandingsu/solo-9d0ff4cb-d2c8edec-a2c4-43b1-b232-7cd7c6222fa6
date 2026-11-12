import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from tendercheck.requirement_models import (
        ENTRY_CONFLICT,
        ENTRY_OK,
        ENTRY_PENDING,
        PACKAGE_INHERITED,
        PACKAGE_PENDING,
    )
    from tendercheck.requirements import RequirementsScraper
except ModuleNotFoundError:  # requests/beautifulsoup4 are optional for core tests
    RequirementsScraper = None

ROOT = Path(__file__).resolve().parents[1] / "sample_site"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


@unittest.skipIf(RequirementsScraper is None, "scraper dependencies are not installed")
class RequirementIntegrationTests(unittest.TestCase):
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

    def extract(self, project_no):
        return RequirementsScraper(project_no=project_no, list_url=self.list_url).run()

    def test_merged_header_and_rowspan_package(self):
        result = self.extract("GSB-2026-101")
        self.assertTrue(result.complete)
        self.assertEqual(len(result.entries), 4)
        self.assertEqual(
            [(entry.package_no, entry.item_no, entry.variants[0].quantity) for entry in result.entries],
            [
                ("包1", "1-1", "1200"),
                ("包1", "1-2", "800"),
                ("包2", "2-1", "200"),
                ("包2", "2-2", "3000"),
            ],
        )
        self.assertTrue(all(entry.status == ENTRY_OK for entry in result.entries))

    def test_continuation_page_inherits_package_when_continuous(self):
        result = self.extract("GSB-2026-102")
        self.assertTrue(result.complete)
        continued = [entry for entry in result.entries if entry.item_no in {"2-2", "2-3"}]
        self.assertEqual(len(continued), 2)
        for entry in continued:
            self.assertEqual(entry.package_no, "包2")
            self.assertEqual(entry.package_status, PACKAGE_INHERITED)
        self.assertEqual(continued[0].variants[0].sources[0].url, self.list_url.replace(
            "/notices/page-1.html", "/detail/r-102-original-page-2.html"
        ))

    def test_missing_page_is_incomplete_and_marks_orphans_pending(self):
        result = self.extract("GSB-2026-103")
        self.assertFalse(result.complete)
        self.assertTrue(any("不完整" in issue for issue in result.issues))
        self.assertTrue(any("r-103-original-page-2.html" in page.url and not page.fetch_ok
                            for page in result.detail_pages))
        orphans = [entry for entry in result.entries if entry.item_no in {"4-1", "4-2"}]
        self.assertEqual(len(orphans), 2)
        for entry in orphans:
            self.assertEqual(entry.package_no, "")
            self.assertEqual(entry.package_status, PACKAGE_PENDING)
            self.assertEqual(entry.status, ENTRY_PENDING)

    def test_identical_rows_merge_and_conflicts_keep_originals(self):
        result = self.extract("GSB-2026-104")
        self.assertTrue(result.complete)
        self.assertEqual(result.duplicate_rows_merged, 1)
        conflict = [entry for entry in result.entries if entry.item_no == "2-1"][0]
        self.assertEqual(conflict.status, ENTRY_CONFLICT)
        self.assertEqual(len(conflict.variants), 2)
        quantities = {variant.quantity for variant in conflict.variants}
        self.assertEqual(quantities, {"50", "60"})
        for variant in conflict.variants:
            self.assertTrue(variant.sources)
            self.assertTrue(variant.sources[0].url.startswith("http"))


class RequirementRouteTests(unittest.TestCase):
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

    def setUp(self):
        try:
            from app import app
        except ModuleNotFoundError:
            self.skipTest("flask dependencies are not installed")
        self.client = app.test_client()

    def test_requirements_page_is_available(self):
        response = self.client.get("/requirements")
        self.assertEqual(response.status_code, 200)
        self.assertIn("采购需求清单整理", response.data.decode("utf-8"))

    def test_submit_renders_entries_with_sources(self):
        response = self.client.post(
            "/requirements",
            data={"project_no": "GSB-2026-104", "list_url": self.list_url},
        )
        text = response.data.decode("utf-8")
        self.assertEqual(response.status_code, 200)
        self.assertIn("字段冲突", text)
        self.assertIn("原文 1", text)
        self.assertIn("r-104-original-page-2.html", text)

    def test_incomplete_result_is_flagged_in_page(self):
        response = self.client.post(
            "/requirements",
            data={"project_no": "GSB-2026-103", "list_url": self.list_url},
        )
        text = response.data.decode("utf-8")
        self.assertIn("不完整", text)
        self.assertIn("待核实", text)

    def test_empty_project_number_is_rejected(self):
        response = self.client.post(
            "/requirements",
            data={"project_no": "  ", "list_url": self.list_url},
        )
        self.assertIn("请输入项目编号", response.data.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
