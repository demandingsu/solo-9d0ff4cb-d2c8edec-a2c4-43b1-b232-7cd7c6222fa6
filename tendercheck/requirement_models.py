from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

PACKAGE_CONFIRMED = "confirmed"
PACKAGE_INHERITED = "inherited"
PACKAGE_PENDING = "pending"

PACKAGE_STATUS_LABELS = {
    PACKAGE_CONFIRMED: "包号明确",
    PACKAGE_INHERITED: "续表承接",
    PACKAGE_PENDING: "待核实",
}

ENTRY_OK = "ok"
ENTRY_CONFLICT = "conflict"
ENTRY_PENDING = "pending"

ENTRY_STATUS_LABELS = {
    ENTRY_OK: "已整理",
    ENTRY_CONFLICT: "字段冲突",
    ENTRY_PENDING: "待核实",
}

FIELD_LABELS = {
    "package_no": "包号",
    "item_no": "品目号",
    "name": "名称",
    "quantity": "数量",
    "unit": "单位",
}


@dataclass
class DataRow:
    """One expanded data row from a requirement table."""

    package_no: str
    item_no: str
    name: str
    quantity: str
    unit: str
    source_url: str
    source_label: str
    source_row: str
    row_index: int
    package_status: str = ""
    package_note: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class ExtractedTable:
    """A requirement table after rowspan/colspan expansion."""

    caption: str
    page_url: str
    page_label: str
    table_index: int
    header_fields: dict[str, int]
    rows: list[DataRow]
    is_continuation: bool = False

    @property
    def has_package_column(self) -> bool:
        return "package_no" in self.header_fields


@dataclass
class RowSource:
    url: str
    label: str
    raw: str


@dataclass
class RequirementVariant:
    """A distinct field-value combination, with the original rows behind it."""

    name: str
    quantity: str
    unit: str
    sources: list[RowSource] = field(default_factory=list)


@dataclass
class RequirementEntry:
    package_no: str
    item_no: str
    package_status: str
    package_note: str
    status: str
    variants: list[RequirementVariant] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    merged_count: int = 0

    @property
    def package_status_label(self) -> str:
        return PACKAGE_STATUS_LABELS.get(self.package_status, "待核实")

    @property
    def status_label(self) -> str:
        return ENTRY_STATUS_LABELS.get(self.status, "待核实")

    @property
    def is_conflict(self) -> bool:
        return self.status == ENTRY_CONFLICT

    @property
    def source_count(self) -> int:
        return sum(len(variant.sources) for variant in self.variants)

    @property
    def name_or_empty(self) -> str:
        return self.variants[0].name if self.variants else ""


@dataclass
class RequirementPage:
    url: str
    label: str
    fetch_ok: bool = True
    error: Optional[str] = None
    table_count: int = 0
    detail_title: Optional[str] = None
    page_no: Optional[int] = None
    is_continuation: bool = False
    soup: object = field(default=None, repr=False, compare=False)

    @property
    def short_label(self) -> str:
        return self.label


@dataclass
class RequirementResult:
    project_no: str
    list_url: str
    checked_at: datetime
    entries: list[RequirementEntry] = field(default_factory=list)
    complete: bool = True
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    listing_pages: list[str] = field(default_factory=list)
    detail_pages: list[RequirementPage] = field(default_factory=list)
    tables_found: int = 0
    duplicate_rows_merged: int = 0
    skipped_tables: int = 0

    @property
    def completeness_label(self) -> str:
        return "完整" if self.complete else "不完整"

    @property
    def conflict_count(self) -> int:
        return sum(1 for entry in self.entries if entry.status == ENTRY_CONFLICT)

    @property
    def pending_count(self) -> int:
        return sum(1 for entry in self.entries if entry.status == ENTRY_PENDING)

    @property
    def package_count(self) -> int:
        return len(
            {
                entry.package_no
                for entry in self.entries
                if entry.package_no
            }
        )
