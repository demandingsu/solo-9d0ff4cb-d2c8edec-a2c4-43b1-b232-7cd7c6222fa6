from __future__ import annotations

import logging
import posixpath
import re
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from .parsing import BEIJING_TZ, clean_text, extract_project_number, normalize_project, project_mentioned
from .requirement_models import (
    DataRow,
    ENTRY_CONFLICT,
    ENTRY_OK,
    ENTRY_PENDING,
    ExtractedTable,
    PACKAGE_CONFIRMED,
    PACKAGE_INHERITED,
    PACKAGE_PENDING,
    RequirementEntry,
    RequirementPage,
    RequirementResult,
    RequirementVariant,
    RowSource,
)
from .scraper import FetchError, HttpFetcher, InputError, ListFetchError, TenderScraper, canonical_url, same_site

logger = logging.getLogger(__name__)

_PREV_TEXT_RE = re.compile(r"^(?:上一页|上页|前一页|prev|previous|<|‹|«)$", re.IGNORECASE)
_NEXT_TEXT_RE = re.compile(r"^(?:下一页|下页|后一页|next|>|›|»)$", re.IGNORECASE)

_PAGE_QUERY_KEYS = {
    "page",
    "p",
    "pg",
    "pageno",
    "pagenum",
    "pageindex",
    "current",
    "currentpage",
}

_PAGE_PATH_RE = re.compile(
    r"(?:page|paged|list|index)[-_]?(\d+)(?:\.[a-z]+)?$",
    re.IGNORECASE,
)

_PAGER_MARKER_RE = re.compile(r"(?:pagination|pager|page-link|page-item|pagelist)", re.IGNORECASE)

_NAME_HEADER_RE = re.compile(
    r"(?:品目名称|货物名称|服务名称|标的名称|采购标的|采购内容|物品名称|设备名称|产品名称|名称)$"
)
_PACKAGE_HEADER_RE = re.compile(
    r"(?:采购包|采购包号|包号|包编号|分包|分包号|标包|标段|包件|包组|所属包|包)$"
)
_ITEM_HEADER_EXPLICIT_RE = re.compile(r"^(?:品目号|品目编号|品目)$")
_ITEM_HEADER_GENERIC_RE = re.compile(r"^(?:序号|编号)$")
_QUANTITY_HEADER_RE = re.compile(r"(?:采购数量|需求数量|数量)$")
_UNIT_HEADER_RE = re.compile(r"(?:计量单位|数量单位|单位)$")

_SUMMARY_NAME_RE = re.compile(r"^(?:合计|总计|小计|合\s*计|总\s*计|小\s*计)(?:[（(].*[）)])?$")
_NOTE_NAME_RE = re.compile(r"^(?:注|备注|说明)[：:]")


def _clamp_span(value, default: int = 1, maximum: int = 50) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if number < 1:
        return default
    return min(number, maximum)


def _norm(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", clean_text(value))
    return re.sub(r"\s+", " ", value).strip().casefold()


def _package_key(value: str | None) -> str:
    text = _norm(value)
    text = re.sub(r"\s+", "", text)
    # Treat "包1" and "1" as the same package number when merging rows.
    text = re.sub(r"^(?:采购包|分包|标包|标段|包件|包组|包|第)", "", text)
    text = re.sub(r"(?:包|标段|包件|包组)$", "", text)
    return text


def _item_key(value: str | None) -> str:
    return re.sub(r"\s+", "", _norm(value))


def _natural_key(value: str) -> list[tuple[int, object]]:
    return [
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value or "")
    ]


def _field_candidates(text: str | None) -> list[tuple[int, str]]:
    """Map a header cell label to fields and a specificity score."""

    key = unicodedata.normalize("NFKC", clean_text(text))
    key = re.sub(r"[\s*＊:：（）()\[\]【】]", "", key)
    if not key:
        return []

    results: list[tuple[int, str]] = []
    if _PACKAGE_HEADER_RE.search(key) and "项目" not in key:
        results.append((3, "package_no"))
    if _NAME_HEADER_RE.search(key):
        results.append((3, "name"))
    # A combined "数量及单位" header is not itself a column label; it is
    # handled by _map_columns when it spans two columns.
    combined_quantity_unit = "数量" in key and "单位" in key
    if _QUANTITY_HEADER_RE.search(key) and not combined_quantity_unit:
        results.append((3, "quantity"))
    if _UNIT_HEADER_RE.search(key) and not combined_quantity_unit:
        results.append((3, "unit"))
    if _ITEM_HEADER_EXPLICIT_RE.search(key):
        results.append((3, "item_no"))
    elif _ITEM_HEADER_GENERIC_RE.search(key):
        results.append((2, "item_no"))
    return results


def _classify_header(text: str | None) -> Optional[tuple[int, str]]:
    candidates = _field_candidates(text)
    if not candidates:
        return None
    priority = max(item[0] for item in candidates)
    fields = [field for score, field in candidates if score == priority]
    return priority, fields[0]


def classify_header(text: str | None) -> Optional[str]:
    """Public helper: map a header cell label to a requirement field."""

    classified = _classify_header(text)
    return classified[1] if classified else None


def _row_cells(row: Tag) -> list[Tag]:
    cells = row.find_all(["th", "td"], recursive=False)
    if cells:
        return cells
    return [
        cell
        for cell in row.find_all(["th", "td"])
        if cell.find_parent("tr") is row
    ]


def _build_grid(rows: list[Tag]) -> list[list[Optional[object]]]:
    """Expand rowspan/colspan into a rectangular grid of cell objects."""

    grid: dict[int, dict[int, object]] = defaultdict(dict)
    row_count = len(rows)
    for row_index, row in enumerate(rows):
        col = 0
        for cell in _row_cells(row):
            while col in grid.get(row_index, {}):
                col += 1
            rowspan = _clamp_span(cell.get("rowspan"), maximum=max(1, row_count - row_index))
            colspan = _clamp_span(cell.get("colspan"))
            text = clean_text(cell.get_text(" ", strip=True))
            for dr in range(min(rowspan, row_count - row_index)):
                for dc in range(colspan):
                    grid[row_index + dr][col + dc] = text
            col += colspan

    width = max((max(row_map) + 1 for row_map in grid.values() if row_map), default=0)
    return [
        [grid.get(row_index, {}).get(col) for col in range(width)]
        for row_index in range(row_count)
    ]


def _row_nonempty_count(row: list[Optional[object]]) -> int:
    return sum(
        1 for value in row if value is not None and clean_text(str(value))
    )


def _row_has_header_label(row: list[Optional[object]]) -> bool:
    return any(
        value is not None and _classify_header(str(value)) is not None
        for value in row
    )


def _find_header_end(table: Tag, rows: list[Tag], grid) -> Optional[tuple[int, int]]:
    """Return (header_start, header_end) row indexes, skipping leading titles."""

    thead = table.find("thead")
    if thead is not None:
        thead_indexes = [
            index for index, row in enumerate(rows) if row.find_parent("thead") is thead
        ]
        if thead_indexes:
            end = max(thead_indexes) + 1
            start = min(thead_indexes)
            # Allow a leading caption-like row inside thead before the labels.
            while start < end and not _row_has_header_label(grid[start]):
                if _row_nonempty_count(grid[start]) <= 1:
                    start += 1
                else:
                    break
            if start < end and _row_has_header_label(grid[start]):
                return start, end
            return None

    start = 0
    # A table title above the header often spans every column as one cell.
    while (
        start < min(3, len(rows))
        and _row_nonempty_count(grid[start]) <= 1
        and not _row_has_header_label(grid[start])
    ):
        start += 1

    end = start
    while end < min(len(rows), start + 4) and _row_has_header_label(grid[end]):
        end += 1
    if end == start:
        return None
    return start, end


def _map_columns(grid, header_start: int, header_end: int) -> dict[str, int]:
    candidates: dict[str, tuple[int, int, int]] = {}
    width = len(grid[0]) if grid else 0

    # A header cell that says "数量及单位" spanning two columns has no second
    # header row to split it; assign the first spanned column to 数量 and the
    # second to 单位 so those tables are still usable.
    for row_index in range(header_start, header_end):
        for col in range(width):
            value = grid[row_index][col]
            if value is None:
                continue
            normalized = re.sub(
                r"[\s*＊:：（）()\[\]【】/、]", "",
                unicodedata.normalize("NFKC", clean_text(str(value))),
            )
            if "数量" not in normalized or "单位" not in normalized:
                continue
            span = 1
            while col + span < width and grid[row_index][col + span] == value:
                span += 1
            if span < 2:
                continue
            for field, target_col in (("quantity", col), ("unit", col + 1)):
                score = (2, row_index)
                if field not in candidates or score > candidates[field][:2]:
                    candidates[field] = (score[0], score[1], target_col)

    for row_index in range(header_start, header_end):
        for col in range(width):
            value = grid[row_index][col]
            if value is None:
                continue
            classified = _classify_header(str(value))
            if not classified:
                continue
            priority, field = classified
            # Higher specificity wins; on a tie a lower (more granular) header
            # row wins, which is how a colspan "数量及单位" splits into
            # separate 数量 and 单位 columns.
            score = (priority, row_index)
            if field not in candidates or score > candidates[field][:2]:
                candidates[field] = (score[0], score[1], col)

    mapping = {field: item[2] for field, item in candidates.items()}
    return mapping


def _is_summary_row(name: str, quantity: str, unit: str) -> bool:
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", clean_text(name)))
    if _SUMMARY_NAME_RE.match(normalized):
        return True
    if "合计" in normalized and not quantity and not unit:
        return True
    return False


def extract_requirement_tables(
    soup: BeautifulSoup,
    *,
    page_url: str,
    page_label: str,
) -> list[ExtractedTable]:
    """Extract requirement tables, expanding merged header and body cells."""

    tables: list[ExtractedTable] = []
    for table_index, table in enumerate(soup.find_all("table"), start=1):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        grid = _build_grid(rows)
        bounds = _find_header_end(table, rows, grid)
        if bounds is None:
            continue
        header_start, header_end = bounds
        mapping = _map_columns(grid, header_start, header_end)
        if "name" not in mapping or len(mapping) < 3:
            continue

        extracted_rows: list[DataRow] = []
        width = len(grid[0]) if grid else 0
        for row_index in range(header_end, len(rows)):
            row = grid[row_index]
            values: dict[str, str] = {}
            for field, col in mapping.items():
                values[field] = clean_text(str(row[col])) if col < width and row[col] is not None else ""

            if not any(values.values()):
                continue
            # Repeated header rows inside a tbody (common in long tables).
            header_like = sum(
                1
                for value in values.values()
                if value and _classify_header(value) is not None
            )
            if header_like >= 2:
                continue

            name = values.get("name", "")
            quantity = values.get("quantity", "")
            unit = values.get("unit", "")
            if _is_summary_row(name, quantity, unit):
                continue
            if not name and not values.get("item_no"):
                continue
            if _NOTE_NAME_RE.match(name) and not quantity and not unit:
                continue

            # A repeated header row that only repeats "包号/品目号" is caught
            # above; also skip rows whose name cell is itself a header label.
            if name and _classify_header(name) is not None:
                continue
            # Requirement tables sometimes continue on the same page with a
            # short "续表" separator row that spans only the leading columns.
            if name in {"续表", "续上表", "接上表"} and not quantity and not unit:
                continue

            raw_cells = [
                clean_text(cell.get_text(" ", strip=True))
                for cell in _row_cells(rows[row_index])
            ]
            raw_row = " | ".join(cell for cell in raw_cells if cell)
            extracted_rows.append(
                DataRow(
                    package_no=values.get("package_no", ""),
                    item_no=values.get("item_no", ""),
                    name=name,
                    quantity=quantity,
                    unit=unit,
                    source_url=page_url,
                    source_label=page_label,
                    source_row=raw_row,
                    row_index=row_index + 1,
                )
            )

        if not extracted_rows:
            continue
        tables.append(
            ExtractedTable(
                caption=clean_text(table.caption.get_text(" ", strip=True)) if table.caption else "",
                page_url=page_url,
                page_label=page_label,
                table_index=table_index,
                header_fields=mapping,
                rows=extracted_rows,
            )
        )
    return tables


@dataclass
class PackageCarry:
    """Package-number state for continuous continuation tables."""

    last_package: Optional[str] = None
    broken: bool = False
    prev_number: Optional[int] = None

    def begin_page(self, page: RequirementPage) -> bool:
        """Update continuity. Returns True when a new gap was detected."""

        new_break = False
        if not page.fetch_ok:
            new_break = not self.broken
            self.broken = True
            if page.page_no is not None:
                self.prev_number = page.page_no
            return new_break
        if (
            self.prev_number is not None
            and page.page_no is not None
            and page.page_no != self.prev_number + 1
        ):
            new_break = not self.broken
            self.broken = True
        if page.page_no is not None:
            self.prev_number = page.page_no
        return new_break

    @property
    def can_inherit(self) -> bool:
        return not self.broken and bool(self.last_package)

    def apply(self, row: DataRow) -> None:
        if row.package_no:
            row.package_status = PACKAGE_CONFIRMED
            self.last_package = row.package_no
            # An explicitly written package number re-anchors the continuation
            # chain from this point in the document.
            self.broken = False
            return
        if self.can_inherit:
            row.package_no = self.last_package or ""
            row.package_status = PACKAGE_INHERITED
            row.notes.append(
                f"续表未写包号，按连续分页承接上一包「{row.package_no}」"
            )
            return
        row.package_status = PACKAGE_PENDING
        if self.broken:
            row.notes.append("分页中断后未写明包号，无法确认所属包，待核实")
        else:
            row.notes.append("未写明包号，且没有已知的上一包可承接，待核实")


def assign_packages(tables: list[ExtractedTable], carry: PackageCarry) -> None:
    for table in tables:
        for row in table.rows:
            carry.apply(row)


def _variant_key(row: DataRow) -> tuple[str, str, str]:
    return (_norm(row.name), _norm(row.quantity), _norm(row.unit))


def merge_rows(rows: list[DataRow]) -> tuple[list[RequirementEntry], int]:
    """Merge rows by package+item, retaining every distinct source variant."""

    keyed: dict[tuple[str, str], RequirementEntry] = {}
    standalone: list[RequirementEntry] = []
    duplicates = 0

    for row in rows:
        package_key = _package_key(row.package_no)
        item_key = _item_key(row.item_no)
        row_notes = list(row.notes)
        source = RowSource(url=row.source_url, label=row.source_label, raw=row.source_row)

        if not package_key or not item_key:
            if not package_key:
                note = "缺少包号，无法确认所属包，待核实"
                entry_status = ENTRY_PENDING
            else:
                note = "缺少品目号，无法与其他行合并，待核实"
                entry_status = ENTRY_PENDING
            standalone.append(
                RequirementEntry(
                    package_no=row.package_no,
                    item_no=row.item_no,
                    package_status=row.package_status or PACKAGE_PENDING,
                    package_note="；".join(row_notes),
                    status=entry_status,
                    variants=[
                        RequirementVariant(
                            name=row.name,
                            quantity=row.quantity,
                            unit=row.unit,
                            sources=[source],
                        )
                    ],
                    notes=[note],
                )
            )
            continue

        key = (package_key, item_key)
        entry = keyed.get(key)
        if entry is None:
            entry = RequirementEntry(
                package_no=row.package_no,
                item_no=row.item_no,
                package_status=row.package_status,
                package_note="；".join(row_notes),
                status=ENTRY_OK,
                variants=[
                    RequirementVariant(
                        name=row.name,
                        quantity=row.quantity,
                        unit=row.unit,
                        sources=[source],
                    )
                ],
            )
            keyed[key] = entry
            continue

        if entry.package_no and row.package_no and _norm(entry.package_no) != _norm(row.package_no):
            entry.notes.append(
                f"同一包号在来源中写法不同：{entry.package_no} / {row.package_no}"
            )
        if entry.item_no and row.item_no and _norm(entry.item_no) != _norm(row.item_no):
            entry.notes.append(
                f"同一品目号在来源中写法不同：{entry.item_no} / {row.item_no}"
            )
        if row_notes:
            entry.package_note = "；".join(
                filter(None, [entry.package_note, *row_notes])
            )

        rank = {PACKAGE_PENDING: 0, PACKAGE_INHERITED: 1, PACKAGE_CONFIRMED: 2}
        if rank.get(row.package_status, 0) > rank.get(entry.package_status, 0):
            entry.package_status = row.package_status

        variant_key = _variant_key(row)
        matched = None
        for variant in entry.variants:
            if (
                _norm(variant.name),
                _norm(variant.quantity),
                _norm(variant.unit),
            ) == variant_key:
                matched = variant
                break
        if matched is not None:
            matched.sources.append(source)
            duplicates += 1
        else:
            entry.variants.append(
                RequirementVariant(
                    name=row.name,
                    quantity=row.quantity,
                    unit=row.unit,
                    sources=[source],
                )
            )

    entries = list(keyed.values())
    for entry in entries:
        if len(entry.variants) > 1:
            entry.status = ENTRY_CONFLICT
            entry.notes.append(
                f"字段冲突：同一包号、品目号存在 {len(entry.variants)} 种不同取值，"
                "已保留全部原文，未自动选择"
            )
        elif entry.package_status == PACKAGE_PENDING:
            entry.status = ENTRY_PENDING
        entry.merged_count = entry.source_count - len(entry.variants)
        if entry.merged_count > 0 and not entry.is_conflict:
            entry.notes.append(f"合并了 {entry.merged_count} 条完全相同重复行")

    for entry in standalone:
        entry.merged_count = 0
        entry.notes.append("未按包号和品目号合并")

    entries.extend(standalone)
    entries.sort(key=_entry_sort_key)
    return entries, duplicates


def _entry_sort_key(entry: RequirementEntry) -> tuple:
    if not entry.package_no:
        return (2, 0, _natural_key(entry.item_no), _natural_key(entry.name_or_empty))
    return (
        0,
        _natural_key(entry.package_no),
        _natural_key(entry.item_no),
        _natural_key(entry.name_or_empty),
    )


def _page_number_from(text: str | None, url: str) -> Optional[int]:
    cleaned = clean_text(text)
    match = re.search(r"第\s*(\d+)\s*页", cleaned)
    if match:
        return int(match.group(1))
    if re.fullmatch(r"\d+", cleaned):
        return int(cleaned)

    # A page number explicitly present in the URL is more trustworthy than
    # the anchor text (e.g. a "next" link whose label carries no number).
    parsed = urlsplit(url)
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in _PAGE_QUERY_KEYS and re.fullmatch(r"\d+", value.strip()):
            return int(value.strip())
    path = unquote(parsed.path)
    match = _PAGE_PATH_RE.search(path)
    if match:
        return int(match.group(1))
    return None


def find_pagination_links(
    soup: BeautifulSoup,
    *,
    page_url: str,
    origin: tuple[str, str],
    base_dir: str,
) -> list[tuple[Optional[int], str]]:
    """Find same-site, same-directory pagination links on a detail page."""

    links: dict[str, tuple[Optional[int], str]] = {}
    current_number = _page_number_from("", page_url)
    for anchor in soup.find_all("a", href=True):
        href = clean_text(anchor.get("href"))
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute_url = urljoin(page_url, href)
        parsed = urlsplit(absolute_url)
        if parsed.scheme not in {"http", "https"}:
            continue
        absolute_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, "")
        )
        if not same_site(absolute_url, origin):
            continue
        path = unquote(parsed.path)
        if posixpath.dirname(path) != base_dir:
            continue
        if canonical_url(absolute_url) == canonical_url(page_url):
            continue

        rel_value = anchor.get("rel", [])
        if isinstance(rel_value, str):
            rel_value = rel_value.split()
        rel = {str(item).lower() for item in rel_value}
        classes = " ".join(anchor.get("class", [])).lower()
        text = clean_text(anchor.get_text(" ", strip=True))
        nav = anchor.find_parent(["nav", "ul", "div"])
        nav_marker = ""
        if nav is not None:
            nav_marker = " ".join(nav.get("class", [])).lower() + " " + str(nav.get("id", "")).lower()

        number = _page_number_from(text, absolute_url)
        is_prev = bool(rel & {"prev", "previous"}) or bool(_PREV_TEXT_RE.match(text))
        if is_prev:
            continue

        pager_marker = bool(_PAGER_MARKER_RE.search(classes + " " + nav_marker))
        page_query = any(
            key.lower() in _PAGE_QUERY_KEYS
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        )
        page_path = bool(_PAGE_PATH_RE.search(path))
        next_text = bool(_NEXT_TEXT_RE.match(text))
        is_next = bool(rel & {"next"}) or next_text

        # Follow only links that either carry a page number in a pagination
        # context (numbered links / page query / page-like path) or explicitly
        # announce themselves as "next". Plain numeric anchor text outside a
        # pager could be an unrelated in-page link, so it is ignored.
        include = False
        if is_next:
            include = True
        elif number is not None and (page_query or page_path or pager_marker):
            include = True

        if not include:
            continue
        if current_number is not None and number is not None and number <= current_number:
            continue
        key = canonical_url(absolute_url)
        existing = links.get(key)
        if existing is None or (existing[0] is None and number is not None):
            links[key] = (number, absolute_url)

    return sorted(
        links.values(),
        key=lambda item: (item[0] is None, item[0] or 0),
    )


class RequirementsScraper:
    """Read requirement tables from project-related, paginated detail pages."""

    def __init__(
        self,
        *,
        project_no: str,
        list_url: str,
        fetcher: Optional[HttpFetcher] = None,
        max_pages: int = 15,
        max_details: int = 80,
        max_requirement_pages: int = 20,
    ) -> None:
        project_no = clean_text(project_no)
        if not project_no:
            raise InputError("请输入项目编号")
        self.project_no = project_no
        self.list_url = list_url
        self._owns_fetcher = fetcher is None
        self.fetcher = fetcher or HttpFetcher()
        self.max_requirement_pages = max_requirement_pages
        self.base = TenderScraper(
            project_no=project_no,
            list_url=list_url,
            fetcher=self.fetcher,
            max_pages=max_pages,
            max_details=max_details,
        )
        self.origin = self.base.origin

    def run(self) -> RequirementResult:
        try:
            return self._run()
        finally:
            if self._owns_fetcher:
                close = getattr(self.fetcher, "close", None)
                if close is not None:
                    close()

    def _run(self) -> RequirementResult:
        listing_pages, candidates, issues = self.base.listing_candidates()
        issues = list(issues)
        complete = not issues
        detail_pages: list[RequirementPage] = []
        all_rows: list[DataRow] = []
        tables_found = 0
        skipped_tables = 0
        seen_detail_urls: set[str] = set()
        processed_pages: set[str] = set()

        for candidate in candidates:
            candidate_key = canonical_url(candidate.url)
            if candidate_key in seen_detail_urls or candidate_key in processed_pages:
                # A continuation page may itself be listed as an announcement
                # link; it was already read as part of an earlier chain.
                continue
            seen_detail_urls.add(candidate_key)
            try:
                first_page = self.fetcher.fetch(candidate.url)
            except FetchError as exc:
                detail_pages.append(
                    RequirementPage(
                        url=candidate.url,
                        label=candidate.title,
                        fetch_ok=False,
                        error=str(exc),
                    )
                )
                issue = (
                    f"相关详情页抓取失败：{candidate.url}（{exc}），该页采购需求未纳入"
                )
                issues.append(issue)
                complete = False
                continue

            content = clean_text(first_page.soup.get_text(" ", strip=True))
            declared = extract_project_number(content)
            if not (
                candidate.related
                or project_mentioned(content, self.project_no)
                or (
                    declared
                    and normalize_project(declared) == normalize_project(self.project_no)
                )
            ):
                continue

            title = self.base._extract_detail_title(first_page.soup) or candidate.title
            chain, chain_issues = self._read_detail_chain(
                candidate.url,
                first_page,
                title,
            )
            if chain_issues:
                issues.extend(chain_issues)
                complete = False

            carry = PackageCarry()
            for page in chain:
                if not page.fetch_ok:
                    carry.begin_page(page)
                    issues.append(
                        f"采购需求分页抓取失败：{page.url}（{page.error}），"
                        "后续无包号行将标为待核实"
                    )
                    complete = False
                    continue

                if carry.begin_page(page):
                    page.is_continuation = True
                    issues.append(
                        f"采购需求分页不连续：{page.url}，此后的续表包号无法自动承接"
                    )
                    complete = False

                tables = extract_requirement_tables(
                    page.soup,
                    page_url=page.url,
                    page_label=page.label,
                )
                tables_found += len(tables)
                page.table_count = len(tables)
                if not tables:
                    skipped_tables += 1
                assign_packages(tables, carry)
                for table in tables:
                    all_rows.extend(table.rows)

            detail_pages.extend(chain)
            for page in chain:
                processed_pages.add(canonical_url(page.url))

        entries, duplicates = merge_rows(all_rows)
        if not entries:
            issues.append("未从相关公告中提取到采购需求明细表")
            complete = False
        if not complete:
            issues.append("存在分页或相关详情未成功读取，当前明细不完整")
        issues = _unique(issues)

        notes: list[str] = []
        notes.append(f"读取列表页 {len(listing_pages)} 个")
        notes.append(f"检查相关详情页 {len(detail_pages)} 个")
        notes.append(f"还原合并单元格后得到采购需求表 {tables_found} 张")
        if duplicates:
            notes.append(f"合并完全相同的重复行 {duplicates} 条")
        conflict_count = sum(1 for entry in entries if entry.status == ENTRY_CONFLICT)
        pending_count = sum(1 for entry in entries if entry.status == ENTRY_PENDING)
        if conflict_count:
            notes.append(f"字段冲突 {conflict_count} 条，已保留全部原文及来源")
        if pending_count:
            notes.append(f"包号待核实 {pending_count} 条")
        if not complete:
            notes.append("结果标记为不完整，请补齐缺失页后重新整理")

        return RequirementResult(
            project_no=self.project_no,
            list_url=self.list_url,
            checked_at=datetime.now(BEIJING_TZ),
            entries=entries,
            complete=complete,
            issues=issues,
            notes=notes,
            listing_pages=listing_pages,
            detail_pages=detail_pages,
            tables_found=tables_found,
            duplicate_rows_merged=duplicates,
            skipped_tables=skipped_tables,
        )

    def _read_detail_chain(
        self,
        start_url: str,
        first_page,
        title: str,
    ) -> tuple[list[RequirementPage], list[str]]:
        chain: list[RequirementPage] = []
        issues: list[str] = []
        base_dir = posixpath.dirname(unquote(urlsplit(start_url).path))
        start_number = _page_number_from("", start_url) or 1
        queue = deque([(start_url, start_number)])
        seen: set[str] = set()
        limit_hit = False

        fetched = {canonical_url(start_url): first_page}

        while queue:
            if len(chain) >= self.max_requirement_pages:
                limit_hit = True
                break
            url, number = queue.popleft()
            key = canonical_url(url)
            if key in seen:
                continue
            seen.add(key)

            page_data = fetched.pop(key, None)
            if page_data is None:
                try:
                    page_data = self.fetcher.fetch(url)
                except FetchError as exc:
                    chain.append(
                        RequirementPage(
                            url=url,
                            label=self._page_label(title, number),
                            fetch_ok=False,
                            error=str(exc),
                            page_no=number,
                        )
                    )
                    continue

            page_url = page_data.url
            page = RequirementPage(
                url=page_url,
                label=self._page_label(title, number),
                fetch_ok=True,
                page_no=number,
                detail_title=title,
                soup=page_data.soup,
            )
            chain.append(page)

            for link_number, link in find_pagination_links(
                page_data.soup,
                page_url=page_url,
                origin=self.origin,
                base_dir=base_dir,
            ):
                link_key = canonical_url(link)
                if link_key in seen:
                    continue
                if any(canonical_url(queued[0]) == link_key for queued in queue):
                    continue
                queue.append((link, link_number))

            if len(chain) > self.max_requirement_pages:
                limit_hit = True
                break

        if limit_hit:
            issues.append(
                f"采购需求分页超过上限 {self.max_requirement_pages}，可能存在未读取的明细"
            )
        return chain, issues

    @staticmethod
    def _page_label(title: str, number: Optional[int]) -> str:
        if number is None or number <= 1:
            return title
        return f"{title}（第 {number} 页）"


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
