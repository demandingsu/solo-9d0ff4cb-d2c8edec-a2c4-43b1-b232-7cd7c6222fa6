import unittest
from datetime import datetime

from bs4 import BeautifulSoup

from tendercheck.parsing import parse_datetime
from tendercheck.requirement_models import (
    DataRow,
    ENTRY_CONFLICT,
    ENTRY_OK,
    ENTRY_PENDING,
    PACKAGE_CONFIRMED,
    PACKAGE_INHERITED,
    PACKAGE_PENDING,
    RequirementPage,
)
from tendercheck.requirements import (
    PackageCarry,
    assign_packages,
    extract_requirement_tables,
    find_pagination_links,
    merge_rows,
)

URL = "http://example.test/detail/req.html"


def table_html(body: str, *, head: str = "") -> str:
    return f"<html><body><table>{head}<tbody>{body}</tbody></table></body></html>"


def extract(html: str, url: str = URL, label: str = "需求表"):
    soup = BeautifulSoup(html, "html.parser")
    return extract_requirement_tables(soup, page_url=url, page_label=label)


class TableExpansionTests(unittest.TestCase):
    def test_rowspan_and_colspan_header_are_restored(self):
        html = table_html(
            "<tr><td rowspan='2'>包1</td><td>1-1</td><td>口罩</td><td>1200</td><td>只</td></tr>"
            "<tr><td>1-2</td><td>帽子</td><td>800</td><td>只</td></tr>"
            "<tr><td>包2</td><td>2-1</td><td>试剂盒</td><td>200</td><td>盒</td></tr>",
            head=(
                "<thead><tr><th rowspan='2'>包号</th><th rowspan='2'>品目号</th>"
                "<th rowspan='2'>品目名称</th><th colspan='2'>数量及单位</th></tr>"
                "<tr><th>数量</th><th>单位</th></tr></thead>"
            ),
        )
        tables = extract(html)
        self.assertEqual(len(tables), 1)
        table = tables[0]
        self.assertEqual(
            table.header_fields,
            {"package_no": 0, "item_no": 1, "name": 2, "quantity": 3, "unit": 4},
        )
        rows = table.rows
        self.assertEqual(len(rows), 3)
        self.assertEqual((rows[0].package_no, rows[1].package_no), ("包1", "包1"))
        self.assertEqual(rows[1].item_no, "1-2")
        self.assertEqual(rows[2].package_no, "包2")
        self.assertEqual((rows[0].quantity, rows[0].unit), ("1200", "只"))

    def test_caption_and_total_rows_are_skipped(self):
        html = table_html(
            "<tr><td>包1</td><td>1-1</td><td>口罩</td><td>100</td><td>只</td></tr>"
            "<tr><td colspan='3'>合计</td><td>100</td><td>只</td></tr>",
            head="<thead><tr><th>包号</th><th>品目号</th><th>品目名称</th><th>数量</th><th>单位</th></tr></thead>",
        )
        tables = extract(html)
        self.assertEqual([row.name for row in tables[0].rows], ["口罩"])

    def test_unrelated_tables_are_ignored(self):
        html = (
            "<html><body>"
            "<table><tr><th>评审因素</th><th>分值</th></tr>"
            "<tr><td>技术方案</td><td>30</td></tr></table>"
            "</body></html>"
        )
        self.assertEqual(extract(html), [])

    def test_combined_quantity_and_unit_header_is_split(self):
        html = (
            "<html><body><table><thead><tr><th>包号</th><th>品目号</th>"
            "<th>名称</th><th colspan='2'>数量及单位</th></tr></thead><tbody>"
            "<tr><td>包1</td><td>1-1</td><td>桌子</td><td>10</td><td>张</td></tr>"
            "</tbody></table></body></html>"
        )
        row = extract(html)[0].rows[0]
        self.assertEqual((row.quantity, row.unit), ("10", "张"))

    def test_source_row_and_url_are_recorded(self):
        html = table_html(
            "<tr><td>包1</td><td>1-1</td><td>口罩</td><td>100</td><td>只</td></tr>",
            head="<thead><tr><th>包号</th><th>品目号</th><th>品目名称</th><th>数量</th><th>单位</th></tr></thead>",
        )
        row = extract(html, url="http://example.test/p3.html", label="公告（第 3 页）")[0].rows[0]
        self.assertEqual(row.source_url, "http://example.test/p3.html")
        self.assertEqual(row.source_label, "公告（第 3 页）")
        self.assertIn("口罩", row.source_row)


def row(package, item, name, quantity, unit, *, url=URL, label="需求表"):
    return DataRow(
        package_no=package,
        item_no=item,
        name=name,
        quantity=quantity,
        unit=unit,
        source_url=url,
        source_label=label,
        source_row=f"{package} | {item} | {name} | {quantity} | {unit}",
        row_index=1,
    )


class PackageContinuationTests(unittest.TestCase):
    def test_continuation_inherits_when_pages_are_continuous(self):
        page1 = RequirementPage(url="http://x/req.html", label="第 1 页", page_no=1)
        continuation = RequirementPage(
            url="http://x/req-2.html", label="第 2 页", page_no=2
        )
        carry = PackageCarry()
        carry.begin_page(page1)
        first = row("包1", "1-1", "A", "1", "个")
        carry.apply(first)
        self.assertEqual(first.package_status, PACKAGE_CONFIRMED)
        carry.begin_page(continuation)
        second = row("", "1-2", "B", "2", "个")
        carry.apply(second)
        self.assertEqual(second.package_no, "包1")
        self.assertEqual(second.package_status, PACKAGE_INHERITED)

    def test_continuation_is_not_inherited_after_failed_page(self):
        page1 = RequirementPage(url="http://x/req.html", label="第 1 页", page_no=1)
        failed = RequirementPage(
            url="http://x/req-2.html", label="第 2 页", page_no=2, fetch_ok=False
        )
        page3 = RequirementPage(url="http://x/req-3.html", label="第 3 页", page_no=3)
        carry = PackageCarry()
        carry.begin_page(page1)
        carry.apply(row("包1", "1-1", "A", "1", "个"))
        carry.begin_page(failed)
        carry.begin_page(page3)
        after_gap = row("", "3-1", "C", "3", "个")
        carry.apply(after_gap)
        self.assertEqual(after_gap.package_no, "")
        self.assertEqual(after_gap.package_status, PACKAGE_PENDING)

    def test_continuation_after_numbered_gap_is_pending(self):
        page1 = RequirementPage(url="http://x/req.html", label="第 1 页", page_no=1)
        page3 = RequirementPage(url="http://x/req-3.html", label="第 3 页", page_no=3)
        carry = PackageCarry()
        carry.begin_page(page1)
        carry.apply(row("包2", "2-1", "A", "1", "个"))
        carry.begin_page(page3)
        after_gap = row("", "4-1", "D", "4", "个")
        carry.apply(after_gap)
        self.assertEqual(after_gap.package_status, PACKAGE_PENDING)

    def test_explicit_package_after_gap_reanchors_continuation(self):
        page1 = RequirementPage(url="http://x/req.html", label="第 1 页", page_no=1)
        page3 = RequirementPage(url="http://x/req-3.html", label="第 3 页", page_no=3)
        page4 = RequirementPage(url="http://x/req-4.html", label="第 4 页", page_no=4)
        carry = PackageCarry()
        carry.begin_page(page1)
        carry.apply(row("包1", "1-1", "A", "1", "个"))
        carry.begin_page(page3)
        unnamed = row("", "4-1", "D", "4", "个")
        carry.apply(unnamed)
        self.assertEqual(unnamed.package_status, PACKAGE_PENDING)
        carry.begin_page(page4)
        explicit = row("包5", "5-1", "E", "5", "个")
        carry.apply(explicit)
        inherited = row("", "5-2", "F", "6", "个")
        carry.apply(inherited)
        self.assertEqual(inherited.package_no, "包5")
        self.assertEqual(inherited.package_status, PACKAGE_INHERITED)

    def test_assign_packages_keeps_explicit_package(self):
        html = table_html(
            "<tr><td>包2</td><td>2-1</td><td>试剂盒</td><td>1</td><td>盒</td></tr>",
            head="<thead><tr><th>包号</th><th>品目号</th><th>品目名称</th><th>数量</th><th>单位</th></tr></thead>",
        )
        table = extract(html)[0]
        assign_packages([table], PackageCarry())
        self.assertEqual(table.rows[0].package_status, PACKAGE_CONFIRMED)


class MergeTests(unittest.TestCase):
    def test_identical_rows_merge_and_keep_all_sources(self):
        entries, duplicates = merge_rows(
            [
                row("包1", "1-1", "口罩", "100", "只", url="http://x/p1", label="第 1 页"),
                row("包1", "1-1", "口罩", "100", "只", url="http://x/p2", label="第 2 页"),
            ]
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(duplicates, 1)
        entry = entries[0]
        self.assertEqual(entry.status, ENTRY_OK)
        self.assertEqual(len(entry.variants), 1)
        self.assertEqual(
            [source.url for source in entry.variants[0].sources],
            ["http://x/p1", "http://x/p2"],
        )

    def test_conflicting_values_keep_both_originals(self):
        entries, _ = merge_rows(
            [
                row("包2", "2-1", "隔离面罩", "50", "盒", url="http://x/p1"),
                row("包2", "2-1", "隔离面罩", "60", "盒", url="http://x/p2"),
            ]
        )
        entry = entries[0]
        self.assertEqual(entry.status, ENTRY_CONFLICT)
        self.assertEqual(len(entry.variants), 2)
        self.assertEqual(
            {(variant.name, variant.quantity, variant.unit) for variant in entry.variants},
            {("隔离面罩", "50", "盒"), ("隔离面罩", "60", "盒")},
        )
        self.assertEqual(
            {variant.sources[0].url for variant in entry.variants},
            {"http://x/p1", "http://x/p2"},
        )

    def test_package_notation_variants_merge(self):
        entries, _ = merge_rows(
            [
                row("包1", "1-1", "A", "1", "个"),
                row("1", "1-1", "A", "1", "个"),
            ]
        )
        self.assertEqual(len(entries), 1)
        self.assertIn(entries[0].package_no, {"包1", "1"})

    def test_pending_rows_without_package_stay_separate(self):
        entries, _ = merge_rows(
            [row("", "4-1", "A", "1", "个"), row("", "4-1", "A", "1", "个")]
        )
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(entry.status == ENTRY_PENDING for entry in entries))

    def test_pending_without_item_number_is_marked(self):
        entries, _ = merge_rows([row("包1", "", "A", "1", "个")])
        self.assertEqual(entries[0].status, ENTRY_PENDING)


class PaginationLinkTests(unittest.TestCase):
    def test_next_and_numbered_links_are_found(self):
        soup = BeautifulSoup(
            """
            <html><body><nav class="pagination">
              <a class="page-link prev" rel="prev" href="req.html">上一页</a>
              <span class="current">2</span>
              <a class="page-link" href="req-3.html">3</a>
              <a class="page-link next" rel="next" href="req-3.html">下一页</a>
            </nav></body></html>
            """,
            "html.parser",
        )
        links = find_pagination_links(
            soup,
            page_url="http://example.test/detail/req-2.html",
            origin=("http", "example.test"),
            base_dir="/detail",
        )
        self.assertEqual([url for _, url in links], ["http://example.test/detail/req-3.html"])
        self.assertEqual(links[0][0], 3)

    def test_other_directories_and_other_sites_are_ignored(self):
        soup = BeautifulSoup(
            """
            <html><body><nav class="pagination">
              <a class="page-link next" rel="next" href="/other/list-2.html">下一页</a>
              <a class="page-link next" rel="next" href="http://other.test/detail/req-2.html">下一页</a>
            </nav></body></html>
            """,
            "html.parser",
        )
        self.assertEqual(
            find_pagination_links(
                soup,
                page_url="http://example.test/detail/req.html",
                origin=("http", "example.test"),
                base_dir="/detail",
            ),
            [],
        )

    def test_prev_link_is_never_followed(self):
        soup = BeautifulSoup(
            """
            <html><body><nav class="pagination">
              <a class="page-link" href="req-1.html">1</a>
              <a class="page-link prev" rel="prev" href="req-1.html">上一页</a>
            </nav></body></html>
            """,
            "html.parser",
        )
        links = find_pagination_links(
            soup,
            page_url="http://example.test/detail/req-2.html",
            origin=("http", "example.test"),
            base_dir="/detail",
        )
        self.assertEqual([url for _, url in links], ["http://example.test/detail/req-1.html"])
        self.assertEqual(links[0][0], 1)

    def test_query_based_pagination_is_followed(self):
        soup = BeautifulSoup(
            """
            <html><body><nav class="pagination">
              <a class="page-link" href="req.html?pageNo=2&amp;code=A">2</a>
              <a class="page-link next" rel="next" href="req.html?pageNo=2&amp;code=A">下一页</a>
            </nav></body></html>
            """,
            "html.parser",
        )
        links = find_pagination_links(
            soup,
            page_url="http://example.test/detail/req.html?pageNo=1&code=A",
            origin=("http", "example.test"),
            base_dir="/detail",
        )
        self.assertEqual(
            [url for _, url in links],
            ["http://example.test/detail/req.html?pageNo=2&code=A"],
        )
        self.assertEqual(links[0][0], 2)

    def test_unrelated_numeric_anchor_is_not_followed(self):
        soup = BeautifulSoup(
            """
            <html><body>
              <a href="other.html">3</a>
              <a class="page-link" href="manual.html">操作手册</a>
            </body></html>
            """,
            "html.parser",
        )
        self.assertEqual(
            find_pagination_links(
                soup,
                page_url="http://example.test/detail/req.html",
                origin=("http", "example.test"),
                base_dir="/detail",
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
