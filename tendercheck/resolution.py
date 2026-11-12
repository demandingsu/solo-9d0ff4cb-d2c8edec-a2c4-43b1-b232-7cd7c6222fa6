from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable, Optional

from .models import (
    Announcement,
    CheckResult,
    KIND_CANCELLATION,
    KIND_CORRECTION,
    KIND_ORIGINAL,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_LABELS,
    STATUS_PENDING,
    TimelineConflict,
)
from .parsing import format_datetime, unique_preserve_order


def _sort_key(announcement: Announcement) -> tuple[int, datetime, str]:
    if announcement.publish_time is None:
        return (1, datetime.max.replace(tzinfo=None), announcement.title)
    return (0, announcement.publish_time, announcement.title)


def _detect_conflicts(announcements: Iterable[Announcement]) -> list[TimelineConflict]:
    groups: dict[datetime, list[Announcement]] = defaultdict(list)
    for announcement in announcements:
        if announcement.publish_time is not None and announcement.fetch_ok:
            # Announcement pages usually publish times to minute precision;
            # group by minute so 16:00:00 and 16:00:30 are still reviewed as
            # the same displayed publication moment.
            minute = announcement.publish_time.replace(second=0, microsecond=0)
            groups[minute].append(announcement)

    conflicts: list[TimelineConflict] = []
    for publish_time, group in groups.items():
        if len(group) < 2:
            continue
        kinds = {item.kind for item in group}
        correction_deadlines = {
            item.deadline
            for item in group
            if item.kind == KIND_CORRECTION and item.deadline is not None
        }
        original_deadlines = {
            item.deadline
            for item in group
            if item.kind == KIND_ORIGINAL and item.deadline is not None
        }

        reason: Optional[str] = None
        if len(correction_deadlines) > 1:
            reason = "同一发布时间存在多个截标时间不一致的更正公告"
        elif KIND_CANCELLATION in kinds and len(kinds) > 1:
            reason = "同一发布时间同时出现取消公告和其他类型公告"
        elif KIND_ORIGINAL in kinds and len(kinds) > 1:
            reason = "同一发布时间同时出现原公告和其他类型公告，先后关系无法确定"
        elif kinds == {KIND_ORIGINAL} and len(original_deadlines) > 1:
            reason = "同一发布时间存在多个截标时间不一致的原公告"

        if reason:
            conflicts.append(
                TimelineConflict(
                    publish_time=publish_time,
                    announcements=sorted(group, key=lambda item: item.title),
                    reason=reason,
                )
            )
    return conflicts


def resolve_check(
    *,
    project_no: str,
    list_url: str,
    announcements: list[Announcement],
    issues: list[str],
    pages: list[str],
    duplicates_skipped: int,
    detail_count: int,
    now: datetime,
) -> CheckResult:
    """Apply the business rules to a normalized announcement timeline."""

    timeline = sorted(announcements, key=_sort_key)
    result_issues = list(issues)
    conflicts = _detect_conflicts(timeline)

    for conflict in conflicts:
        titles = "、".join(f"《{item.title}》" for item in conflict.announcements)
        result_issues.append(
            f"{format_datetime(conflict.publish_time)}：{conflict.reason}（{titles}）"
        )

    failed_details = [item for item in timeline if not item.fetch_ok]
    if failed_details:
        for item in failed_details:
            result_issues.append(
                f"详情页抓取失败：《{item.title}》（{item.url}）{item.error or ''}"
            )

    if len(timeline) > 1 and any(item.publish_time is None for item in timeline):
        result_issues.append("存在缺少发布时间的相关公告，无法可靠排列公告先后关系")

    cancellations = [
        item
        for item in timeline
        if item.kind == KIND_CANCELLATION and item.fetch_ok
    ]
    cancellation_notice: Optional[Announcement] = None
    cancelled_at: Optional[datetime] = None
    ignored_after_cancel: list[Announcement] = []

    if cancellations:
        cancellation_notice = max(
            cancellations,
            key=lambda item: item.publish_time.timestamp() if item.publish_time else float("-inf"),
        )
        cancelled_at = cancellation_notice.publish_time
        if cancelled_at is not None:
            for item in timeline:
                if (
                    item.kind == KIND_CORRECTION
                    and item.publish_time is not None
                    and item.publish_time > cancelled_at
                ):
                    item.ignored_after_cancel = True
                    ignored_after_cancel.append(item)

    deadline_notices = [
        item
        for item in timeline
        if item.fetch_ok
        and item.kind in {KIND_ORIGINAL, KIND_CORRECTION}
        and item.deadline is not None
    ]
    deadline_notice: Optional[Announcement] = None
    deadline: Optional[datetime] = None
    if deadline_notices:
        deadline_notice = max(
            deadline_notices,
            key=lambda item: item.publish_time.timestamp() if item.publish_time else float("-inf"),
        )
        deadline = deadline_notice.deadline

    fatal = bool(result_issues or failed_details or conflicts)
    if not timeline:
        result_issues.append("未找到与项目编号匹配的原公告、更正公告或取消公告")
        fatal = True
    if len(timeline) > 1 and any(item.publish_time is None for item in timeline):
        fatal = True
    if not cancellations and timeline and deadline is None:
        result_issues.append("未能从相关公告中提取有效的投标/响应截止时间")
        fatal = True

    notes: list[str] = []
    if pages:
        notes.append(f"已核对 {len(pages)} 个列表页")
    notes.append(f"去重后纳入 {len(timeline)} 条相关公告")
    if duplicates_skipped:
        notes.append(f"跨页或镜像重复公告已合并 {duplicates_skipped} 条")
    if ignored_after_cancel:
        notes.append(
            f"取消公告之后发现 {len(ignored_after_cancel)} 条普通更正；按规则不恢复招标"
        )

    if fatal:
        status = STATUS_PENDING
        headline = "存在未决信息，暂不能确认当前是否仍可投标"
        # A pending result must not present a tentative deadline as definitive.
        display_deadline = None
    elif cancellations:
        status = STATUS_CANCELLED
        headline = "项目已发布取消公告，不再接受投标"
        display_deadline = None
    elif deadline is None:
        status = STATUS_PENDING
        headline = "缺少有效截标时间，暂不能确认"
        display_deadline = None
    elif deadline <= now:
        status = STATUS_EXPIRED
        headline = "当前截标时间已过"
        display_deadline = deadline
    else:
        status = STATUS_ACTIVE
        headline = "项目未取消，仍可按当前截标时间投标"
        display_deadline = deadline

    return CheckResult(
        project_no=project_no,
        list_url=list_url,
        status=status,
        status_label=STATUS_LABELS[status],
        headline=headline,
        checked_at=now,
        deadline=display_deadline,
        deadline_notice=deadline_notice,
        cancelled_at=cancelled_at,
        cancellation_notice=cancellation_notice,
        announcements=timeline,
        issues=unique_preserve_order(result_issues),
        notes=notes,
        conflicts=conflicts,
        pages=pages,
        duplicates_skipped=duplicates_skipped,
        detail_count=detail_count,
        ignored_after_cancel=ignored_after_cancel,
    )
