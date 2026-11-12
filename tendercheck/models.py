from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


KIND_ORIGINAL = "original"
KIND_CORRECTION = "correction"
KIND_CANCELLATION = "cancellation"
KIND_OTHER = "other"

KIND_LABELS = {
    KIND_ORIGINAL: "原公告",
    KIND_CORRECTION: "更正公告",
    KIND_CANCELLATION: "取消公告",
    KIND_OTHER: "其他公告",
}

STATUS_ACTIVE = "active"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"
STATUS_PENDING = "pending"

STATUS_LABELS = {
    STATUS_ACTIVE: "可投标",
    STATUS_EXPIRED: "已截止",
    STATUS_CANCELLED: "已取消",
    STATUS_PENDING: "待核实",
}


@dataclass
class Announcement:
    """A normalized, de-duplicated announcement used in the decision chain."""

    url: str
    title: str
    kind: str
    publish_time: Optional[datetime]
    project_no: Optional[str] = None
    deadline: Optional[datetime] = None
    deadline_text: Optional[str] = None
    found_on: Optional[str] = None
    fetch_ok: bool = True
    error: Optional[str] = None
    source_note: Optional[str] = None
    ignored_after_cancel: bool = False

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, KIND_LABELS[KIND_OTHER])


@dataclass
class TimelineConflict:
    publish_time: datetime
    announcements: list[Announcement]
    reason: str


@dataclass
class CheckResult:
    project_no: str
    list_url: str
    status: str
    status_label: str
    headline: str
    checked_at: datetime
    deadline: Optional[datetime] = None
    deadline_notice: Optional[Announcement] = None
    cancelled_at: Optional[datetime] = None
    cancellation_notice: Optional[Announcement] = None
    announcements: list[Announcement] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    conflicts: list[TimelineConflict] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    duplicates_skipped: int = 0
    detail_count: int = 0
    ignored_after_cancel: list[Announcement] = field(default_factory=list)

    @property
    def announcement_count(self) -> int:
        return len(self.announcements)
