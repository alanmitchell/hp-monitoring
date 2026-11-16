"""The web app: the page, its JSON endpoints and the image files.

The page is three static files in `static/` (no build step) plus Plotly's
JavaScript, which is served from the installed `plotly` Python package.

A thin layer. The queries are in `heatpump.analysis.queries`; this module only
checks the request, calls them and shapes the answer. Times in requests are ISO
8601 with a time zone (``2026-11-14T05:00:00Z``); times in answers are epoch
milliseconds, UTC. Interactive documentation of the endpoints is at /docs.

The app may be published to the internet (Tailscale Funnel), with no login. So
everything here is read-only, every answer has a size limit, and error messages
say nothing about the machine.
"""

import importlib.util
import threading
from datetime import datetime
from pathlib import Path
from typing import Annotated

import duckdb
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from heatpump.analysis import queries
from heatpump.common import db
from heatpump.common.paths import image_root
from heatpump.common.timeutil import utc_now

STATIC_DIR = Path(__file__).parent / "static"

# Limits on one answer, so that no single request is expensive
MAX_POINTS_LIMIT = 5000  # per channel; the page asks for 2,600
MAX_IMAGES = 5000  # image records; longer ranges get every 2nd, 3rd, ... image

HEADERS = {
    "X-Content-Type-Options": "nosniff",  # browsers must not guess file types
    "X-Frame-Options": "DENY",  # other sites may not embed the page
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",  # keep the site out of search engines
}

When = Annotated[datetime, Query(description="ISO 8601 time with a time zone")]


class Database:
    """The app's one connection to `hp-db`, shared by all requests.

    Requests are handled in several threads, but a Quack connection can run only
    one query at a time, hence the lock. Queries take tens of milliseconds. If a
    query fails (for example because `hp-db` was restarted), it is tried once
    more on a new connection.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._con: duckdb.DuckDBPyConnection | None = None

    def run(self, query, *args, **kwargs):
        """Call ``query(connection, *args, **kwargs)``."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._con is None:
                        self._con = db.connect()
                    return query(self._con, *args, **kwargs)
                except duckdb.Error:
                    self.close()
                    if attempt == 2:
                        raise

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            except duckdb.Error:
                pass
            self._con = None


def create_app(max_gap: float = queries.DEFAULT_MAX_GAP) -> FastAPI:
    app = FastAPI(title="Heat pump monitoring")
    app.add_middleware(GZipMiddleware, minimum_size=2000)
    database = Database()

    @app.exception_handler(duckdb.Error)
    def database_unavailable(request: Request, exc: duckdb.Error):
        print(f"Database error on {request.url.path}: {exc}")  # details go to the log only
        return JSONResponse({"detail": "The database is unavailable"}, status_code=503)

    @app.middleware("http")
    async def add_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(HEADERS)
        return response

    @app.get("/api/sensors")
    def sensors() -> list[dict]:
        """The measurement channels."""
        return database.run(queries.sensors)

    @app.get("/api/latest")
    def latest() -> list[dict]:
        """Every channel with its newest reading from the last day (`ts` is null if none)."""
        newest = {row["sensor_id"]: row for row in database.run(queries.latest, utc_now())}
        empty = {"ts": None, "value": None, "quality": None}
        return [
            {**sensor, **newest.get(sensor["sensor_id"], empty)}
            for sensor in database.run(queries.sensors)
        ]

    @app.get("/api/readings")
    def readings(
        sensors: Annotated[str, Query(description="Channel names, separated by commas")],
        start: When,
        end: When,
        max_points: Annotated[int, Query(ge=10, le=MAX_POINTS_LIMIT)] = queries.DEFAULT_MAX_POINTS,
    ) -> dict:
        """Readings of the named channels for start <= time < end.

        A channel with more than `max_points` readings in the range is returned
        as buckets (`bucket_seconds` is set): `value` is the time-weighted
        average and `low`/`high` the extremes in each bucket. Otherwise `value`
        holds the stored readings. A null in `value` is a break in the line.
        """
        _check_range(start, end)
        catalog = {s["name"]: s for s in database.run(queries.sensors)}
        names = list(dict.fromkeys(n.strip() for n in sensors.split(",") if n.strip()))
        unknown = [n for n in names if n not in catalog]
        if unknown:
            raise HTTPException(404, f"No such channel: {', '.join(unknown)}")
        ids = [catalog[n]["sensor_id"] for n in names]
        found = database.run(queries.readings, ids, start, end, max_points, max_gap)
        return {
            "start": _ms(start),
            "end": _ms(end),
            "series": [
                {
                    "name": n,
                    "unit": catalog[n]["unit"],
                    "kind": catalog[n]["kind"],
                    "bucket_seconds": series.bucket_seconds,
                    "ts": series.ts,
                    "value": series.value,
                    "low": series.low,
                    "high": series.high,
                }
                for n in names
                for series in [found[catalog[n]["sensor_id"]]]
            ],
        }

    @app.get("/api/images")
    def images(start: When, end: When) -> list[dict]:
        """Images captured in start <= time < end, oldest first. Fetch one with /images/{image_id}.

        A range with more than 5,000 images returns every 2nd, 3rd, ... image.
        """
        _check_range(start, end)
        return database.run(queries.images, start, end, MAX_IMAGES)

    @app.get("/images/{image_id}", response_class=FileResponse)
    def image(image_id: int):
        """The JPG file of one image."""
        relpath = database.run(queries.image_path, image_id)
        if relpath is None:
            raise HTTPException(404, "No such image")
        root = image_root().resolve()
        path = (root / relpath).resolve()
        # The path comes from the database, but never serve a file outside the image root
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, "The image file is missing")
        # An image never changes, so the browser may keep it
        return FileResponse(
            path, media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    @app.get("/", include_in_schema=False)
    def page():
        # no-cache: the browser checks for a newer page after the code is updated
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/static/plotly.min.js", include_in_schema=False)
    def plotly_js():
        spec = importlib.util.find_spec("plotly")
        path = Path(spec.origin).parent / "package_data" / "plotly.min.js" if spec else None
        if path is None or not path.is_file():
            raise HTTPException(404, "plotly.min.js not found; run: uv sync --all-extras")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/robots.txt", include_in_schema=False)
    def robots():
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _check_range(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(
            422, "start and end need a time zone, for example 2026-11-14T05:00:00Z"
        )
    if end <= start:
        raise HTTPException(422, "end must be later than start")


def _ms(value: datetime) -> int:
    return round(value.timestamp() * 1000)
