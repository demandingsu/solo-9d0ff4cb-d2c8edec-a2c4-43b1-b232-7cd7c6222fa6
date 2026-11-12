from __future__ import annotations

import re
import unicodedata
from datetime import datetime, time, timedelta, timezone
from typing import Iterable, Optional

from .models import (
    KIND_CANCELLATION,
    KIND_CORRECTION,
    KIND_ORIGINAL,
    KIND_OTHER,
)

# Procurement announcements in the target scenario use Beijing time. Keeping a
# fixed offset avoids depending on host tzdata inside a slim container.
BEIJING_TZ = timezone(timedelta(hours=8), name="UTC+08:00")

_DATE_RE = re.compile(
    r"(?P<year>\d{4})\s*(?:年|[./-])\s*"
    r"(?P<month>\d{1,2})\s*(?:月|[./-])\s*"
    r"(?P<day>\d{1,2})(?:\s*日)?"
)

_TIME_RE = re.compile(
    r"(?:^|[\sT(（,，])"
    r"(?P<ampm>上午|中午|下午|晚上)?\s*"
    r"(?P<hour>\d{1,2})\s*[时:：]\s*"
    r"(?P<minute>\d{1,2})(?:\s*分)?"
    r"(?:\s*[：:]\s*(?P<second>\d{1,2})\s*秒?)?"
)

_PUBLISH_LABEL_RE = re.compile(
    r"(?:发布日期|发布时间|公告日期|公告时间|公示日期|公示时间|更新日期|更新时间|上网时间)"
    r"\s*[：:：]?\s*"
)

_DEADLINE_LABEL_RE = re.compile(
    r"(?:投标|响应|报价|磋商|谈判|询价|开标|递交|提交|上传)"
    r"[^。；;\n]{0,35}?"
    r"(?:截止时间|截止日期|截止|时间)"
    r"|(?:截止时间|截止日期)"
)

_DEADLINE_EXCLUSION_RE = re.compile(
    r"(?:报名|获取|领取|下载|购买|质疑|提问|澄清|保证金|缴纳|付款|支付|交货|服务期|有效期|样品|资格审查|预审|答疑|异议)"
)

_EXTENSION_LABEL_RE = re.compile(
    r"(?:延期|顺延|推迟|延后)\s*(?:至|到)\s*[：:：]?"
    r"|(?:调整|变更|更改)\s*(?:至|到|为)\s*[：:：]?"
)

_CANCEL_RE = re.compile(r"(?:取消|终止|废标|流标|撤销)")
_CORRECTION_RE = re.compile(r"(?:更正|澄清|变更|补充|修改|答疑|延期)")
_RESULT_RE = re.compile(r"(?:中标|成交|结果|合同|验收|履约|公示)")
_ORIGINAL_RE = re.compile(
    r"(?:招标|采购|磋商|谈判|询价|比选|遴选|邀请|公开招标|竞争性)"
)

_PROJECT_LABEL_RE = re.compile(
    r"(?:项目|招标|采购|标段)(?:编号|编码|号)\s*[：:：]\s*"
    r"(?P<project>[A-Za-z0-9_\-./（）()（）]+)"
)

_EXPLICIT_TYPE_RE = re.compile(
    r"(?:公告类型|信息类型|通知类型|公告种类)\s*[：:：]\s*"
    r"(?P<kind>[^\s，。；;、]{1,20})"
)


def clean_text(value: str | None) -> str:
    """Collapse whitespace without rewriting source punctuation."""

    if not value:
        return ""
    value = value.replace("\xa0", " ").replace("　", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalize_project(value: str | None) -> str:
    """Normalize project numbers for case/width-insensitive matching."""

    value = unicodedata.normalize("NFKC", clean_text(value)).upper()
    value = value.translate(
        str.maketrans(
            {
                "－": "-",
                "—": "-",
                "–": "-",
                "‐": "-",
                "‑": "-",
                "‒": "-",
            }
        )
    )
    return re.sub(r"\s+", "", value)


def project_mentioned(text: str | None, project_no: str) -> bool:
    key = normalize_project(project_no)
    haystack = normalize_project(text)
    if not key or not haystack:
        return False

    start = haystack.find(key)
    while start != -1:
        end = start + len(key)
        before = haystack[start - 1] if start > 0 else ""
        after = haystack[end] if end < len(haystack) else ""
        before_ok = not before or not before.isalnum()
        after_ok = not after or not after.isalnum()
        if before_ok and after_ok:
            return True
        start = haystack.find(key, start + 1)
    return False


def extract_project_number(text: str | None) -> Optional[str]:
    if not text:
        return None
    match = _PROJECT_LABEL_RE.search(clean_text(text))
    if not match:
        return None
    return match.group("project").strip("，。；;、")


def extract_explicit_kind(text: str | None) -> Optional[str]:
    if not text:
        return None
    match = _EXPLICIT_TYPE_RE.search(clean_text(text))
    return match.group("kind") if match else None


def detect_kind(title: str | None, content: str | None = None, explicit_type: str | None = None) -> str:
    """Classify an announcement, with cancellation taking precedence."""

    title_text = clean_text(title)
    explicit_text = clean_text(explicit_type)
    content_text = clean_text(content)
    primary = f"{explicit_text} {title_text}"

    if _CANCEL_RE.search(primary):
        return KIND_CANCELLATION
    if _CORRECTION_RE.search(primary):
        return KIND_CORRECTION
    if _RESULT_RE.search(primary):
        return KIND_OTHER
    if _ORIGINAL_RE.search(primary) or _ORIGINAL_RE.search(content_text):
        return KIND_ORIGINAL
    return KIND_OTHER


def _valid_date(year: int, month: int, day: int) -> bool:
    try:
        datetime(year, month, day)
    except ValueError:
        return False
    return True


def _valid_time(hour: int, minute: int, second: int) -> bool:
    return 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59


def find_datetime_candidates(
    text: str | None,
    *,
    default_time: time = time(0, 0, 0),
) -> list[dict[str, object]]:
    """Find Chinese/ISO-like date candidates and their positions in text."""

    if not text:
        return []

    text = unicodedata.normalize("NFKC", clean_text(text))
    candidates: list[dict[str, object]] = []
    for match in _DATE_RE.finditer(text):
        year = int(match.group("year"))
        month = int(match.group("month"))
        day = int(match.group("day"))
        if not _valid_date(year, month, day):
            continue

        hour = default_time.hour
        minute = default_time.minute
        second = default_time.second
        tzinfo = BEIJING_TZ
        end = match.end()

        # A time immediately following the date is part of the same timestamp.
        tail = text[end : end + 34]
        tail_offset = 0
        weekday = re.match(r"^\s*[（(][^）)]{1,12}[）)]", tail)
        if weekday:
            tail_offset = weekday.end()
        time_match = _TIME_RE.match(tail[tail_offset:])
        if time_match:
            hour = int(time_match.group("hour"))
            minute = int(time_match.group("minute"))
            second = int(time_match.group("second") or 0)
            ampm = time_match.group("ampm")
            if ampm in {"下午", "晚上"} and hour < 12:
                hour += 12
            elif ampm == "中午" and hour < 12:
                hour += 12
            if not _valid_time(hour, minute, second):
                hour, minute, second = (
                    default_time.hour,
                    default_time.minute,
                    default_time.second,
                )
            else:
                end += tail_offset + time_match.end()
                timezone_match = re.match(
                    r"\s*(?P<zone>Z|[+-]\d{2}:?\d{2})",
                    tail[tail_offset + time_match.end() :],
                )
                if timezone_match:
                    zone = timezone_match.group("zone")
                    if zone == "Z":
                        tzinfo = timezone.utc
                        end += timezone_match.end()
                    else:
                        zone_digits = zone.replace(":", "")
                        sign = 1 if zone_digits.startswith("+") else -1
                        offset_hours = int(zone_digits[1:3])
                        offset_minutes = int(zone_digits[3:5])
                        if offset_hours <= 23 and offset_minutes <= 59:
                            tzinfo = timezone(
                                sign * timedelta(hours=offset_hours, minutes=offset_minutes)
                            )
                            end += timezone_match.end()

        candidates.append(
            {
                "start": match.start(),
                "end": end,
                "datetime": datetime(year, month, day, hour, minute, second, tzinfo=tzinfo).astimezone(BEIJING_TZ),
                "raw": text[match.start() : end],
            }
        )
    return candidates


def parse_datetime(
    text: str | None,
    *,
    default_time: time = time(0, 0, 0),
) -> Optional[datetime]:
    candidates = find_datetime_candidates(text, default_time=default_time)
    if not candidates:
        return None
    return candidates[0]["datetime"]  # type: ignore[return-value]


def extract_publish_time(text: str | None) -> Optional[datetime]:
    """Extract an explicitly labelled publication time from page text."""

    if not text:
        return None
    text = clean_text(text)
    for label in _PUBLISH_LABEL_RE.finditer(text):
        candidates = find_datetime_candidates(text[label.end() : label.end() + 60])
        if candidates:
            return candidates[0]["datetime"]  # type: ignore[return-value]
    return None


def extract_deadline(text: str | None) -> tuple[Optional[datetime], Optional[str]]:
    """Extract the effective bid/response deadline from announcement text.

    The last date after a deadline/extension label is used. This intentionally
    handles common correction wording such as “原定……，现延期至……”.
    """

    if not text:
        return None, None
    text = clean_text(text)
    found: list[dict[str, object]] = []

    for label in _DEADLINE_LABEL_RE.finditer(text):
        prefix = text[max(0, label.start() - 12) : label.start()]
        label_text = text[label.start() : label.end()]
        if _DEADLINE_EXCLUSION_RE.search(prefix + label_text):
            continue
        window = text[label.end() : label.end() + 110]
        window = re.split(r"[。；;]", window, maxsplit=1)[0]
        for candidate in find_datetime_candidates(
            window,
            default_time=time(23, 59, 59),
        ):
            candidate = dict(candidate)
            candidate["start"] = int(candidate["start"]) + label.end()
            candidate["end"] = int(candidate["end"]) + label.end()
            candidate["label_start"] = label.start()
            if re.search(r"(?:投标|响应|报价|磋商|谈判|询价|递交|提交|上传)", label_text):
                candidate["priority"] = 3
            elif "开标" in label_text:
                candidate["priority"] = 1
            else:
                candidate["priority"] = 2
            found.append(candidate)

    for label in _EXTENSION_LABEL_RE.finditer(text):
        window = text[label.end() : label.end() + 70]
        window = re.split(r"[。；;]", window, maxsplit=1)[0]
        extension_prefix = text[max(0, label.start() - 24) : label.start()]
        for candidate in find_datetime_candidates(
            window,
            default_time=time(23, 59, 59),
        ):
            candidate = dict(candidate)
            candidate["start"] = int(candidate["start"]) + label.end()
            candidate["end"] = int(candidate["end"]) + label.end()
            candidate["label_start"] = label.start()
            if re.search(r"(?:投标|响应|报价|磋商|谈判|询价|递交|提交|上传)", extension_prefix):
                candidate["priority"] = 3
            elif "开标" in extension_prefix:
                candidate["priority"] = 1
            else:
                candidate["priority"] = 2
            found.append(candidate)

    if not found:
        return None, None

    # Explicit bid/response labels outrank a later opening-time label. Within
    # the same priority, the final date in a correction sentence wins.
    chosen = max(
        found,
        key=lambda item: (int(item.get("priority", 0)), int(item["end"])),
    )
    label_start = int(chosen.get("label_start", chosen["start"]))
    snippet_start = max(0, label_start)
    snippet_end = min(len(text), int(chosen["end"]))
    snippet = text[snippet_start:snippet_end].strip(" ，。；;、")
    return chosen["datetime"], snippet  # type: ignore[return-value]


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")


def unique_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
