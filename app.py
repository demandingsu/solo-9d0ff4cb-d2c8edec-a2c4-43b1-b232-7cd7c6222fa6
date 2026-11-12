from __future__ import annotations

import logging
import os

from flask import Flask, render_template, request

from tendercheck.parsing import BEIJING_TZ, format_datetime
from tendercheck.scraper import InputError, ListFetchError, TenderScraper

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")

DEFAULT_PROJECT_NO = os.getenv("DEMO_PROJECT_NO", "GSB-2026-001")
DEFAULT_LIST_URL = os.getenv(
    "DEMO_LIST_URL",
    "http://localhost:8080/notices/page-1.html",
)


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return max(1, value)
    except ValueError:
        return default


@app.template_filter("dt")
def datetime_filter(value):
    return format_datetime(value)


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


@app.route("/", methods=["GET", "POST"])
def index():
    project_no = DEFAULT_PROJECT_NO
    list_url = DEFAULT_LIST_URL
    result = None
    error = None

    if request.method == "POST":
        project_no = request.form.get("project_no", "").strip()
        list_url = request.form.get("list_url", "").strip()
        try:
            scraper = TenderScraper(
                project_no=project_no,
                list_url=list_url,
                max_pages=_int_env("MAX_LIST_PAGES", 15),
                max_details=_int_env("MAX_DETAIL_PAGES", 80),
            )
            result = scraper.run()
        except (InputError, ListFetchError) as exc:
            error = str(exc)
        except Exception:
            logger.exception("tender check failed")
            error = "核对过程中发生未预期错误，请检查网址后重试。"

    return render_template(
        "index.html",
        project_no=project_no,
        list_url=list_url,
        result=result,
        error=error,
        beijing_tz=BEIJING_TZ,
    )


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=_int_env("PORT", 5000),
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
    )
