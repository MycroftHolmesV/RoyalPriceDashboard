from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
APP_VERSION = "0.6.1"
DATA_ROOT = Path(os.environ.get("ROYAL_PRICE_DATA_DIR", "/data"))
OPTIONS_FILE = Path(
    os.environ.get("ROYAL_PRICE_OPTIONS_FILE", "/data/options.json")
)
UPSTREAM_SCRIPT = Path(
    os.environ.get(
        "ROYAL_PRICE_UPSTREAM_SCRIPT",
        "/opt/upstream/RoyalPriceDashboardBrowse.py",
    )
)
UPSTREAM_COMMIT = "bf5212c26576d468a6af2043565ece2d01f8b503"
LISTEN_HOST = os.environ.get("ROYAL_PRICE_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("ROYAL_PRICE_PORT", "8099"))
ALLOWED_CLIENTS = frozenset(
    value.strip()
    for value in os.environ.get("ROYAL_PRICE_ALLOWED_CLIENTS", "").split(",")
    if value.strip()
)
HISTORY_RETENTION_DAYS_AFTER_SAILING = 30
STORAGE_WARNING_FREE_BYTES = 1 * 1024 * 1024 * 1024
STORAGE_CRITICAL_FREE_BYTES = 256 * 1024 * 1024
DISCOVERY_CACHE_SECONDS = 6 * 60 * 60
MANUAL_REFRESH_COOLDOWN_SECONDS = 10 * 60
REFRESH_FAILURE_BACKOFF_BASE_SECONDS = 15 * 60
REFRESH_FAILURE_BACKOFF_MAX_SECONDS = 6 * 60 * 60
MAX_REQUEST_BODY_BYTES = 1_000_000
MAX_CHUNK_LINE_BYTES = 8_192
MAX_TRAILER_BYTES = 16_384

ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
PRODUCT_RE = re.compile(
    r"^\s*(?P<name>.*?)\s+"
    r"(?:(?P<price>\d+(?:\.\d{2})?)\s+(?P<currency>[A-Z]{3})"
    r"(?:\s+per\s+(?P<unit>day|night))?|Price Not Available)\s+"
    r"\(prefix:\s*(?P<prefix>[^,]+),\s*product:\s*(?P<product>[^)]+)\)\s*$"
)
PRODUCT_DESCRIPTION_MARKER = "__ROYAL_PRICE_DASHBOARD_DESCRIPTION__ "
MAX_PRODUCT_DESCRIPTION_CHARS = 20_000

DEFAULT_OPTIONS: dict[str, Any] = {
    "ship": None,
    "sail_date": None,
    "currency": "USD",
    "watched_refresh_interval_hours": 12,
    "unwatched_refresh_interval_hours": 24,
    "notifications_enabled": True,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("royal-price-dashboard")


class DashboardError(RuntimeError):
    """A user-visible dashboard operation error."""


def cruise_request_log_context(raw: dict[str, Any]) -> dict[str, Any]:
    """Return bounded, non-sensitive cruise fields for rejection diagnostics."""
    context: dict[str, Any] = {}
    for field in (
        "client_version",
        "cruise_line",
        "ship_id",
        "ship",
        "sail_date",
    ):
        value = raw.get(field)
        if value is None or isinstance(value, (bool, int, float)):
            context[field] = value
        elif isinstance(value, str):
            context[field] = value[:160]
        else:
            context[field] = f"<{type(value).__name__}>"
    return context


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value).strip()


class _ProductDescriptionTextParser(HTMLParser):
    BLOCK_TAGS = frozenset(
        {
            "article",
            "br",
            "div",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
            "ol",
            "p",
            "section",
            "ul",
        }
    )
    IGNORED_TAGS = frozenset({"noscript", "script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        normalized = tag.casefold()
        if self.ignored_depth:
            self.ignored_depth += 1
        elif normalized in self.IGNORED_TAGS:
            self.ignored_depth = 1
        elif normalized in self.BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self.ignored_depth:
            self.ignored_depth -= 1
        elif tag.casefold() in self.BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def normalize_product_description(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None

    parser = _ProductDescriptionTextParser()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]*>", " ", value)

    text = text.replace("\u2014", " - ")
    text = "".join(
        character if ord(character) >= 32 or character in "\t\n\r" else " "
        for character in text
    )
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    return text[:MAX_PRODUCT_DESCRIPTION_CHARS].rstrip()


def parse_product_description_marker(line: str) -> tuple[str, str] | None:
    marker_index = line.find(PRODUCT_DESCRIPTION_MARKER)
    if marker_index < 0:
        return None
    encoded = line[marker_index + len(PRODUCT_DESCRIPTION_MARKER) :]
    try:
        payload = json.loads(encoded)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    product_id = str(payload.get("id") or "").strip()
    description = normalize_product_description(payload.get("description"))
    if not product_id or len(product_id) > 200 or description is None:
        return None
    return product_id, description


def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (OSError, json.JSONDecodeError) as error:
        LOGGER.warning("Could not read %s: %s", path, error)
        return copy.deepcopy(default)


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def format_storage_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            if unit == "bytes":
                whole_bytes = int(amount)
                label = "byte" if whole_bytes == 1 else "bytes"
                return f"{whole_bytes} {label}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def regular_file_tree_size(root: Path) -> int:
    """Measure regular files below root without following symbolic links."""
    if not root.exists():
        return 0
    total = 0
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
    return total


def sqlite_file_family_size(path: Path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.is_file() and not candidate.is_symlink():
            total += candidate.stat().st_size
    return total


def sailing_key(ship: str, sail_date: str, currency: str) -> str:
    return json.dumps(
        [ship.strip(), sail_date.strip(), currency.strip().upper()],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def cruise_duration(
    config: dict[str, Any],
    catalog: dict[str, Any] | None = None,
) -> int | None:
    raw_duration = config.get("duration")
    if raw_duration in (None, "") and isinstance(catalog, dict):
        sailing = catalog.get("sailing")
        if isinstance(sailing, dict):
            raw_duration = sailing.get("duration")

    if raw_duration not in (None, ""):
        try:
            duration = int(raw_duration)
        except (TypeError, ValueError):
            duration = 0
        if 1 <= duration <= 365:
            return duration

    descriptions = [config.get("description")]
    if isinstance(catalog, dict):
        sailing = catalog.get("sailing")
        if isinstance(sailing, dict):
            descriptions.append(sailing.get("description"))
    for description in descriptions:
        match = re.search(
            r"\b(\d{1,3})\s+(?:Night|Nt)\b",
            str(description or ""),
            flags=re.IGNORECASE,
        )
        if match:
            duration = int(match.group(1))
            if 1 <= duration <= 365:
                return duration
    return None


def cruise_completion(
    config: dict[str, Any],
    catalog: dict[str, Any] | None = None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Return the known return date and whether the cruise has returned."""
    duration = cruise_duration(config, catalog)
    if duration is None:
        return {"return_date": None, "completed": False}
    try:
        sail_date = date.fromisoformat(str(config.get("sail_date") or ""))
    except ValueError:
        return {"return_date": None, "completed": False}

    return_date = sail_date + timedelta(days=duration)
    current_date = today or datetime.now(timezone.utc).date()
    return {
        "return_date": return_date.isoformat(),
        "completed": current_date >= return_date,
    }


def load_options(path: Path) -> dict[str, Any]:
    raw = read_json(path, {})
    if not isinstance(raw, dict):
        raise DashboardError("App options must be a JSON object.")
    options = {**DEFAULT_OPTIONS, **raw}

    raw_ship = options.get("ship")
    raw_sail_date = options.get("sail_date")
    ship = str(raw_ship).strip() if raw_ship is not None else None
    sail_date = str(raw_sail_date).strip() if raw_sail_date is not None else None
    if bool(ship) != bool(sail_date):
        raise DashboardError(
            "Legacy ship and sail_date options must either both be set or both be empty."
        )
    if ship and len(ship) > 120:
        raise DashboardError("The configured ship name is invalid.")
    if sail_date:
        try:
            date.fromisoformat(sail_date)
        except ValueError as error:
            raise DashboardError("sail_date must use YYYY-MM-DD.") from error

    currency = str(options["currency"]).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise DashboardError("currency must be a three-letter code.")

    def refresh_interval(option_name: str, value: Any) -> int:
        try:
            interval = int(value)
        except (TypeError, ValueError) as error:
            raise DashboardError(f"{option_name} must be a whole number.") from error
        if not 1 <= interval <= 168:
            raise DashboardError(f"{option_name} must be between 1 and 168.")
        return interval

    watched_interval = refresh_interval(
        "watched_refresh_interval_hours",
        options["watched_refresh_interval_hours"],
    )
    unwatched_interval = refresh_interval(
        "unwatched_refresh_interval_hours",
        raw.get(
            "unwatched_refresh_interval_hours",
            raw.get(
                "refresh_interval_hours",
                DEFAULT_OPTIONS["unwatched_refresh_interval_hours"],
            ),
        ),
    )

    return {
        "ship": ship,
        "sail_date": sail_date,
        "currency": currency,
        "watched_refresh_interval_hours": watched_interval,
        "unwatched_refresh_interval_hours": unwatched_interval,
        "notifications_enabled": bool(options["notifications_enabled"]),
    }


def browser_ship_argument(ship: str) -> str:
    value = re.sub(r"\s+of the Seas$", "", ship.strip(), flags=re.IGNORECASE)
    value = re.sub(r"^Celebrity\s+", "", value, flags=re.IGNORECASE)
    return value.strip()


def browser_date_argument(iso_date: str) -> str:
    parsed = date.fromisoformat(iso_date)
    return parsed.strftime("%m/%d/%y")


def cruise_line_for_ship(ship: str) -> str:
    normalized = ship.strip().casefold()
    if normalized.startswith("celebrity "):
        return "celebrity"
    if normalized.endswith(" of the seas"):
        return "royal-caribbean"
    raise DashboardError(
        "The ship name does not identify a Royal Caribbean or Celebrity ship."
    )


def cruise_identifier(ship: str, sail_date: str, currency: str) -> str:
    digest = hashlib.sha256(
        sailing_key(ship, sail_date, currency).encode("utf-8")
    ).hexdigest()
    return f"c-{digest[:16]}"


def validate_cruise_config(
    raw: dict[str, Any],
    *,
    defaults: dict[str, Any] | None = None,
    require_future: bool = False,
) -> dict[str, Any]:
    fallback = defaults or DEFAULT_OPTIONS
    ship = str(raw.get("ship") or "").strip()
    if not ship or len(ship) > 120 or any(ord(character) < 32 for character in ship):
        raise DashboardError("Choose a valid ship.")

    inferred_line = cruise_line_for_ship(ship)
    cruise_line = str(raw.get("cruise_line") or inferred_line).strip().casefold()
    if cruise_line not in {"royal-caribbean", "celebrity"}:
        raise DashboardError("Choose Royal Caribbean or Celebrity.")
    if cruise_line != inferred_line:
        raise DashboardError("The selected ship does not belong to that cruise line.")

    sail_date = str(raw.get("sail_date") or "").strip()
    try:
        parsed_sail_date = date.fromisoformat(sail_date)
    except ValueError as error:
        raise DashboardError("sail_date must use YYYY-MM-DD.") from error
    if require_future and parsed_sail_date < datetime.now(timezone.utc).date():
        raise DashboardError("Choose a sailing that has not departed.")

    currency = str(raw.get("currency") or fallback.get("currency") or "USD")
    currency = currency.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise DashboardError("currency must be a three-letter code.")

    try:
        interval = int(
            raw.get(
                "refresh_interval_hours",
                fallback.get(
                    "unwatched_refresh_interval_hours",
                    DEFAULT_OPTIONS["unwatched_refresh_interval_hours"],
                ),
            )
        )
    except (TypeError, ValueError) as error:
        raise DashboardError("refresh_interval_hours must be a whole number.") from error
    if not 1 <= interval <= 168:
        raise DashboardError("refresh_interval_hours must be between 1 and 168.")

    raw_duration = raw.get("duration")
    duration: int | None = None
    if raw_duration not in (None, ""):
        try:
            duration = int(raw_duration)
        except (TypeError, ValueError) as error:
            raise DashboardError("The sailing duration must be a whole number.") from error
        if not 1 <= duration <= 365:
            raise DashboardError("The sailing duration is invalid.")

    description = str(raw.get("description") or "").strip()
    if len(description) > 500:
        raise DashboardError("The sailing description is too long.")

    created_at = str(raw.get("created_at") or utc_now())
    return {
        "id": cruise_identifier(ship, sail_date, currency),
        "cruise_line": cruise_line,
        "ship": ship,
        "sail_date": sail_date,
        "duration": duration,
        "description": description or None,
        "currency": currency,
        "refresh_interval_hours": interval,
        "notifications_enabled": bool(
            raw.get(
                "notifications_enabled",
                fallback.get("notifications_enabled", True),
            )
        ),
        "created_at": created_at,
    }


def _run_upstream_menu(arguments: list[str], timeout_seconds: int = 90) -> str:
    if not UPSTREAM_SCRIPT.is_file():
        raise DashboardError(f"Upstream browser is missing: {UPSTREAM_SCRIPT}")
    try:
        completed = subprocess.run(
            [sys.executable, str(UPSTREAM_SCRIPT), *arguments],
            cwd=UPSTREAM_SCRIPT.parent,
            input="q\n\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise DashboardError("Cruise discovery timed out.") from error
    except OSError as error:
        raise DashboardError(f"Could not start cruise discovery: {error}") from error
    if completed.returncode != 0:
        tail = "\n".join(strip_ansi(line) for line in completed.stdout.splitlines()[-8:])
        raise DashboardError(
            f"Cruise discovery exited with code {completed.returncode}.\n{tail}"
        )
    return completed.stdout


def parse_ship_menu(output: str) -> list[dict[str, str]]:
    in_menu = False
    ships: list[dict[str, str]] = []
    for raw_line in output.splitlines():
        line = strip_ansi(raw_line)
        if line == "Select Ship:":
            in_menu = True
            continue
        if not in_menu:
            continue
        if re.match(r"^[qQ]\)", line):
            break
        match = re.match(r"^\d+\)\s+(.+?)\s*$", line)
        if not match:
            continue
        name = match.group(1)
        try:
            cruise_line = cruise_line_for_ship(name)
        except DashboardError:
            continue
        ships.append(
            {
                "id": hashlib.sha256(name.encode("utf-8")).hexdigest()[:12],
                "name": name,
                "cruise_line": cruise_line,
            }
        )
    if not ships:
        raise DashboardError("The upstream browser returned no ships.")
    return ships


def parse_sailing_menu(output: str) -> list[dict[str, Any]]:
    in_menu = False
    sailings: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = strip_ansi(raw_line)
        if line == "Select sailing:":
            in_menu = True
            continue
        if not in_menu:
            continue
        if re.match(r"^[qQ]\)", line):
            break
        match = re.match(
            r"^\d+\)\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+(.+?)\s*$",
            line,
        )
        if not match:
            continue
        display_date, description = match.groups()
        parsed_date: date | None = None
        for date_format in ("%m/%d/%y", "%m/%d/%Y"):
            try:
                parsed_date = datetime.strptime(display_date, date_format).date()
                break
            except ValueError:
                continue
        if parsed_date is None:
            continue
        duration_match = re.search(
            r"\b(\d+)\s+(?:Night(?:s)?|Nt)\b",
            description,
            re.IGNORECASE,
        )
        sailings.append(
            {
                "sail_date": parsed_date.isoformat(),
                "display_date": display_date,
                "duration": int(duration_match.group(1)) if duration_match else None,
                "description": description,
            }
        )
    if not sailings:
        raise DashboardError("The upstream browser returned no sailings for that ship.")
    return sailings


def parse_browser_output(
    output: str,
    *,
    ship: str,
    sail_date: str,
    currency: str,
) -> dict[str, Any]:
    category = "Other"
    subcategory: str | None = None
    products_started = False
    itinerary: list[str] = []
    sailing_description: str | None = None
    descriptions: dict[str, str] = {}
    products: dict[str, dict[str, Any]] = {}

    for raw_line in output.splitlines():
        has_blue_heading = "\x1b[94m" in raw_line and "(prefix:" not in raw_line
        line = strip_ansi(raw_line)
        if not line:
            continue

        parsed_description = parse_product_description_marker(line)
        if parsed_description is not None:
            product_id, description = parsed_description
            descriptions[product_id] = description
            continue

        if line.startswith("Browsing for "):
            sailing_description = line.removeprefix("Browsing for ")

        if line.startswith("Gathering list of products"):
            products_started = True

        if not products_started and re.match(r"^Day \d+ \(", line):
            itinerary.append(line)

        if has_blue_heading:
            if re.match(r"^Day \d+:", line):
                subcategory = line
            else:
                category = line
                subcategory = None
            continue

        match = PRODUCT_RE.match(line)
        if not match:
            continue

        product_code = match.group("product").strip()
        parsed_price = match.group("price")
        item = {
            "id": product_code,
            "name": match.group("name").strip(),
            "category": category,
            "subcategory": subcategory,
            "prefix": match.group("prefix").strip(),
            "product": product_code,
            "price": float(parsed_price) if parsed_price is not None else None,
            "currency": (match.group("currency") or currency).upper(),
            "unit": match.group("unit"),
            "price_available": parsed_price is not None,
            "description": None,
        }

        existing = products.get(product_code)
        if existing is None:
            products[product_code] = item
        elif existing["price"] is None and item["price"] is not None:
            existing["price"] = item["price"]
            existing["currency"] = item["currency"]
            existing["unit"] = item["unit"]
            existing["price_available"] = True

    for product_id, description in descriptions.items():
        item = products.get(product_id)
        if item is not None:
            item["description"] = description

    if not products:
        tail = "\n".join(strip_ansi(line) for line in output.splitlines()[-12:])
        raise DashboardError(
            "The browser returned no products. Its final output was:\n" + tail
        )

    ordered = sorted(
        products.values(),
        key=lambda item: (
            item["category"].casefold(),
            (item["subcategory"] or "").casefold(),
            item["name"].casefold(),
        ),
    )
    return {
        "generated_at": utc_now(),
        "source": {
            "name": "BrowseRoyalCaribbeanPrice.py",
            "commit": UPSTREAM_COMMIT,
            "pricing": "public",
        },
        "sailing": {
            "ship": ship,
            "sail_date": sail_date,
            "currency": currency,
            "description": sailing_description,
            "itinerary": itinerary,
        },
        "items": ordered,
    }


def run_browser(options: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    if not UPSTREAM_SCRIPT.is_file():
        raise DashboardError(f"Upstream browser is missing: {UPSTREAM_SCRIPT}")

    command = [
        sys.executable,
        str(UPSTREAM_SCRIPT),
        "-c",
        options["currency"],
        "-s",
        browser_ship_argument(options["ship"]),
        "-d",
        browser_date_argument(options["sail_date"]),
        "-w",
        "-k",
        "alpha",
    ]
    LOGGER.info(
        "Refreshing public catalog for %s on %s",
        options["ship"],
        options["sail_date"],
    )
    try:
        completed = subprocess.run(
            command,
            cwd=UPSTREAM_SCRIPT.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15 * 60,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise DashboardError("The price browser timed out after 15 minutes.") from error
    except OSError as error:
        raise DashboardError(f"Could not start the price browser: {error}") from error

    catalog = parse_browser_output(
        completed.stdout,
        ship=options["ship"],
        sail_date=options["sail_date"],
        currency=options["currency"],
    )
    warning = None
    if completed.returncode != 0:
        warning = (
            f"The browser exited with code {completed.returncode} after returning "
            f"{len(catalog['items'])} products."
        )
        LOGGER.warning(warning)
    return catalog, warning


class HistoryStore:
    """Compact, per-sailing price and availability change history."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            existing_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"
            ).fetchone()
            if existing_table is None:
                connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY,
                    sailing_key TEXT NOT NULL,
                    ship TEXT NOT NULL,
                    sail_date TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    price REAL,
                    available INTEGER NOT NULL CHECK (available IN (0, 1)),
                    UNIQUE (sailing_key, product_id, observed_at)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS price_history_lookup
                ON price_history (sailing_key, product_id, observed_at, id)
                """
            )
            connection.execute("PRAGMA user_version = 1")

    def _reclaim_unused_pages(self) -> None:
        """Return deleted pages to the filesystem for new incremental databases."""
        try:
            with self._connect() as connection:
                mode = int(connection.execute("PRAGMA auto_vacuum").fetchone()[0])
                if mode == 2:
                    free_pages = int(
                        connection.execute("PRAGMA freelist_count").fetchone()[0]
                    )
                    for _ in range(free_pages):
                        connection.execute("PRAGMA incremental_vacuum(1)")
                    connection.commit()
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as error:
            LOGGER.warning("Could not reclaim unused price-history pages: %s", error)

    @staticmethod
    def _sailing_details(
        catalog: dict[str, Any],
        options: dict[str, Any],
    ) -> tuple[str, str, str, str]:
        sailing = catalog.get("sailing", {})
        ship = str(sailing.get("ship") or options["ship"]).strip()
        sail_date = str(sailing.get("sail_date") or options["sail_date"]).strip()
        currency = str(sailing.get("currency") or options["currency"]).strip().upper()
        date.fromisoformat(sail_date)
        return sailing_key(ship, sail_date, currency), ship, sail_date, currency

    def record_catalog(
        self,
        catalog: dict[str, Any],
        options: dict[str, Any],
    ) -> int:
        items = catalog.get("items", [])
        if not items:
            return 0

        key, ship, sail_date, currency = self._sailing_details(catalog, options)
        observed_at = str(catalog.get("generated_at") or utc_now())
        inserted = 0
        with self._connect() as connection:
            latest_rows = connection.execute(
                """
                SELECT history.product_id, history.price, history.available
                FROM price_history AS history
                JOIN (
                    SELECT product_id, MAX(id) AS latest_id
                    FROM price_history
                    WHERE sailing_key = ?
                    GROUP BY product_id
                ) AS latest ON latest.latest_id = history.id
                """,
                (key,),
            ).fetchall()
            latest = {row["product_id"]: row for row in latest_rows}

            for item in items:
                product_id = str(item["id"])
                raw_price = item.get("price")
                available = raw_price is not None and bool(
                    item.get("price_available", True)
                )
                price = round(float(raw_price), 2) if available else None
                previous = latest.get(product_id)
                if previous is not None:
                    same_availability = bool(previous["available"]) == available
                    prior_price = previous["price"]
                    same_price = (
                        prior_price is None
                        if price is None
                        else prior_price is not None
                        and abs(float(prior_price) - price) < 0.0001
                    )
                    if same_availability and same_price:
                        continue

                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO price_history (
                        sailing_key, ship, sail_date, currency,
                        product_id, observed_at, price, available
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        ship,
                        sail_date,
                        currency,
                        product_id,
                        observed_at,
                        price,
                        int(available),
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def get_history(
        self,
        *,
        item: dict[str, Any],
        catalog: dict[str, Any],
        options: dict[str, Any],
    ) -> dict[str, Any]:
        key, ship, sail_date, currency = self._sailing_details(catalog, options)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT observed_at, price, available
                FROM price_history
                WHERE sailing_key = ? AND product_id = ?
                ORDER BY observed_at, id
                """,
                (key, str(item["id"])),
            ).fetchall()

        points = [
            {
                "observed_at": row["observed_at"],
                "price": float(row["price"]) if row["price"] is not None else None,
                "available": bool(row["available"]),
            }
            for row in rows
        ]
        available_prices = [
            point["price"]
            for point in points
            if point["available"] and point["price"] is not None
        ]
        return {
            "item": copy.deepcopy(item),
            "sailing": {
                "ship": ship,
                "sail_date": sail_date,
                "currency": currency,
            },
            "mode": "changes_only",
            "retention_days_after_sailing": HISTORY_RETENTION_DAYS_AFTER_SAILING,
            "summary": {
                "events": len(points),
                "first_price": available_prices[0] if available_prices else None,
                "lowest_price": min(available_prices) if available_prices else None,
                "highest_price": max(available_prices) if available_prices else None,
                "current_price": (
                    points[-1]["price"]
                    if points and points[-1]["available"]
                    else None
                ),
            },
            "points": points,
        }

    def get_changes(
        self,
        *,
        catalog: dict[str, Any],
        options: dict[str, Any],
    ) -> dict[str, Any]:
        """Return actual changes, excluding each product's initial baseline."""
        key, _ship, _sail_date, _currency = self._sailing_details(catalog, options)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, product_id, observed_at, price, available
                FROM price_history
                WHERE sailing_key = ?
                ORDER BY product_id, observed_at, id
                """,
                (key,),
            ).fetchall()

        prior_by_product: dict[str, sqlite3.Row] = {}
        available_prices_by_product: dict[str, list[float]] = {}
        changes: list[dict[str, Any]] = []
        latest_price_changes: dict[str, dict[str, Any]] = {}
        for row in rows:
            product_id = str(row["product_id"])
            if bool(row["available"]) and row["price"] is not None:
                available_prices_by_product.setdefault(product_id, []).append(
                    float(row["price"])
                )
            previous = prior_by_product.get(product_id)
            prior_by_product[product_id] = row
            if previous is None:
                continue

            previous_price = (
                float(previous["price"])
                if previous["price"] is not None
                else None
            )
            price = float(row["price"]) if row["price"] is not None else None
            event = {
                "product_id": product_id,
                "observed_at": row["observed_at"],
                "previous_price": previous_price,
                "price": price,
                "previous_available": bool(previous["available"]),
                "available": bool(row["available"]),
                "price_delta": (
                    round(price - previous_price, 2)
                    if price is not None and previous_price is not None
                    else None
                ),
                "_history_id": int(row["id"]),
            }
            changes.append(event)
            if event["price_delta"] not in (None, 0):
                latest_price_changes[product_id] = event

        changes.sort(
            key=lambda event: (event["observed_at"], event["_history_id"]),
            reverse=True,
        )
        for event in changes:
            event.pop("_history_id", None)
        price_stats = {
            product_id: {
                "recorded_price_count": len(prices),
                "average_price": round(sum(prices) / len(prices), 2),
                "lowest_price": min(prices),
                "highest_price": max(prices),
            }
            for product_id, prices in available_prices_by_product.items()
            if prices
        }
        return {
            "changes": changes,
            "latest_price_changes": latest_price_changes,
            "price_stats": price_stats,
        }

    def delete_sailing(self, options: dict[str, Any]) -> int:
        key = sailing_key(
            str(options["ship"]),
            str(options["sail_date"]),
            str(options["currency"]),
        )
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM price_history WHERE sailing_key = ?",
                (key,),
            )
            deleted = cursor.rowcount
        if deleted:
            self._reclaim_unused_pages()
        return deleted

    def purge_expired(self, today: date | None = None) -> int:
        current_date = today or datetime.now(timezone.utc).date()
        cutoff = current_date - timedelta(
            days=HISTORY_RETENTION_DAYS_AFTER_SAILING
        )
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM price_history WHERE sail_date <= ?",
                (cutoff.isoformat(),),
            )
            deleted = cursor.rowcount
        if deleted:
            self._reclaim_unused_pages()
        return deleted


class CruiseRuntime:
    def __init__(self, data_root: Path, config: dict[str, Any]) -> None:
        self.config = config
        self.directory = data_root / "cruises" / config["id"]
        self.catalog_file = self.directory / "catalog.json"
        self.preferences_file = self.directory / "preferences.json"
        self.catalog = read_json(self.catalog_file, {"items": []})
        if not isinstance(self.catalog, dict):
            self.catalog = {"items": []}
        self.catalog.setdefault("items", [])
        self.preferences = read_json(
            self.preferences_file,
            {"pinned": [], "watching": {}},
        )
        if not isinstance(self.preferences, dict):
            self.preferences = {"pinned": [], "watching": {}}
        self.preferences.pop("hidden", None)
        if not isinstance(self.preferences.get("pinned"), list):
            self.preferences["pinned"] = []
        if not isinstance(self.preferences.get("watching"), dict):
            self.preferences["watching"] = {}
        self.refreshing = False
        self.last_error: str | None = None
        self.last_warning: str | None = None
        self.last_refresh_started_at: datetime | None = None
        self.next_refresh_allowed_at: datetime | None = None
        self.consecutive_refresh_failures = 0


class CatalogManager:
    def __init__(self, data_root: Path, options_file: Path) -> None:
        self.data_root = data_root
        self.registry_file = data_root / "cruises.json"
        self.legacy_catalog_file = data_root / "catalog.json"
        self.legacy_preferences_file = data_root / "preferences.json"
        self.history_file = data_root / "price-history.sqlite3"
        self.app_options = load_options(options_file)
        self.lock = threading.RLock()
        self.refresh_gate = threading.Lock()
        self.discovery_lock = threading.Lock()
        self._ship_cache: tuple[float, list[dict[str, str]]] | None = None
        self._sailing_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._cruises: dict[str, CruiseRuntime] = {}
        self.active_cruise_id: str | None = None
        self.history_error: str | None = None
        self.last_history_purge_date: date | None = None

        self._load_or_migrate_cruises()
        try:
            self.history: HistoryStore | None = HistoryStore(self.history_file)
            for cruise_id in self._cruises:
                self._record_history(cruise_id)
            self.purge_history()
        except (OSError, sqlite3.Error, ValueError) as error:
            self.history = None
            self.history_error = str(error)
            LOGGER.exception("Price history could not be initialized")

    @property
    def options(self) -> dict[str, Any]:
        runtime = self._active_runtime(required=False)
        return runtime.config if runtime is not None else self.app_options

    @property
    def catalog(self) -> dict[str, Any]:
        return self._active_runtime().catalog

    @catalog.setter
    def catalog(self, value: dict[str, Any]) -> None:
        self._active_runtime().catalog = value

    @property
    def preferences(self) -> dict[str, Any]:
        return self._active_runtime().preferences

    @preferences.setter
    def preferences(self, value: dict[str, Any]) -> None:
        self._active_runtime().preferences = value

    @property
    def catalog_file(self) -> Path:
        return self._active_runtime().catalog_file

    @property
    def preferences_file(self) -> Path:
        return self._active_runtime().preferences_file

    @property
    def refreshing(self) -> bool:
        runtime = self._active_runtime(required=False)
        return runtime.refreshing if runtime is not None else False

    @property
    def last_error(self) -> str | None:
        runtime = self._active_runtime(required=False)
        return runtime.last_error if runtime is not None else None

    @property
    def last_warning(self) -> str | None:
        runtime = self._active_runtime(required=False)
        return runtime.last_warning if runtime is not None else None

    def _active_runtime(self, *, required: bool = True) -> CruiseRuntime | None:
        runtime = self._cruises.get(self.active_cruise_id or "")
        if runtime is None and required:
            raise DashboardError("Add a cruise before using the catalog.")
        return runtime

    def _runtime(self, cruise_id: str | None = None) -> CruiseRuntime:
        if cruise_id is None:
            return self._active_runtime()
        runtime = self._cruises.get(cruise_id)
        if runtime is None:
            raise DashboardError("Unknown cruise.")
        return runtime

    def storage_status(self) -> dict[str, Any]:
        app_data_bytes: int | None = None
        history_bytes: int | None = None
        measurement_error: str | None = None
        try:
            app_data_bytes = regular_file_tree_size(self.data_root)
            history_bytes = sqlite_file_family_size(self.history_file)
        except OSError:
            measurement_error = "Some App data files could not be measured."

        filesystem_total_bytes: int | None = None
        filesystem_free_bytes: int | None = None
        free_percent: float | None = None
        try:
            disk = shutil.disk_usage(self.data_root)
            filesystem_total_bytes = int(disk.total)
            filesystem_free_bytes = int(disk.free)
            if disk.total > 0:
                free_percent = round((disk.free / disk.total) * 100, 1)
        except OSError:
            measurement_error = (
                "Available Home Assistant storage could not be measured."
            )

        level = "unknown"
        message = measurement_error
        if filesystem_free_bytes is not None:
            if filesystem_free_bytes < STORAGE_CRITICAL_FREE_BYTES:
                level = "critical"
                message = (
                    f"Only {format_storage_bytes(filesystem_free_bytes)} is free on "
                    "the Home Assistant data filesystem. New cruises and price "
                    f"refreshes are paused until at least "
                    f"{format_storage_bytes(STORAGE_CRITICAL_FREE_BYTES)} is free."
                )
            elif filesystem_free_bytes < STORAGE_WARNING_FREE_BYTES:
                level = "warning"
                message = (
                    "Home Assistant storage is running low: "
                    f"{format_storage_bytes(filesystem_free_bytes)} is free. Remove "
                    "completed cruises or free host storage before adding more."
                )
            else:
                level = "ok"

        return {
            "level": level,
            "growth_allowed": level != "critical",
            "message": message,
            "app_data_bytes": app_data_bytes,
            "history_bytes": history_bytes,
            "filesystem_total_bytes": filesystem_total_bytes,
            "filesystem_free_bytes": filesystem_free_bytes,
            "free_percent": free_percent,
            "warning_free_bytes": STORAGE_WARNING_FREE_BYTES,
            "critical_free_bytes": STORAGE_CRITICAL_FREE_BYTES,
        }

    def _require_storage_for_growth(self) -> dict[str, Any]:
        storage = self.storage_status()
        if not storage["growth_allowed"]:
            raise DashboardError(str(storage["message"]))
        return storage

    def _load_or_migrate_cruises(self) -> None:
        if self.registry_file.exists():
            try:
                with self.registry_file.open("r", encoding="utf-8") as handle:
                    registry = json.load(handle)
            except (OSError, json.JSONDecodeError) as error:
                raise DashboardError(
                    f"Could not read the cruise registry: {error}"
                ) from error
            if not isinstance(registry, dict) or registry.get("version") != 1:
                raise DashboardError("The cruise registry has an unsupported format.")
            raw_cruises = registry.get("cruises")
            if not isinstance(raw_cruises, list):
                raise DashboardError("The cruise registry is missing its cruise list.")
            for raw_config in raw_cruises:
                if not isinstance(raw_config, dict):
                    raise DashboardError("The cruise registry contains an invalid cruise.")
                config = validate_cruise_config(raw_config, defaults=self.app_options)
                if raw_config.get("id") not in (None, config["id"]):
                    raise DashboardError("A cruise registry identifier is invalid.")
                if config["id"] in self._cruises:
                    raise DashboardError("The cruise registry contains a duplicate cruise.")
                self._cruises[config["id"]] = CruiseRuntime(self.data_root, config)
            requested_active = registry.get("active_cruise_id")
            if requested_active in self._cruises:
                self.active_cruise_id = requested_active
            elif self._cruises:
                self.active_cruise_id = next(iter(self._cruises))
                self._persist_registry()
            return

        if not self.app_options.get("ship") or not self.app_options.get("sail_date"):
            return

        legacy_config = validate_cruise_config(
            {
                **self.app_options,
                "cruise_line": cruise_line_for_ship(self.app_options["ship"]),
            },
            defaults=self.app_options,
        )
        runtime = CruiseRuntime(self.data_root, legacy_config)
        legacy_catalog = read_json(self.legacy_catalog_file, {"items": []})
        if isinstance(legacy_catalog, dict):
            runtime.catalog = legacy_catalog
            runtime.catalog.setdefault("items", [])
        legacy_preferences = read_json(
            self.legacy_preferences_file,
            {"watching": {}},
        )
        if isinstance(legacy_preferences, dict):
            legacy_watching = legacy_preferences.get("watching", {})
            runtime.preferences = {
                "pinned": [],
                "watching": (
                    legacy_watching if isinstance(legacy_watching, dict) else {}
                ),
            }
        self._cruises[legacy_config["id"]] = runtime
        self.active_cruise_id = legacy_config["id"]
        write_json_atomic(runtime.catalog_file, runtime.catalog)
        write_json_atomic(runtime.preferences_file, runtime.preferences)
        self._persist_registry()
        LOGGER.info(
            "Migrated legacy single-cruise data into cruise %s; legacy files were retained",
            legacy_config["id"],
        )

    def _persist_registry(self) -> None:
        write_json_atomic(
            self.registry_file,
            {
                "version": 1,
                "active_cruise_id": self.active_cruise_id,
                "cruises": [runtime.config for runtime in self._cruises.values()],
            },
        )

    def create_cruise(
        self,
        raw: dict[str, Any],
        *,
        validate_discovery: bool = False,
    ) -> str:
        self._require_storage_for_growth()
        canonical_raw = raw
        if validate_discovery:
            requested_id = str(raw.get("ship_id") or "").strip()
            requested_name = str(raw.get("ship") or "").strip()
            ships = self._discover_all_ships()
            selected_ship = next(
                (
                    ship
                    for ship in ships
                    if requested_id and ship["id"] == requested_id
                ),
                None,
            )
            if selected_ship is None and requested_name:
                selected_ship = next(
                    (
                        ship
                        for ship in ships
                        if ship["name"].casefold() == requested_name.casefold()
                    ),
                    None,
                )
            if selected_ship is None:
                if not requested_id and not requested_name:
                    raise DashboardError("Choose a valid ship.")
                raise DashboardError("That ship is not in the current discovery list.")
            canonical_raw = {
                **raw,
                "cruise_line": selected_ship["cruise_line"],
                "ship": selected_ship["name"],
            }

        config = validate_cruise_config(
            canonical_raw,
            defaults=self.app_options,
            require_future=True,
        )
        with self.lock:
            if config["id"] in self._cruises:
                raise DashboardError("That cruise is already in the dashboard.")

        if validate_discovery:
            sailing = next(
                (
                    candidate
                    for candidate in self.discover_sailings(config["ship"])
                    if candidate["sail_date"] == config["sail_date"]
                ),
                None,
            )
            if sailing is None:
                raise DashboardError("That sailing is not in the current discovery list.")
            config = validate_cruise_config(
                {
                    **canonical_raw,
                    "duration": sailing["duration"],
                    "description": sailing["description"],
                },
                defaults=self.app_options,
                require_future=True,
            )

        with self.lock:
            if config["id"] in self._cruises:
                raise DashboardError("That cruise is already in the dashboard.")
            runtime = CruiseRuntime(self.data_root, config)
            write_json_atomic(runtime.catalog_file, runtime.catalog)
            write_json_atomic(runtime.preferences_file, runtime.preferences)
            self._cruises[config["id"]] = runtime
            self.active_cruise_id = config["id"]
            self._persist_registry()
            return config["id"]

    def set_active_cruise(self, cruise_id: str) -> None:
        with self.lock:
            self._runtime(cruise_id)
            self.active_cruise_id = cruise_id
            self._persist_registry()

    def remove_cruise(self, cruise_id: str) -> tuple[dict[str, Any], list[str]]:
        with self.lock:
            runtime = self._runtime(cruise_id)
            if runtime.refreshing:
                raise DashboardError(
                    "Wait for this cruise's price refresh to finish before removing it."
                )

            previous_active = self.active_cruise_id
            del self._cruises[cruise_id]
            if previous_active == cruise_id:
                self.active_cruise_id = next(iter(self._cruises), None)
            try:
                self._persist_registry()
            except Exception:
                self._cruises[cruise_id] = runtime
                self.active_cruise_id = previous_active
                raise

            removed = copy.deepcopy(runtime.config)
            warnings: list[str] = []
            cruises_root = self.data_root / "cruises"
            if runtime.directory.parent != cruises_root:
                warnings.append("The removed cruise's files were not deleted safely.")
                LOGGER.error(
                    "Refused to delete unexpected cruise directory %s",
                    runtime.directory,
                )
            else:
                try:
                    shutil.rmtree(runtime.directory)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    warnings.append(
                        "The cruise was removed, but some saved files could not be deleted."
                    )
                    LOGGER.exception(
                        "Could not delete saved files for removed cruise %s: %s",
                        cruise_id,
                        error,
                    )

            if self.history is not None:
                try:
                    self.history.delete_sailing(runtime.config)
                    self.history_error = None
                except (OSError, sqlite3.Error, ValueError) as error:
                    self.history_error = str(error)
                    warnings.append(
                        "The cruise was removed, but its saved price history could not be deleted."
                    )
                    LOGGER.exception(
                        "Could not delete price history for removed cruise %s",
                        cruise_id,
                    )
            return removed, warnings

    def _discover_all_ships(self) -> list[dict[str, str]]:
        with self.discovery_lock:
            now = time.monotonic()
            if (
                self._ship_cache is None
                or now - self._ship_cache[0] >= DISCOVERY_CACHE_SECONDS
            ):
                self._ship_cache = (now, parse_ship_menu(_run_upstream_menu([])))
            return copy.deepcopy(self._ship_cache[1])

    def discover_ships(self, cruise_line: str) -> list[dict[str, str]]:
        requested_line = cruise_line.strip().casefold()
        if requested_line not in {"royal-caribbean", "celebrity"}:
            raise DashboardError("Choose Royal Caribbean or Celebrity.")
        return [
            ship
            for ship in self._discover_all_ships()
            if ship["cruise_line"] == requested_line
        ]

    def discover_sailings(self, ship: str) -> list[dict[str, Any]]:
        selected_ship = str(ship).strip()
        if (
            not selected_ship
            or len(selected_ship) > 120
            or any(ord(character) < 32 for character in selected_ship)
        ):
            raise DashboardError("Choose a valid ship.")
        cruise_line_for_ship(selected_ship)
        cache_key = selected_ship.casefold()
        with self.discovery_lock:
            now = time.monotonic()
            cached = self._sailing_cache.get(cache_key)
            if cached is None or now - cached[0] >= DISCOVERY_CACHE_SECONDS:
                output = _run_upstream_menu(
                    ["-s", browser_ship_argument(selected_ship)]
                )
                cached = (now, parse_sailing_menu(output))
                self._sailing_cache[cache_key] = cached
            today = datetime.now(timezone.utc).date().isoformat()
            sailings = [
                copy.deepcopy(sailing)
                for sailing in cached[1]
                if sailing["sail_date"] >= today
            ]
            if not sailings:
                raise DashboardError("No future sailings were returned for that ship.")
            return sailings

    def _item_index(self, cruise_id: str | None = None) -> dict[str, dict[str, Any]]:
        runtime = self._runtime(cruise_id)
        return {item["id"]: item for item in runtime.catalog.get("items", [])}

    def state(self) -> dict[str, Any]:
        with self.lock:
            now = datetime.now(timezone.utc)
            active = self._active_runtime(required=False)
            cruises = []
            for runtime in self._cruises.values():
                cruise_config = copy.deepcopy(runtime.config)
                cruise_config["duration"] = cruise_duration(
                    runtime.config,
                    runtime.catalog,
                )
                refresh_mode, refresh_interval = self._refresh_schedule(runtime)
                cruise_config["refresh_mode"] = refresh_mode
                cruise_config["refresh_interval_hours"] = refresh_interval
                completion = cruise_completion(cruise_config)
                cruises.append(
                    {
                        **cruise_config,
                        **completion,
                        "generated_at": runtime.catalog.get("generated_at"),
                        "catalog_count": len(runtime.catalog.get("items", [])),
                        "watch_count": len(
                            runtime.preferences.get("watching", {})
                        ),
                        "pinned_count": len(runtime.preferences.get("pinned", [])),
                        "refreshing": runtime.refreshing,
                        "refresh_cooldown_seconds": (
                            self._refresh_cooldown_seconds(runtime, now)
                        ),
                        "last_error": runtime.last_error,
                        "last_warning": runtime.last_warning,
                    }
                )
            active_completion = (
                cruise_completion(active.config, active.catalog)
                if active is not None
                else {"return_date": None, "completed": False}
            )
            active_config = copy.deepcopy(
                active.config if active is not None else self.app_options
            )
            if active is not None:
                active_config["duration"] = cruise_duration(
                    active.config,
                    active.catalog,
                )
                active_refresh_mode, active_refresh_interval = (
                    self._refresh_schedule(active)
                )
                active_config["refresh_mode"] = active_refresh_mode
                active_config["refresh_interval_hours"] = active_refresh_interval
            else:
                active_refresh_mode = None
                active_refresh_interval = None
            status = {
                **active_completion,
                "refreshing": active.refreshing if active is not None else False,
                "refreshing_count": sum(
                    1 for runtime in self._cruises.values() if runtime.refreshing
                ),
                "refresh_cooldown_seconds": (
                    self._refresh_cooldown_seconds(active, now)
                    if active is not None
                    else 0
                ),
                "refresh_available_at": (
                    active.next_refresh_allowed_at.isoformat()
                    if active is not None
                    and self._refresh_cooldown_seconds(active, now) > 0
                    else None
                ),
                "refresh_mode": active_refresh_mode,
                "refresh_interval_hours": active_refresh_interval,
                "last_error": active.last_error if active is not None else None,
                "last_warning": active.last_warning if active is not None else None,
                "history": {
                    "enabled": self.history is not None,
                    "mode": "changes_only",
                    "retention_days_after_sailing": (
                        HISTORY_RETENTION_DAYS_AFTER_SAILING
                    ),
                    "last_error": self.history_error,
                },
                "storage": self.storage_status(),
            }
            return {
                "setup_required": active is None,
                "active_cruise_id": self.active_cruise_id,
                "cruises": cruises,
                "config": active_config,
                "catalog": copy.deepcopy(
                    active.catalog if active is not None else {"items": []}
                ),
                "preferences": copy.deepcopy(
                    active.preferences
                    if active is not None
                    else {"pinned": [], "watching": {}}
                ),
                "status": status,
            }

    def _record_history(
        self,
        cruise_id: str,
        catalog: dict[str, Any] | None = None,
    ) -> int:
        if self.history is None:
            return 0
        runtime = self._runtime(cruise_id)
        target_catalog = catalog if catalog is not None else runtime.catalog
        try:
            inserted = self.history.record_catalog(target_catalog, runtime.config)
            self.history_error = None
            if inserted:
                LOGGER.info(
                    "Recorded %d price history changes for %s",
                    inserted,
                    cruise_id,
                )
            return inserted
        except (OSError, sqlite3.Error, ValueError) as error:
            self.history_error = str(error)
            LOGGER.exception("Price history update failed for %s", cruise_id)
            return 0

    def history_for(
        self,
        item_id: str,
        cruise_id: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            runtime = self._runtime(cruise_id)
            item = self._item_index(runtime.config["id"]).get(item_id)
            if item is None:
                raise DashboardError("Unknown catalog item.")
            item_copy = copy.deepcopy(item)
            catalog_copy = copy.deepcopy(runtime.catalog)
            options_copy = copy.deepcopy(runtime.config)
            history = self.history
        if history is None:
            raise DashboardError(
                f"Price history is unavailable: {self.history_error or 'unknown error'}"
            )
        try:
            return history.get_history(
                item=item_copy,
                catalog=catalog_copy,
                options=options_copy,
            )
        except (OSError, sqlite3.Error, ValueError) as error:
            self.history_error = str(error)
            raise DashboardError(f"Could not read price history: {error}") from error

    def changes_for(
        self,
        cruise_id: str | None = None,
        *,
        scope: str = "watched",
        since: str | None = None,
        limit: int = 100,
        latest_only: bool = False,
    ) -> dict[str, Any]:
        if scope not in {"watched", "all"}:
            raise DashboardError("Changes scope must be watched or all.")
        if not 1 <= limit <= 500:
            raise DashboardError("Changes limit must be between 1 and 500.")

        since_at: datetime | None = None
        if since:
            try:
                since_at = datetime.fromisoformat(since)
            except (TypeError, ValueError) as error:
                raise DashboardError("Changes since must be an ISO timestamp.") from error
            if since_at.tzinfo is None:
                raise DashboardError("Changes since must include a timezone.")
            since_at = since_at.astimezone(timezone.utc)

        with self.lock:
            runtime = self._runtime(cruise_id)
            catalog_copy = copy.deepcopy(runtime.catalog)
            options_copy = copy.deepcopy(runtime.config)
            watched_ids = set(runtime.preferences.get("watching", {}))
            history = self.history
        if history is None:
            raise DashboardError(
                f"Price history is unavailable: {self.history_error or 'unknown error'}"
            )

        try:
            history_changes = history.get_changes(
                catalog=catalog_copy,
                options=options_copy,
            )
        except (OSError, sqlite3.Error, ValueError) as error:
            self.history_error = str(error)
            raise DashboardError(f"Could not read price changes: {error}") from error

        item_index = {
            str(item["id"]): item
            for item in catalog_copy.get("items", [])
            if isinstance(item, dict) and item.get("id") is not None
        }
        allowed_ids = watched_ids if scope == "watched" else set(item_index)

        def is_after_since(event: dict[str, Any]) -> bool:
            if since_at is None:
                return True
            try:
                observed_at = datetime.fromisoformat(str(event["observed_at"]))
            except (TypeError, ValueError):
                return False
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            return observed_at.astimezone(timezone.utc) > since_at

        def decorate(event: dict[str, Any]) -> dict[str, Any] | None:
            item = item_index.get(str(event["product_id"]))
            if item is None:
                return None
            return {
                **copy.deepcopy(event),
                "item": {
                    "id": item["id"],
                    "name": item["name"],
                    "category": item.get("category"),
                    "subcategory": item.get("subcategory"),
                    "currency": item.get("currency", options_copy["currency"]),
                    "unit": item.get("unit"),
                },
            }

        matching = [
            event
            for event in history_changes["changes"]
            if event["product_id"] in allowed_ids
            and event["product_id"] in item_index
            and is_after_since(event)
        ]
        if latest_only:
            seen_product_ids: set[str] = set()
            latest_matching: list[dict[str, Any]] = []
            for event in matching:
                product_id = str(event["product_id"])
                if product_id in seen_product_ids:
                    continue
                seen_product_ids.add(product_id)
                latest_matching.append(event)
            matching = latest_matching
        changes = [
            decorated
            for event in matching[:limit]
            if (decorated := decorate(event)) is not None
        ]
        watched_latest = {
            product_id: decorated
            for product_id, event in history_changes["latest_price_changes"].items()
            if product_id in watched_ids
            and (decorated := decorate(event)) is not None
        }

        def watched_price_stat(
            product_id: str,
            stats: dict[str, Any],
        ) -> dict[str, Any] | None:
            item = item_index.get(product_id)
            if item is None:
                return None
            raw_price = item.get("price")
            if raw_price is None or not bool(item.get("price_available", True)):
                return None
            current_price = round(float(raw_price), 2)
            average_price = float(stats["average_price"])
            lowest_price = float(stats["lowest_price"])
            highest_price = float(stats["highest_price"])
            recorded_price_count = int(stats["recorded_price_count"])
            enough_history = recorded_price_count >= 2
            return {
                **copy.deepcopy(stats),
                "current_price": current_price,
                "below_average": (
                    enough_history and current_price < average_price - 0.005
                ),
                "record_low": (
                    enough_history
                    and current_price <= lowest_price + 0.005
                    and highest_price > current_price + 0.005
                ),
            }

        watched_price_stats = {
            product_id: result
            for product_id, stats in history_changes["price_stats"].items()
            if product_id in watched_ids
            and (result := watched_price_stat(product_id, stats)) is not None
        }
        sailing = catalog_copy.get("sailing", {})
        return {
            "sailing": {
                "ship": sailing.get("ship", options_copy["ship"]),
                "sail_date": sailing.get("sail_date", options_copy["sail_date"]),
                "currency": sailing.get("currency", options_copy["currency"]),
            },
            "scope": scope,
            "since": since_at.isoformat() if since_at is not None else None,
            "limit": limit,
            "latest_only": latest_only,
            "total": len(matching),
            "truncated": len(matching) > limit,
            "changes": changes,
            "watched_latest": watched_latest,
            "watched_price_stats": watched_price_stats,
        }

    def purge_history(self) -> None:
        current_date = datetime.now(timezone.utc).date()
        if self.last_history_purge_date == current_date:
            return
        self.last_history_purge_date = current_date
        if self.history is None:
            return
        try:
            deleted = self.history.purge_expired(current_date)
            self.history_error = None
            if deleted:
                LOGGER.info("Purged %d expired price history events", deleted)
        except (OSError, sqlite3.Error) as error:
            self.history_error = str(error)
            LOGGER.exception("Price history retention cleanup failed")

    def set_pinned(
        self,
        item_id: str,
        pinned: bool,
        cruise_id: str | None = None,
    ) -> None:
        with self.lock:
            runtime = self._runtime(cruise_id)
            if item_id not in self._item_index(runtime.config["id"]):
                raise DashboardError("Unknown catalog item.")
            pinned_items = set(runtime.preferences.get("pinned", []))
            if pinned:
                pinned_items.add(item_id)
            else:
                pinned_items.discard(item_id)
            runtime.preferences["pinned"] = sorted(pinned_items)
            write_json_atomic(runtime.preferences_file, runtime.preferences)

    def set_watching(
        self,
        item_id: str,
        watching: bool,
        target_price: float | None = None,
        cruise_id: str | None = None,
    ) -> None:
        with self.lock:
            runtime = self._runtime(cruise_id)
            item = self._item_index(runtime.config["id"]).get(item_id)
            if item is None:
                raise DashboardError("Unknown catalog item.")
            watches = runtime.preferences.setdefault("watching", {})
            if not watching:
                watches.pop(item_id, None)
            else:
                if target_price is None:
                    target_price = item.get("price")
                if target_price is None or target_price < 0:
                    raise DashboardError("This item does not have a usable target price.")
                prior = watches.get(item_id, {})
                watches[item_id] = {
                    "target_price": round(float(target_price), 2),
                    "added_at": prior.get("added_at", utc_now()),
                    "last_alerted_price": prior.get("last_alerted_price"),
                    "last_alerted_at": prior.get("last_alerted_at"),
                }
            write_json_atomic(runtime.preferences_file, runtime.preferences)

    def set_target(
        self,
        item_id: str,
        target_price: float,
        cruise_id: str | None = None,
    ) -> None:
        with self.lock:
            runtime = self._runtime(cruise_id)
            watch = runtime.preferences.setdefault("watching", {}).get(item_id)
            if watch is None:
                raise DashboardError("Watch the item before setting a target.")
            if target_price < 0:
                raise DashboardError("Target price cannot be negative.")
            watch["target_price"] = round(float(target_price), 2)
            watch["last_alerted_price"] = None
            watch["last_alerted_at"] = None
            write_json_atomic(runtime.preferences_file, runtime.preferences)

    def export_watchlist(self, cruise_id: str | None = None) -> str:
        with self.lock:
            runtime = self._runtime(cruise_id)
            items = self._item_index(runtime.config["id"])
            watches = runtime.preferences.get("watching", {})
            lines = ["watchList:"]
            for item_id in sorted(
                watches,
                key=lambda key: items.get(key, {}).get("name", key).casefold(),
            ):
                item = items.get(item_id)
                if item is None:
                    continue
                watch = watches[item_id]
                lines.extend(
                    [
                        f"  - name: {json.dumps(item['name'], ensure_ascii=False)}",
                        f"    prefix: {json.dumps(item['prefix'])}",
                        f"    product: {json.dumps(item['product'])}",
                        f"    price: {float(watch['target_price']):.2f}",
                        "    enabled: true",
                        f"    currency: {json.dumps(item['currency'])}",
                    ]
                )
            if len(lines) == 1:
                lines.append("  []")
            return "\n".join(lines) + "\n"

    def _refresh_schedule(self, runtime: CruiseRuntime) -> tuple[str, int]:
        watches = runtime.preferences.get("watching", {})
        watched = isinstance(watches, dict) and bool(watches)
        mode = "watched" if watched else "unwatched"
        option_name = f"{mode}_refresh_interval_hours"
        return mode, int(self.app_options[option_name])

    def is_refresh_due(self, cruise_id: str | None = None) -> bool:
        with self.lock:
            runtime = self._runtime(cruise_id)
            if cruise_completion(runtime.config, runtime.catalog)["completed"]:
                return False
            if self._refresh_cooldown_seconds(runtime) > 0:
                return False
            generated = runtime.catalog.get("generated_at")
            if not generated:
                return True
            try:
                generated_at = datetime.fromisoformat(generated)
            except (TypeError, ValueError):
                return True
            age_seconds = (datetime.now(timezone.utc) - generated_at).total_seconds()
            _mode, interval_hours = self._refresh_schedule(runtime)
            return age_seconds >= interval_hours * 3600

    def due_cruise_ids(self) -> list[str]:
        with self.lock:
            cruise_ids = list(self._cruises)
        return [cruise_id for cruise_id in cruise_ids if self.is_refresh_due(cruise_id)]

    @staticmethod
    def _refresh_cooldown_seconds(
        runtime: CruiseRuntime,
        now: datetime | None = None,
    ) -> int:
        allowed_at = runtime.next_refresh_allowed_at
        if allowed_at is None:
            return 0
        current = now or datetime.now(timezone.utc)
        return max(0, int((allowed_at - current).total_seconds() + 0.999))

    def start_refresh(
        self,
        cruise_id: str | None = None,
        *,
        manual: bool = False,
    ) -> bool:
        with self.lock:
            runtime = self._runtime(cruise_id)
            resolved_id = runtime.config["id"]
            completion = cruise_completion(runtime.config, runtime.catalog)
            if completion["completed"]:
                raise DashboardError(
                    f"This cruise returned on {completion['return_date']} and is no "
                    "longer refreshed. You can keep it for reference or remove it."
                )
            if runtime.refreshing:
                return False
            storage = self.storage_status()
            if not storage["growth_allowed"]:
                if manual:
                    raise DashboardError(str(storage["message"]))
                return False
            cooldown_seconds = self._refresh_cooldown_seconds(runtime)
            if cooldown_seconds > 0:
                if manual:
                    wait_minutes = max(1, (cooldown_seconds + 59) // 60)
                    raise DashboardError(
                        "Please wait "
                        f"{wait_minutes} minute{'s' if wait_minutes != 1 else ''} "
                        "before refreshing this cruise again."
                    )
                return False
            started_at = datetime.now(timezone.utc)
            runtime.refreshing = True
            runtime.last_error = None
            runtime.last_warning = None
            runtime.last_refresh_started_at = started_at
            runtime.next_refresh_allowed_at = started_at + timedelta(
                seconds=MANUAL_REFRESH_COOLDOWN_SECONDS
            )
        thread = threading.Thread(
            target=self._refresh_worker,
            args=(resolved_id,),
            name=f"royal-price-refresh-{resolved_id}",
            daemon=True,
        )
        thread.start()
        return True

    def _refresh_worker(self, cruise_id: str) -> None:
        runtime = self._runtime(cruise_id)
        try:
            with self.refresh_gate:
                catalog, warning = run_browser(copy.deepcopy(runtime.config))
            self._require_storage_for_growth()
            with self.lock:
                runtime = self._runtime(cruise_id)
                runtime.catalog = catalog
                runtime.last_warning = warning
                runtime.consecutive_refresh_failures = 0
                write_json_atomic(runtime.catalog_file, runtime.catalog)
                candidates = self._alert_candidates_locked(cruise_id)
            self._record_history(cruise_id, catalog)

            for candidate in candidates:
                if self._send_notification(cruise_id, candidate):
                    with self.lock:
                        runtime = self._runtime(cruise_id)
                        watch = runtime.preferences.get("watching", {}).get(
                            candidate["id"]
                        )
                        if watch is not None:
                            watch["last_alerted_price"] = candidate["price"]
                            watch["last_alerted_at"] = utc_now()
                            write_json_atomic(
                                runtime.preferences_file,
                                runtime.preferences,
                            )
            LOGGER.info(
                "Catalog refresh completed for %s with %d products",
                cruise_id,
                len(catalog["items"]),
            )
        except Exception as error:
            LOGGER.exception("Catalog refresh failed for %s", cruise_id)
            with self.lock:
                runtime = self._runtime(cruise_id)
                runtime.consecutive_refresh_failures += 1
                exponent = min(runtime.consecutive_refresh_failures - 1, 16)
                backoff_seconds = min(
                    REFRESH_FAILURE_BACKOFF_BASE_SECONDS * (2**exponent),
                    REFRESH_FAILURE_BACKOFF_MAX_SECONDS,
                )
                retry_at = datetime.now(timezone.utc) + timedelta(
                    seconds=backoff_seconds
                )
                if (
                    runtime.next_refresh_allowed_at is None
                    or retry_at > runtime.next_refresh_allowed_at
                ):
                    runtime.next_refresh_allowed_at = retry_at
                runtime.last_error = str(error)
        finally:
            with self.lock:
                runtime.refreshing = False

    def _alert_candidates_locked(
        self,
        cruise_id: str | None = None,
    ) -> list[dict[str, Any]]:
        runtime = self._runtime(cruise_id)
        items = self._item_index(runtime.config["id"])
        candidates: list[dict[str, Any]] = []
        changed_preferences = False
        for item_id, watch in runtime.preferences.get("watching", {}).items():
            item = items.get(item_id)
            if item is None or item.get("price") is None:
                continue
            price = float(item["price"])
            target = float(watch["target_price"])
            last_alerted = watch.get("last_alerted_price")
            if price < target:
                if last_alerted is None or price < float(last_alerted):
                    candidates.append({**item, "target_price": target})
            elif last_alerted is not None:
                watch["last_alerted_price"] = None
                watch["last_alerted_at"] = None
                changed_preferences = True
        if changed_preferences:
            write_json_atomic(runtime.preferences_file, runtime.preferences)
        return candidates

    def _send_notification(self, cruise_id: str, item: dict[str, Any]) -> bool:
        runtime = self._runtime(cruise_id)
        if not runtime.config["notifications_enabled"]:
            return False
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            LOGGER.warning("SUPERVISOR_TOKEN is unavailable; notification skipped")
            return False

        unit = f" per {item['unit']}" if item.get("unit") else ""
        line_name = (
            "Celebrity" if runtime.config["cruise_line"] == "celebrity"
            else "Royal Caribbean"
        )
        payload = {
            "title": f"{line_name} price drop",
            "message": (
                f"{item['name']} is now {item['price']:.2f} {item['currency']}{unit}, "
                f"below your {item['target_price']:.2f} target for "
                f"{runtime.config['ship']} on {runtime.config['sail_date']}."
            ),
            "notification_id": f"royal_price_{cruise_id}_{item['product']}",
        }
        request = urllib.request.Request(
            "http://supervisor/core/api/services/persistent_notification/create",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError) as error:
            LOGGER.warning("Could not create Home Assistant notification: %s", error)
            return False


class DashboardHandler(BaseHTTPRequestHandler):
    manager: CatalogManager
    server_version = f"RoyalPriceDashboard/{APP_VERSION}"

    def log_message(self, message: str, *args: Any) -> None:
        LOGGER.info("HTTP %s - %s", self.address_string(), message % args)

    def _reject_untrusted_client(self) -> bool:
        client = self.client_address[0]
        if not ALLOWED_CLIENTS or client in ALLOWED_CLIENTS:
            return False
        LOGGER.warning("Rejected HTTP request from non-Ingress client %s", client)
        self._send_json({"error": "Forbidden"}, HTTPStatus.FORBIDDEN)
        return True

    def _route_path(self) -> str:
        path = urllib.parse.urlsplit(self.path).path
        for marker in ("/api/", "/health"):
            index = path.find(marker)
            if index >= 0:
                return path[index:]
        for asset in ("/app.js", "/styles.css", "/index.html"):
            if path.endswith(asset):
                return asset
        return "/"

    def _send_bytes(
        self,
        payload: bytes,
        *,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(
        self,
        payload: Any,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self._send_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            content_type="application/json; charset=utf-8",
            status=status,
        )

    def _read_chunked_body(self) -> bytes:
        payload = bytearray()
        while True:
            size_line = self.rfile.readline(MAX_CHUNK_LINE_BYTES + 1)
            if (
                not size_line
                or len(size_line) > MAX_CHUNK_LINE_BYTES
                or not size_line.endswith(b"\r\n")
            ):
                raise DashboardError("Request body uses invalid chunk framing.")
            size_token = size_line[:-2].split(b";", 1)[0].strip()
            if len(size_token) > 16 or not re.fullmatch(
                rb"[0-9a-fA-F]+",
                size_token,
            ):
                raise DashboardError("Request body uses an invalid chunk size.")
            chunk_size = int(size_token, 16)
            if chunk_size == 0:
                trailer_bytes = 0
                while True:
                    trailer_line = self.rfile.readline(MAX_CHUNK_LINE_BYTES + 1)
                    trailer_bytes += len(trailer_line)
                    if (
                        not trailer_line
                        or len(trailer_line) > MAX_CHUNK_LINE_BYTES
                        or trailer_bytes > MAX_TRAILER_BYTES
                        or not trailer_line.endswith(b"\r\n")
                    ):
                        raise DashboardError("Request trailers are invalid or too large.")
                    if trailer_line == b"\r\n":
                        return bytes(payload)

            if len(payload) + chunk_size > MAX_REQUEST_BODY_BYTES:
                raise DashboardError("Request body is too large.")
            chunk = self.rfile.read(chunk_size)
            if len(chunk) != chunk_size or self.rfile.read(2) != b"\r\n":
                raise DashboardError("Request body uses invalid chunk framing.")
            payload.extend(chunk)

    def _read_request_body(self) -> bytes:
        transfer_encoding = self.headers.get("Transfer-Encoding")
        if transfer_encoding:
            encodings = [
                value.strip().casefold()
                for value in transfer_encoding.split(",")
                if value.strip()
            ]
            if encodings != ["chunked"]:
                raise DashboardError("Request transfer encoding is unsupported.")
            return self._read_chunked_body()

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        if not re.fullmatch(r"[0-9]+", raw_length):
            raise DashboardError("Request Content-Length is invalid.")
        length = int(raw_length)
        if length > MAX_REQUEST_BODY_BYTES:
            raise DashboardError("Request body is too large.")
        payload = self.rfile.read(length)
        if len(payload) != length:
            raise DashboardError("Request body ended unexpectedly.")
        return payload

    def _read_json(self) -> dict[str, Any]:
        raw_payload = self._read_request_body()
        if not raw_payload:
            return {}
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DashboardError("Request body must be valid JSON.") from error
        if not isinstance(payload, dict):
            raise DashboardError("Request body must be a JSON object.")
        return payload

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self._reject_untrusted_client():
            return
        route = self._route_path()
        if route == "/health":
            self._send_json({"status": "ok", "version": APP_VERSION})
            return
        if route == "/api/state":
            self._send_json(self.manager.state())
            return
        if route == "/api/discovery/ships":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                cruise_line = query.get("line", [""])[0]
                self._send_json({"ships": self.manager.discover_ships(cruise_line)})
            except DashboardError as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if route == "/api/discovery/sailings":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                ship = query.get("ship", [""])[0]
                self._send_json(
                    {
                        "ship": ship,
                        "sailings": self.manager.discover_sailings(ship),
                    }
                )
            except DashboardError as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        scoped_changes_match = re.fullmatch(
            r"/api/cruises/([^/]+)/changes",
            route,
        )
        if scoped_changes_match:
            cruise_id = urllib.parse.unquote(scoped_changes_match.group(1))
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                scope = query.get("scope", ["watched"])[0]
                since = query.get("since", [None])[0]
                raw_limit = query.get("limit", ["100"])[0]
                raw_latest_only = query.get("latest_only", ["false"])[0].lower()
                if raw_latest_only not in {"true", "false"}:
                    raise DashboardError(
                        "Changes latest_only must be true or false."
                    )
                try:
                    limit = int(raw_limit)
                except (TypeError, ValueError) as error:
                    raise DashboardError(
                        "Changes limit must be a whole number."
                    ) from error
                self._send_json(
                    self.manager.changes_for(
                        cruise_id,
                        scope=scope,
                        since=since,
                        limit=limit,
                        latest_only=raw_latest_only == "true",
                    )
                )
            except DashboardError as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        scoped_history_match = re.fullmatch(
            r"/api/cruises/([^/]+)/items/([^/]+)/history",
            route,
        )
        if scoped_history_match:
            cruise_id = urllib.parse.unquote(scoped_history_match.group(1))
            item_id = urllib.parse.unquote(scoped_history_match.group(2))
            try:
                self._send_json(self.manager.history_for(item_id, cruise_id))
            except DashboardError as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        history_match = re.fullmatch(
            r"/api/items/([^/]+)/history",
            route,
        )
        if history_match:
            item_id = urllib.parse.unquote(history_match.group(1))
            try:
                self._send_json(self.manager.history_for(item_id))
            except DashboardError as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        scoped_export_match = re.fullmatch(r"/api/cruises/([^/]+)/export", route)
        if scoped_export_match:
            cruise_id = urllib.parse.unquote(scoped_export_match.group(1))
            try:
                payload = self.manager.export_watchlist(cruise_id).encode("utf-8")
                self._send_bytes(
                    payload,
                    content_type="text/yaml; charset=utf-8",
                )
            except DashboardError as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if route == "/api/export":
            try:
                self._send_bytes(
                    self.manager.export_watchlist().encode("utf-8"),
                    content_type="text/yaml; charset=utf-8",
                )
            except DashboardError as error:
                self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return

        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }
        asset = assets.get(route)
        if asset is None:
            self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        filename, content_type = asset
        try:
            payload = (STATIC_ROOT / filename).read_bytes()
        except OSError:
            self._send_json({"error": "Asset unavailable"}, HTTPStatus.NOT_FOUND)
            return
        self._send_bytes(payload, content_type=content_type)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self._reject_untrusted_client():
            return
        try:
            route = self._route_path()
            body = self._read_json()
            if route == "/api/cruises":
                try:
                    cruise_id = self.manager.create_cruise(
                        body,
                        validate_discovery=True,
                    )
                except (DashboardError, TypeError, ValueError) as error:
                    LOGGER.warning(
                        "Cruise creation rejected: %s; request=%s",
                        error,
                        json.dumps(cruise_request_log_context(body), sort_keys=True),
                    )
                    raise
                started = self.manager.start_refresh(cruise_id)
                self._send_json(
                    {
                        "created": cruise_id,
                        "refresh_started": started,
                        "state": self.manager.state(),
                    },
                    HTTPStatus.CREATED,
                )
                return

            activate_match = re.fullmatch(
                r"/api/cruises/([^/]+)/activate",
                route,
            )
            if activate_match:
                cruise_id = urllib.parse.unquote(activate_match.group(1))
                self.manager.set_active_cruise(cruise_id)
                self._send_json({"ok": True, "state": self.manager.state()})
                return

            scoped_refresh_match = re.fullmatch(
                r"/api/cruises/([^/]+)/refresh",
                route,
            )
            if scoped_refresh_match:
                cruise_id = urllib.parse.unquote(scoped_refresh_match.group(1))
                started = self.manager.start_refresh(cruise_id, manual=True)
                self._send_json(
                    {"started": started, "refreshing": True},
                    HTTPStatus.ACCEPTED,
                )
                return

            if route == "/api/refresh":
                started = self.manager.start_refresh(manual=True)
                self._send_json(
                    {"started": started, "refreshing": True},
                    HTTPStatus.ACCEPTED,
                )
                return

            scoped_match = re.fullmatch(
                r"/api/cruises/([^/]+)/items/([^/]+)/(pin|watch|target)",
                route,
            )
            match = scoped_match or re.fullmatch(
                r"/api/items/([^/]+)/(pin|watch|target)",
                route,
            )
            if not match:
                self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
                return

            if scoped_match:
                cruise_id = urllib.parse.unquote(match.group(1))
                item_id = urllib.parse.unquote(match.group(2))
                action = match.group(3)
            else:
                cruise_id = None
                item_id = urllib.parse.unquote(match.group(1))
                action = match.group(2)
            if action == "pin":
                self.manager.set_pinned(
                    item_id,
                    bool(body.get("pinned", True)),
                    cruise_id,
                )
            elif action == "watch":
                raw_target = body.get("target_price")
                target = float(raw_target) if raw_target is not None else None
                self.manager.set_watching(
                    item_id,
                    bool(body.get("watching", True)),
                    target,
                    cruise_id,
                )
            elif action == "target":
                if "target_price" not in body:
                    raise DashboardError("target_price is required.")
                self.manager.set_target(
                    item_id,
                    float(body["target_price"]),
                    cruise_id,
                )
            self._send_json({"ok": True, "state": self.manager.state()})
        except (DashboardError, TypeError, ValueError) as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            LOGGER.exception("Unhandled request error")
            self._send_json(
                {"error": f"Internal error: {error}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self._reject_untrusted_client():
            return
        try:
            route = self._route_path()
            match = re.fullmatch(r"/api/cruises/([^/]+)", route)
            if not match:
                self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
                return
            cruise_id = urllib.parse.unquote(match.group(1))
            removed, warnings = self.manager.remove_cruise(cruise_id)
            self._send_json(
                {
                    "removed": removed,
                    "warnings": warnings,
                    "state": self.manager.state(),
                }
            )
        except DashboardError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            LOGGER.exception("Unhandled cruise removal error")
            self._send_json(
                {"error": f"Internal error: {error}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def scheduler_loop(manager: CatalogManager, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        manager.purge_history()
        for cruise_id in manager.due_cruise_ids():
            try:
                manager.start_refresh(cruise_id)
            except DashboardError as error:
                LOGGER.info(
                    "Skipped scheduled refresh for cruise %s: %s",
                    cruise_id,
                    error,
                )
        stop_event.wait(60)


def main() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    manager = CatalogManager(DATA_ROOT, OPTIONS_FILE)
    DashboardHandler.manager = manager
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), DashboardHandler)
    stop_event = threading.Event()

    scheduler = threading.Thread(
        target=scheduler_loop,
        args=(manager, stop_event),
        name="royal-price-scheduler",
        daemon=True,
    )
    scheduler.start()

    def stop_server(signum: int, _frame: Any) -> None:
        LOGGER.info("Received signal %s; stopping", signum)
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    LOGGER.info("Royal Price Dashboard listening on port %d", LISTEN_PORT)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
