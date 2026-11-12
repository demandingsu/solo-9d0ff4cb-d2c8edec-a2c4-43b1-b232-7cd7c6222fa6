from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import (
    parse_qsl,
    unquote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

import requests
from bs4 import BeautifulSoup

from .models import Announcement, CheckResult, KIND_OTHER
from .parsing import (
    BEIJING_TZ,
    clean_text,
    detect_kind,
    extract_deadline,
    extract_explicit_kind,
    extract_project_number,
    extract_publish_time,
    normalize_project,
    parse_datetime,
    project_mentioned,
)
from .resolution import resolve_check

logger = logging.getLogger(__name__)


class InputError(ValueError):
    pass


class FetchError(RuntimeError):
    pass


class ListFetchError(RuntimeError):
    pass


@dataclass
class FetchedPage:
    url: str
    soup: BeautifulSoup


@dataclass
class Candidate:
    url: str
    title: str
    context: str
    found_on: str
    list_time: Optional[datetime]
    related: bool
    fallback: bool = False


_TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "spm",
}

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

_DOWNLOAD_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".zip",
    ".rar",
    ".7z",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
}

_PAGE_TEXT_RE = re.compile(
    r"^(?:第?\s*\d+\s*页?|\d+|上一页|上页|前一页|下一页|下页|后一页|首页|末页|尾页|next|prev|previous|»|›|>|<)$",
    re.IGNORECASE,
)

_PAGE_PATH_RE = re.compile(
    r"(?:^|/)(?:page|paged|list|index|notice|notices|announcement|announcements)[-_]?\d+\.(?:html?|shtml)$",
    re.IGNORECASE,
)


def validate_url(url: str) -> str:
    url = clean_text(url)
    if not url:
        raise InputError("请输入公告列表网址")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise InputError("公告列表网址必须是完整的 http:// 或 https:// 地址")
    try:
        parsed.port
    except ValueError as exc:
        raise InputError("网址端口无效") from exc
    if parsed.username or parsed.password:
        raise InputError("网址中不应包含用户名或密码")
    return url


def _normalized_netloc(parsed) -> str:
    host = (parsed.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if port and port != default_port:
        return f"{host}:{port}"
    return host


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    netloc = _normalized_netloc(parsed)
    path = parsed.path or "/"
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_QUERY_KEYS
    ]
    query = urlencode(sorted(query_pairs), doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def url_origin(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), _normalized_netloc(parsed)


def same_site(url: str, origin: tuple[str, str]) -> bool:
    return url_origin(url) == origin


class UrlMapper:
    """Map the browser-facing demo URL to the compose-internal hostname."""

    def __init__(self) -> None:
        self.public_base = os.getenv("SAMPLE_SITE_PUBLIC_URL", "").rstrip("/")
        self.internal_base = os.getenv("SAMPLE_SITE_INTERNAL_URL", "").rstrip("/")

    def to_fetch_url(self, logical_url: str) -> str:
        if self.public_base and self.internal_base:
            if logical_url == self.public_base:
                return self.internal_base
            if logical_url.startswith(self.public_base + "/"):
                return self.internal_base + logical_url[len(self.public_base) :]

            # Inside compose, localhost/127.0.0.1 refers to the web container,
            # not the sample-site container. Treat common loopback spellings
            # as the configured public demo URL.
            logical = urlsplit(logical_url)
            public = urlsplit(self.public_base)
            try:
                logical_port = logical.port
                public_port = public.port
            except ValueError:
                logical_port = None
                public_port = None
            if (
                logical.scheme == public.scheme
                and (logical.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
                and logical_port == public_port
            ):
                suffix = urlunsplit(("", "", logical.path or "/", logical.query, ""))
                return self.internal_base + suffix
        return logical_url

    def to_logical_url(self, fetch_url: str) -> str:
        if self.public_base and self.internal_base:
            if fetch_url == self.internal_base:
                return self.public_base
            if fetch_url.startswith(self.internal_base + "/"):
                return self.public_base + fetch_url[len(self.internal_base) :]
        return fetch_url


class HttpFetcher:
    def __init__(
        self,
        *,
        timeout: tuple[float, float] = (5.0, 15.0),
        max_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.mapper = UrlMapper()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "TenderDeadlineChecker/1.0 (+offline-demo)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            }
        )

    def fetch(self, logical_url: str) -> FetchedPage:
        fetch_url = self.mapper.to_fetch_url(logical_url)
        try:
            response = self.session.get(
                fetch_url,
                timeout=self.timeout,
                stream=True,
                allow_redirects=True,
            )
            with response:
                try:
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    status = response.status_code
                    raise FetchError(f"HTTP {status}") from exc

                content_type = response.headers.get("Content-Type", "").lower()
                if content_type and not any(
                    marker in content_type
                    for marker in ("text/html", "application/xhtml", "text/xml", "application/xml")
                ):
                    raise FetchError(f"响应不是 HTML 页面（{content_type.split(';')[0]}）")

                declared_length = response.headers.get("Content-Length")
                if declared_length and declared_length.isdigit() and int(declared_length) > self.max_bytes:
                    raise FetchError("页面超过大小限制")

                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise FetchError("页面超过大小限制")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                final_url = self.mapper.to_logical_url(response.url)
        except FetchError:
            raise
        except requests.Timeout as exc:
            raise FetchError("请求超时") from exc
        except requests.ConnectionError as exc:
            raise FetchError("无法连接目标站点") from exc
        except requests.RequestException as exc:
            raise FetchError(str(exc)) from exc

        # BeautifulSoup handles common Chinese encodings declared in meta tags.
        soup = BeautifulSoup(raw, "html.parser")
        return FetchedPage(url=final_url, soup=soup)

    def close(self) -> None:
        self.session.close()


class TenderScraper:
    def __init__(
        self,
        *,
        project_no: str,
        list_url: str,
        fetcher: Optional[HttpFetcher] = None,
        max_pages: int = 15,
        max_details: int = 80,
    ) -> None:
        project_no = clean_text(project_no)
        if not project_no:
            raise InputError("请输入项目编号")
        self.project_no = project_no
        self.list_url = validate_url(list_url)
        self._owns_fetcher = fetcher is None
        self.fetcher = fetcher or HttpFetcher()
        self.max_pages = max_pages
        self.max_details = max_details
        mapper = getattr(self.fetcher, "mapper", None)
        if mapper is not None:
            logical_start_url = mapper.to_logical_url(mapper.to_fetch_url(self.list_url))
        else:
            logical_start_url = self.list_url
        self.origin = url_origin(logical_start_url)

    def run(self) -> CheckResult:
        try:
            pages, candidates, issues, duplicate_links = self._crawl_listing_pages()
            selected, selection_issues = self._select_candidates(candidates)
            issues.extend(selection_issues)

            announcements, detail_issues, detail_count = self._fetch_details(selected)
            issues.extend(detail_issues)
            announcements, semantic_duplicates = self._dedupe_announcements(announcements)

            now = datetime.now(BEIJING_TZ)
            return resolve_check(
                project_no=self.project_no,
                list_url=self.list_url,
                announcements=announcements,
                issues=issues,
                pages=pages,
                duplicates_skipped=duplicate_links + semantic_duplicates,
                detail_count=detail_count,
                now=now,
            )
        finally:
            if self._owns_fetcher:
                close = getattr(self.fetcher, "close", None)
                if close is not None:
                    close()

    def _crawl_listing_pages(self) -> tuple[list[str], list[Candidate], list[str], int]:
        queue = [self.list_url]
        visited_pages: set[str] = set()
        pages: list[str] = []
        candidates_by_url: dict[str, Candidate] = {}
        issues: list[str] = []
        duplicate_links = 0

        while queue:
            if len(pages) >= self.max_pages:
                issues.append(f"分页数量超过上限 {self.max_pages}，未继续抓取")
                break
            page_url = queue.pop(0)
            page_key = canonical_url(page_url)
            if page_key in visited_pages:
                continue
            visited_pages.add(page_key)

            try:
                page = self.fetcher.fetch(page_url)
            except FetchError as exc:
                if not pages:
                    raise ListFetchError(f"无法抓取公告列表页：{exc}") from exc
                issues.append(f"分页抓取失败：{page_url}（{exc}）")
                continue

            page_url = page.url
            pages.append(page_url)
            page_candidates, page_links, page_duplicates = self._parse_listing_page(page)
            duplicate_links += page_duplicates

            for candidate in page_candidates:
                key = canonical_url(candidate.url)
                if key in candidates_by_url:
                    if candidate.related:
                        duplicate_links += 1
                    continue
                candidates_by_url[key] = candidate

            for link in page_links:
                if canonical_url(link) not in visited_pages and link not in queue:
                    queue.append(link)

        return pages, list(candidates_by_url.values()), issues, duplicate_links

    def _parse_listing_page(self, page: FetchedPage) -> tuple[list[Candidate], list[str], int]:
        soup = page.soup
        candidates: list[Candidate] = []
        page_links: list[str] = []
        duplicates = 0
        seen_on_page: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = clean_text(anchor.get("href"))
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            absolute_url = urljoin(page.url, href)
            parsed = urlsplit(absolute_url)
            if parsed.scheme not in {"http", "https"}:
                continue
            absolute_url = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, "")
            )
            if not same_site(absolute_url, self.origin):
                continue

            text = self._anchor_text(anchor)
            if self._is_pagination_link(anchor, absolute_url, text):
                if absolute_url not in page_links:
                    page_links.append(absolute_url)
                continue

            if self._is_site_chrome_link(anchor, absolute_url, text):
                continue
            if self._is_attachment_link(anchor, absolute_url, text):
                continue
            if self._is_download_or_non_detail(absolute_url):
                continue
            key = canonical_url(absolute_url)
            if key == canonical_url(page.url):
                continue

            container = self._listing_container(anchor)
            context = clean_text(container.get_text(" ", strip=True)) if container else text
            context = " ".join(
                part
                for part in [context, self._context_attributes(anchor, container), unquote(absolute_url)]
                if part
            )
            list_time = self._extract_list_time(container, context)
            related = project_mentioned(context, self.project_no)

            if key in seen_on_page:
                if related:
                    duplicates += 1
                continue
            seen_on_page.add(key)

            candidates.append(
                Candidate(
                    url=absolute_url,
                    title=text,
                    context=context,
                    found_on=page.url,
                    list_time=list_time,
                    related=related,
                )
            )

        return candidates, page_links, duplicates

    def _anchor_text(self, anchor) -> str:
        text = clean_text(anchor.get_text(" ", strip=True))
        if text:
            return text
        for attr in ("title", "aria-label", "data-title"):
            value = clean_text(anchor.get(attr))
            if value:
                return value
        parent = anchor.parent
        if parent and parent.name in {"h1", "h2", "h3", "h4", "strong"}:
            return clean_text(parent.get_text(" ", strip=True))
        return "未命名公告"

    def _listing_container(self, anchor):
        node = anchor
        for _ in range(8):
            node = node.parent
            if node is None or not getattr(node, "name", None):
                break
            if node.name in {"article", "li", "tr"}:
                return node
            classes = " ".join(node.get("class", [])).lower()
            node_id = str(node.get("id", "")).lower()
            marker = f"{classes} {node_id}"
            if re.search(r"(?:notice|announcement|tender|item|record|row|list-item|news-item)", marker):
                text_length = len(clean_text(node.get_text(" ", strip=True)))
                if text_length < 1500:
                    return node
        return anchor.parent

    def _context_attributes(self, anchor, container) -> str:
        values: list[str] = []
        nodes = [anchor]
        if container is not None:
            nodes.append(container)
        for node in nodes:
            for attr in (
                "data-project-no",
                "data-project",
                "data-project-code",
                "data-code",
                "data-id",
                "title",
            ):
                value = clean_text(node.get(attr)) if node and node.has_attr(attr) else ""
                if value:
                    values.append(value)
        return " ".join(values)

    def _extract_list_time(self, container, context: str) -> Optional[datetime]:
        if container is not None:
            time_tags = container.find_all("time")
            for time_tag in time_tags:
                parent_text = clean_text(time_tag.parent.get_text(" ", strip=True)) if time_tag.parent else ""
                if not re.search(r"(?:发布|公告|更新)", parent_text):
                    continue
                raw = clean_text(time_tag.get("datetime")) or clean_text(
                    time_tag.get_text(" ", strip=True)
                )
                parsed = parse_datetime(raw)
                if parsed:
                    return parsed
            for time_tag in time_tags:
                raw = clean_text(time_tag.get("datetime")) or clean_text(
                    time_tag.get_text(" ", strip=True)
                )
                parsed = parse_datetime(raw)
                if parsed:
                    return parsed
        return extract_publish_time(context)

    def _is_pagination_link(self, anchor, absolute_url: str, text: str) -> bool:
        rel_value = anchor.get("rel", [])
        if isinstance(rel_value, str):
            rel_value = rel_value.split()
        rel = {str(item).lower() for item in rel_value}
        classes = " ".join(anchor.get("class", [])).lower()
        text_key = clean_text(text)

        if rel & {"next", "prev", "previous"}:
            return True
        if re.search(r"(?:pagination|pager|page-link|page-item|next|prev)", classes) and (
            _PAGE_TEXT_RE.match(text_key) or _PAGE_PATH_RE.search(urlsplit(absolute_url).path)
        ):
            return True

        nav = anchor.find_parent("nav")
        if nav is not None:
            nav_marker = " ".join(nav.get("class", [])).lower() + " " + str(nav.get("id", "")).lower()
            if re.search(r"(?:pagination|pager|page)", nav_marker) and _PAGE_TEXT_RE.match(text_key):
                return True

        if _PAGE_TEXT_RE.match(text_key):
            parsed = urlsplit(absolute_url)
            query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
            if query_keys & _PAGE_QUERY_KEYS or _PAGE_PATH_RE.search(parsed.path):
                return True
        return False

    def _is_attachment_link(self, anchor, absolute_url: str, text: str) -> bool:
        normalized_text = clean_text(text).lower()
        if normalized_text in {
            "附件",
            "附件下载",
            "下载附件",
            "文件下载",
            "招标文件下载",
            "采购文件下载",
            "查看附件",
            "下载",
        }:
            return True
        classes = " ".join(anchor.get("class", [])).lower()
        if re.search(r"(?:download|attachment|file-download)", classes):
            return True
        path = urlsplit(absolute_url).path.lower()
        if re.search(r"/(?:download|attachment|file)(?:/|$)", path) and "公告" not in normalized_text:
            return True
        return False

    def _is_site_chrome_link(self, anchor, absolute_url: str, text: str) -> bool:
        """Skip site header/footer links that are not announcement records."""

        item_ancestor = anchor.find_parent(["article", "li", "tr"])
        chrome_ancestor = anchor.find_parent(["header", "footer"])
        if chrome_ancestor is not None and item_ancestor is None:
            return True

        normalized_text = clean_text(text).lower()
        if normalized_text in {
            "首页",
            "返回首页",
            "网站首页",
            "公告列表",
            "采购公告",
            "返回列表",
            "登录",
            "注册",
            "联系我们",
        }:
            return True

        path = urlsplit(absolute_url).path.lower()
        if path in {"/", "/index.html", "/index.htm"} and not project_mentioned(text, self.project_no):
            return True
        return False

    def _is_download_or_non_detail(self, absolute_url: str) -> bool:
        path = urlsplit(absolute_url).path.lower()
        _, extension = os.path.splitext(path)
        if extension in _DOWNLOAD_EXTENSIONS:
            return True
        if _PAGE_PATH_RE.search(path):
            return True
        return False

    def _select_candidates(self, candidates: list[Candidate]) -> tuple[list[Candidate], list[str]]:
        related = [candidate for candidate in candidates if candidate.related]
        if related:
            selected = related
        else:
            # Some real list pages only show a generic title and put the project
            # number in the detail page. Bound this fallback to keep it safe.
            selected = candidates[: self.max_details]
            for candidate in selected:
                candidate.fallback = True

        issues: list[str] = []
        if related and len(related) > self.max_details:
            issues.append(f"相关详情链接超过上限 {self.max_details}，可能存在未核对的公告")
        elif not related and len(candidates) > self.max_details:
            issues.append(f"候选详情链接超过上限 {self.max_details}，可能存在未核对的公告")
        if len(selected) > self.max_details:
            selected = selected[: self.max_details]
        return selected, issues

    def _fetch_details(self, candidates: list[Candidate]) -> tuple[list[Announcement], list[str], int]:
        announcements: list[Announcement] = []
        issues: list[str] = []
        detail_count = 0

        for candidate in candidates:
            detail_count += 1
            try:
                page = self.fetcher.fetch(candidate.url)
            except FetchError as exc:
                if candidate.related:
                    title = candidate.title
                    kind = detect_kind(title, candidate.context)
                    announcements.append(
                        Announcement(
                            url=candidate.url,
                            title=title,
                            kind=kind,
                            publish_time=candidate.list_time,
                            project_no=self.project_no,
                            found_on=candidate.found_on,
                            fetch_ok=False,
                            error=str(exc),
                            source_note="详情页抓取失败，标题/时间来自列表页",
                        )
                    )
                elif candidate.fallback:
                    issues.append(
                        f"候选详情页抓取失败，且列表页未标明项目编号，无法排除相关性：{candidate.url}（{exc}）"
                    )
                continue

            announcement = self._parse_detail_page(page, candidate)
            if announcement is not None:
                announcements.append(announcement)

        return announcements, issues, detail_count

    def _parse_detail_page(self, page: FetchedPage, candidate: Candidate) -> Optional[Announcement]:
        soup = page.soup
        content = clean_text(soup.get_text(" ", strip=True))
        title = self._extract_detail_title(soup) or candidate.title
        explicit_type = extract_explicit_kind(content)
        kind = detect_kind(title, content, explicit_type)
        if kind == KIND_OTHER:
            return None

        declared_project = extract_project_number(content)
        if declared_project and not project_mentioned(declared_project, self.project_no):
            return None
        detail_mentions_project = project_mentioned(f"{title} {content}", self.project_no)
        if not detail_mentions_project and not candidate.related:
            return None

        detail_publish_time = self._extract_detail_publish_time(soup, content)
        publish_time = detail_publish_time or candidate.list_time
        deadline, deadline_text = extract_deadline(content)
        source_note = None
        if detail_publish_time is None and candidate.list_time is not None:
            source_note = "发布时间取自列表页"

        return Announcement(
            url=page.url,
            title=title,
            kind=kind,
            publish_time=publish_time,
            project_no=declared_project or self.project_no,
            deadline=deadline,
            deadline_text=deadline_text,
            found_on=candidate.found_on,
            source_note=source_note,
        )

    def _extract_detail_title(self, soup: BeautifulSoup) -> Optional[str]:
        for selector in ("h1", ".article-title", ".detail-title", ".notice-title", ".title"):
            node = soup.select_one(selector)
            if node:
                text = clean_text(node.get_text(" ", strip=True))
                if text:
                    return text
        meta = soup.find("meta", attrs={"property": "og:title"}) or soup.find(
            "meta", attrs={"name": "title"}
        )
        if meta and meta.get("content"):
            return clean_text(meta.get("content"))
        if soup.title:
            text = clean_text(soup.title.get_text(" ", strip=True))
            return re.split(r"[-_—|]", text)[0].strip() or text
        return None

    def _extract_detail_publish_time(self, soup: BeautifulSoup, content: str) -> Optional[datetime]:
        meta_names = {
            "article:published_time",
            "pubdate",
            "publishdate",
            "publish_date",
            "date",
            "dc.date",
            "dc.date.issued",
        }
        for meta in soup.find_all("meta"):
            name = str(meta.get("property") or meta.get("name") or "").lower()
            if name in meta_names and meta.get("content"):
                parsed = parse_datetime(meta.get("content"))
                if parsed:
                    return parsed

        labelled = extract_publish_time(content)
        if labelled:
            return labelled

        for time_tag in soup.find_all("time"):
            raw = clean_text(time_tag.get("datetime")) or clean_text(
                time_tag.get_text(" ", strip=True)
            )
            parsed = parse_datetime(raw)
            if not parsed:
                continue
            parent_text = clean_text(time_tag.parent.get_text(" ", strip=True)) if time_tag.parent else ""
            marker = " ".join(time_tag.get("class", [])).lower()
            if re.search(r"(?:发布|公告|更新|publish|date|time)", parent_text + " " + marker, re.I):
                return parsed
        return None

    def _dedupe_announcements(self, announcements: list[Announcement]) -> tuple[list[Announcement], int]:
        result: list[Announcement] = []
        duplicates = 0

        for announcement in announcements:
            duplicate_index: Optional[int] = None
            for index, existing in enumerate(result):
                if not self._same_semantic_announcement(existing, announcement):
                    continue
                if existing.deadline != announcement.deadline and existing.deadline and announcement.deadline:
                    # Same title/time but different extracted deadlines can be a
                    # genuine same-timestamp conflict, so keep both.
                    continue
                duplicate_index = index
                break

            if duplicate_index is None:
                result.append(announcement)
                continue

            duplicates += 1
            existing = result[duplicate_index]
            # Prefer a successfully fetched/richer copy when mirrors differ.
            if (not existing.fetch_ok and announcement.fetch_ok) or (
                existing.deadline is None and announcement.deadline is not None
            ):
                result[duplicate_index] = announcement

        return result, duplicates

    def _same_semantic_announcement(self, left: Announcement, right: Announcement) -> bool:
        return (
            normalize_project(left.project_no) == normalize_project(right.project_no)
            and clean_text(left.title).casefold() == clean_text(right.title).casefold()
            and left.kind == right.kind
            and left.publish_time == right.publish_time
        )
