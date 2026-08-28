import copy
import http.client
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import server
import upstream_adapter


SAMPLE_OUTPUT = """
Browsing for Wonder of the Seas sailing on 06/20/31 (4 Night Bahamas & Perfect Day Cruise)
Day 1 (06/20/31): Miami, Florida ↑ 1630
Day 2 (06/21/31): Cruising
Gathering list of products.  This may take a few minutes; please be patient.
\x1b[94mBeverage Packages\x1b[0m
\tDeluxe Beverage Package \x1b[1;32m95.99 USD\x1b[0m per day (prefix: beverage, product: 3222)
\tEvian Water Delivery (12 or 24 Bottles) (larger option) Price Not Available (prefix: beverage, product: 0904)
__ROYAL_PRICE_DASHBOARD_DESCRIPTION__ {"id":"3222","description":"<p>Drinks throughout the cruise, subject to the package terms.</p>"}
__ROYAL_PRICE_DASHBOARD_DESCRIPTION__ {"id":"0904","description":"<p>Drinks throughout the cruise, subject to the package terms.</p>"}
\x1b[94mShore Excursions\x1b[0m
\t\x1b[94mDay 4: Perfect Day Cococay, Bahamas\x1b[0m
\tThrill Waterpark - Full Day Pass  \x1b[1;32m61.99 USD\x1b[0m (prefix: shorex, product: ZH01)
__ROYAL_PRICE_DASHBOARD_DESCRIPTION__ {"id":"ZH01","description":"<p>A fast ride\\u2014bring a towel &amp; sunscreen.</p><script>ignore me</script>"}
\x1b[94mCelebrations\x1b[0m
\tDeluxe Beverage Package \x1b[1;32m95.99 USD\x1b[0m per day (prefix: celebrations, product: 3222)
"""

SHIP_MENU_OUTPUT = """
Select Ship:
\x1b[94m0\x1b[0m) \x1b[1;32mWonder of the Seas\x1b[0m
\x1b[94m1\x1b[0m) \x1b[1;32mCelebrity Beyond\x1b[0m
\x1b[94mq\x1b[0m) - Quit
"""

SAILING_MENU_OUTPUT = """
Getting sailings for Wonder of the Seas

Select sailing:
\x1b[94m0\x1b[0m) \x1b[1;32m06/20/31\x1b[0m 4 Night Bahamas & Perfect Day Cruise
\x1b[94m1\x1b[0m) \x1b[1;32m02/21/2027\x1b[0m 7 Nt Eastern Caribbean Cruise
\x1b[94mq\x1b[0m) - Quit
"""


class ParserTests(unittest.TestCase):
    def test_release_versions_are_consistent(self):
        repository_root = Path(__file__).resolve().parents[1]
        version = server.APP_VERSION
        dockerfile = (repository_root / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn(
            f'version: "{version}"',
            (repository_root / "config.yaml").read_text(encoding="utf-8"),
        )
        self.assertIn(
            f"ARG BUILD_VERSION={version}",
            dockerfile,
        )
        self.assertIn(
            "COPY upstream_adapter.py /opt/upstream/RoyalPriceDashboardBrowse.py",
            dockerfile,
        )
        self.assertIn(
            "ba57bff356d7739158af83a991f2a79de2be583572def0039e73a103244cfa01",
            dockerfile,
        )
        self.assertIn(
            f'const APP_VERSION = "{version}";',
            (repository_root / "static" / "app.js").read_text(encoding="utf-8"),
        )
        index = (repository_root / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"styles.css?v={version}", index)
        self.assertIn(f"app.js?v={version}", index)

    def test_iso_date_is_translated_only_at_browser_boundary(self):
        self.assertEqual(server.browser_date_argument("2031-06-20"), "06/20/31")

    def test_ship_name_is_normalized_for_upstream_cli(self):
        self.assertEqual(
            server.browser_ship_argument("Wonder of the Seas"),
            "Wonder",
        )

    def test_catalog_is_parsed_and_duplicate_product_codes_are_removed(self):
        parsed = server.parse_browser_output(
            SAMPLE_OUTPUT,
            ship="Wonder of the Seas",
            sail_date="2031-06-20",
            currency="USD",
        )
        by_id = {item["id"]: item for item in parsed["items"]}
        self.assertEqual(set(by_id), {"3222", "0904", "ZH01"})
        self.assertEqual(by_id["3222"]["prefix"], "beverage")
        self.assertEqual(by_id["3222"]["price"], 95.99)
        self.assertEqual(by_id["3222"]["unit"], "day")
        self.assertFalse(by_id["0904"]["price_available"])
        self.assertEqual(by_id["ZH01"]["subcategory"], "Day 4: Perfect Day Cococay, Bahamas")
        self.assertEqual(
            by_id["ZH01"]["description"],
            "A fast ride - bring a towel & sunscreen.",
        )
        self.assertEqual(
            by_id["3222"]["description"],
            "Drinks throughout the cruise, subject to the package terms.",
        )
        self.assertEqual(
            by_id["0904"]["description"],
            by_id["3222"]["description"],
        )
        self.assertEqual(len(parsed["sailing"]["itinerary"]), 2)

    def test_invalid_and_unknown_description_markers_are_ignored(self):
        output = SAMPLE_OUTPUT + """
__ROYAL_PRICE_DASHBOARD_DESCRIPTION__ not-json
__ROYAL_PRICE_DASHBOARD_DESCRIPTION__ {"id":"UNKNOWN","description":"No match"}
"""
        parsed = server.parse_browser_output(
            output,
            ship="Wonder of the Seas",
            sail_date="2031-06-20",
            currency="USD",
        )
        self.assertEqual(len(parsed["items"]), 3)

    def test_description_normalization_bounds_and_sanitizes_public_copy(self):
        description = server.normalize_product_description(
            f"<div>First&nbsp;part</div><p>Second{chr(0x2014)}part</p>"
            "<style>hidden</style>"
        )
        self.assertEqual(description, "First part Second - part")
        self.assertEqual(
            len(
                server.normalize_product_description(
                    "x" * (server.MAX_PRODUCT_DESCRIPTION_CHARS + 50)
                )
            ),
            server.MAX_PRODUCT_DESCRIPTION_CHARS,
        )

    def test_ship_menu_is_structured_and_brand_is_inferred(self):
        ships = server.parse_ship_menu(SHIP_MENU_OUTPUT)

        self.assertEqual(
            [(ship["name"], ship["cruise_line"]) for ship in ships],
            [
                ("Wonder of the Seas", "royal-caribbean"),
                ("Celebrity Beyond", "celebrity"),
            ],
        )

    def test_sailing_menu_is_translated_to_iso_with_duration(self):
        sailings = server.parse_sailing_menu(SAILING_MENU_OUTPUT)

        self.assertEqual(sailings[0]["sail_date"], "2031-06-20")
        self.assertEqual(sailings[0]["duration"], 4)
        self.assertEqual(sailings[1]["sail_date"], "2027-02-21")
        self.assertEqual(sailings[1]["duration"], 7)


class UpstreamAdapterTests(unittest.TestCase):
    PRODUCT_QUERY = """
    query WebProductsByCategory {
      products {
        commerceProducts {
          id
          title
          variantOptions { code name }
        }
      }
    }
    """

    def payload(self, category="shorex"):
        return {
            "operationName": "WebProductsByCategory",
            "variables": {"category": category},
            "query": self.PRODUCT_QUERY,
        }

    def test_description_is_requested_for_every_product_category(self):
        self.assertEqual(
            upstream_adapter.DESCRIPTION_MARKER,
            server.PRODUCT_DESCRIPTION_MARKER,
        )
        original = self.payload()
        updated = upstream_adapter.add_description_to_product_query(original)

        self.assertIsNot(updated, original)
        self.assertIn("title description", updated["query"])
        self.assertNotIn("title description", original["query"])
        beverage = self.payload("beverage")
        updated_beverage = upstream_adapter.add_description_to_product_query(
            beverage
        )
        self.assertIsNot(updated_beverage, beverage)
        self.assertIn("title description", updated_beverage["query"])

        unrelated = {
            "operationName": "WebCategories",
            "variables": {},
            "query": self.PRODUCT_QUERY,
        }
        self.assertIs(
            upstream_adapter.add_description_to_product_query(unrelated),
            unrelated,
        )

    def test_extensions_preserve_output_and_emit_machine_readable_markers(self):
        requests = []
        printed = []
        logged = []

        def original_request(*args, **kwargs):
            requests.append((args, kwargs))
            return "response"

        def original_print(*args):
            printed.append(args)

        namespace = {
            "_execute_api_request": original_request,
            "print_and_sort_products": original_print,
            "log": logged.append,
        }
        upstream_adapter.install_extensions(namespace)

        response = namespace["_execute_api_request"](
            method="POST",
            json_data=self.payload(),
        )
        self.assertEqual(response, "response")
        self.assertIn("title description", requests[0][1]["json_data"]["query"])

        products = [
            {
                "id": "ZH01",
                "description": "A sample description.",
                "variantOptions": [
                    {"code": "ZH01", "name": "Default"},
                    {"code": "ZH02", "name": "Larger option"},
                ],
            },
            {"id": "BLANK", "description": ""},
        ]
        namespace["print_and_sort_products"](
            products,
            "alpha",
            "asc",
            "USD",
            "shorex",
            True,
        )
        self.assertEqual(len(printed), 1)
        self.assertEqual(len(logged), 2)
        self.assertTrue(logged[0].startswith(upstream_adapter.DESCRIPTION_MARKER))
        markers = [
            json.loads(entry.removeprefix(upstream_adapter.DESCRIPTION_MARKER))
            for entry in logged
        ]
        self.assertEqual(
            markers,
            [
                {"id": "ZH01", "description": "A sample description."},
                {"id": "ZH02", "description": "A sample description."},
            ],
        )

        namespace["print_and_sort_products"](
            products,
            "alpha",
            "asc",
            "USD",
            "beverage",
            True,
        )
        self.assertEqual(len(logged), 4)
        self.assertTrue(logged[2].startswith(upstream_adapter.DESCRIPTION_MARKER))

    def test_main_loads_and_extends_a_pinned_module_without_rewriting_it(self):
        fake_source = """
def _execute_api_request(*args, **kwargs):
    print(kwargs["json_data"]["query"])
    return None

def print_and_sort_products(*args):
    print("ORIGINAL PRODUCT OUTPUT")

log = print

def main(args=None):
    _execute_api_request(json_data={
        "operationName": "WebProductsByCategory",
        "variables": {"category": "beverage"},
        "query": "commerceProducts { id title variantOptions { code } }",
    })
    print_and_sort_products(
        [{"id": "ZH01", "description": "Adapter description."}],
        "alpha",
        "asc",
        "USD",
        "beverage",
        True,
    )
"""
        with tempfile.TemporaryDirectory() as temp_directory:
            pinned_script = Path(temp_directory) / "PinnedBrowser.py"
            pinned_script.write_text(fake_source, encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(
                upstream_adapter.os.environ,
                {upstream_adapter.PINNED_SCRIPT_ENV: str(pinned_script)},
            ), mock.patch("sys.stdout", output):
                upstream_adapter.main([])

        rendered = output.getvalue()
        self.assertIn("title description", rendered)
        self.assertIn("ORIGINAL PRODUCT OUTPUT", rendered)
        self.assertIn(upstream_adapter.DESCRIPTION_MARKER, rendered)


class CruiseCompletionTests(unittest.TestCase):
    def test_return_date_is_sail_date_plus_number_of_nights(self):
        config = {
            "sail_date": "2026-08-20",
            "duration": 7,
        }

        before_return = server.cruise_completion(
            config,
            today=date(2026, 8, 26),
        )
        on_return = server.cruise_completion(
            config,
            today=date(2026, 8, 27),
        )

        self.assertEqual(before_return["return_date"], "2026-08-27")
        self.assertFalse(before_return["completed"])
        self.assertTrue(on_return["completed"])

    def test_unknown_duration_does_not_guess_at_completion(self):
        self.assertEqual(
            server.cruise_completion(
                {"sail_date": "2026-08-20", "duration": None},
                today=date(2026, 8, 27),
            ),
            {"return_date": None, "completed": False},
        )

    def test_duration_is_inferred_from_a_cached_sailing_description(self):
        config = {
            "sail_date": "2026-08-20",
            "duration": None,
            "description": None,
        }
        catalog = {
            "sailing": {
                "description": (
                    "Wonder of the Seas sailing on 08/20/26 "
                    "(7 Night Bahamas Cruise)"
                ),
            }
        }

        self.assertEqual(server.cruise_duration(config, catalog), 7)
        self.assertEqual(
            server.cruise_completion(
                config,
                catalog,
                today=date(2026, 8, 27),
            ),
            {"return_date": "2026-08-27", "completed": True},
        )


class MultiCruiseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temp.name)
        self.options_file = self.data_root / "options.json"
        self.options_file.write_text(
            json.dumps(
                {
                    "refresh_interval_hours": 24,
                    "notifications_enabled": False,
                }
            ),
            encoding="utf-8",
        )
        self.first_date = (
            datetime.now(timezone.utc).date() + timedelta(days=120)
        ).isoformat()
        self.second_date = (
            datetime.now(timezone.utc).date() + timedelta(days=240)
        ).isoformat()

    def tearDown(self):
        self.temp.cleanup()

    def add_cruise(self, manager, *, ship, sail_date, description):
        return manager.create_cruise(
            {
                "cruise_line": server.cruise_line_for_ship(ship),
                "ship": ship,
                "sail_date": sail_date,
                "duration": 7,
                "description": description,
                "currency": "USD",
                "refresh_interval_hours": 24,
                "notifications_enabled": False,
            }
        )

    def catalog_for(self, ship, sail_date):
        catalog = server.parse_browser_output(
            SAMPLE_OUTPUT,
            ship=ship,
            sail_date=sail_date,
            currency="USD",
        )
        catalog["sailing"]["ship"] = ship
        catalog["sailing"]["sail_date"] = sail_date
        return catalog

    def test_clean_install_requires_onboarding(self):
        manager = server.CatalogManager(self.data_root, self.options_file)

        state = manager.state()
        self.assertTrue(state["setup_required"])
        self.assertEqual(state["cruises"], [])
        self.assertEqual(state["catalog"]["items"], [])

    def test_cruise_preferences_and_catalogs_are_isolated_and_reload(self):
        manager = server.CatalogManager(self.data_root, self.options_file)
        first_id = self.add_cruise(
            manager,
            ship="Wonder of the Seas",
            sail_date=self.first_date,
            description="7 Night Bahamas Cruise",
        )
        manager.catalog = self.catalog_for("Wonder of the Seas", self.first_date)
        server.write_json_atomic(manager.catalog_file, manager.catalog)
        manager.set_watching("3222", True, 80.0)
        manager.set_pinned("ZH01", True)

        second_id = self.add_cruise(
            manager,
            ship="Celebrity Beyond",
            sail_date=self.second_date,
            description="7 Night Caribbean Cruise",
        )
        manager.catalog = self.catalog_for("Celebrity Beyond", self.second_date)
        server.write_json_atomic(manager.catalog_file, manager.catalog)
        manager.set_pinned("3222", True)

        second_state = manager.state()
        self.assertEqual(second_state["active_cruise_id"], second_id)
        self.assertEqual(second_state["preferences"]["watching"], {})
        self.assertEqual(second_state["preferences"]["pinned"], ["3222"])

        manager.set_active_cruise(first_id)
        first_state = manager.state()
        self.assertIn("3222", first_state["preferences"]["watching"])
        self.assertEqual(first_state["preferences"]["pinned"], ["ZH01"])

        reloaded = server.CatalogManager(self.data_root, self.options_file)
        reloaded.set_active_cruise(second_id)
        self.assertEqual(reloaded.state()["preferences"]["pinned"], ["3222"])
        reloaded.set_active_cruise(first_id)
        self.assertIn("3222", reloaded.state()["preferences"]["watching"])

    def test_duplicate_cruise_is_rejected(self):
        manager = server.CatalogManager(self.data_root, self.options_file)
        kwargs = {
            "ship": "Wonder of the Seas",
            "sail_date": self.first_date,
            "description": "7 Night Bahamas Cruise",
        }
        self.add_cruise(manager, **kwargs)

        with self.assertRaisesRegex(server.DashboardError, "already"):
            self.add_cruise(manager, **kwargs)

    def test_http_creation_can_require_an_authoritative_discovered_sailing(self):
        manager = server.CatalogManager(self.data_root, self.options_file)
        manager._ship_cache = (
            time.monotonic(),
            [
                {
                    "id": "wonder",
                    "name": "Wonder of the Seas",
                    "cruise_line": "royal-caribbean",
                }
            ],
        )
        manager._sailing_cache["wonder of the seas"] = (
            time.monotonic(),
            [
                {
                    "sail_date": self.first_date,
                    "display_date": date.fromisoformat(self.first_date).strftime(
                        "%m/%d/%y"
                    ),
                    "duration": 4,
                    "description": "4 Night Bahamas & Perfect Day Cruise",
                }
            ],
        )

        cruise_id = manager.create_cruise(
            {
                "cruise_line": "royal-caribbean",
                "ship": "Wonder of the Seas",
                "sail_date": self.first_date,
                "duration": 99,
                "description": "Untrusted browser fields",
                "currency": "USD",
            },
            validate_discovery=True,
        )

        config = manager._runtime(cruise_id).config
        self.assertEqual(config["duration"], 4)
        self.assertEqual(
            config["description"],
            "4 Night Bahamas & Perfect Day Cruise",
        )

    def test_http_creation_resolves_the_canonical_ship_from_its_discovery_id(self):
        manager = server.CatalogManager(self.data_root, self.options_file)
        manager._ship_cache = (
            time.monotonic(),
            [
                {
                    "id": "freedom",
                    "name": "Freedom of the Seas",
                    "cruise_line": "royal-caribbean",
                }
            ],
        )
        manager._sailing_cache["freedom of the seas"] = (
            time.monotonic(),
            [
                {
                    "sail_date": self.first_date,
                    "display_date": date.fromisoformat(self.first_date).strftime(
                        "%m/%d/%y"
                    ),
                    "duration": 5,
                    "description": "5 Night Caribbean Getaway Cruise",
                }
            ],
        )

        cruise_id = manager.create_cruise(
            {
                "cruise_line": "stale-client-value",
                "ship_id": "freedom",
                "ship": "Stale client ship name",
                "sail_date": self.first_date,
                "currency": "USD",
            },
            validate_discovery=True,
        )

        config = manager._runtime(cruise_id).config
        self.assertEqual(config["cruise_line"], "royal-caribbean")
        self.assertEqual(config["ship"], "Freedom of the Seas")
        self.assertEqual(config["duration"], 5)

    def test_cruise_rejection_log_context_is_bounded_and_allowlisted(self):
        context = server.cruise_request_log_context(
            {
                "client_version": "0.4.1",
                "cruise_line": ["unexpected"],
                "ship_id": "freedom",
                "ship": "F" * 200,
                "sail_date": "2031-07-04",
                "unrelated_private_value": "must not be logged",
            }
        )

        self.assertEqual(
            set(context),
            {"client_version", "cruise_line", "ship_id", "ship", "sail_date"},
        )
        self.assertEqual(context["cruise_line"], "<list>")
        self.assertEqual(len(context["ship"]), 160)
        self.assertNotIn("unrelated_private_value", context)

    def test_http_post_uses_ship_id_when_client_line_and_name_are_stale(self):
        manager = server.CatalogManager(self.data_root, self.options_file)
        manager._ship_cache = (
            time.monotonic(),
            [
                {
                    "id": "freedom",
                    "name": "Freedom of the Seas",
                    "cruise_line": "royal-caribbean",
                }
            ],
        )
        manager._sailing_cache["freedom of the seas"] = (
            time.monotonic(),
            [
                {
                    "sail_date": self.first_date,
                    "display_date": date.fromisoformat(self.first_date).strftime(
                        "%m/%d/%y"
                    ),
                    "duration": 5,
                    "description": "5 Night Caribbean Getaway Cruise",
                }
            ],
        )
        manager.start_refresh = lambda _cruise_id=None, **_kwargs: False
        server.DashboardHandler.manager = manager
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{httpd.server_port}/api/cruises",
                data=json.dumps(
                    {
                        "client_version": "0.4.1",
                        "cruise_line": "stale-client-value",
                        "ship_id": "freedom",
                        "ship": "Stale client ship name",
                        "sail_date": self.first_date,
                        "currency": "USD",
                        "refresh_interval_hours": 24,
                        "notifications_enabled": True,
                    }
                ).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 201)
                payload = json.load(response)

            with urllib.request.urlopen(
                f"http://127.0.0.1:{httpd.server_port}/health",
                timeout=5,
            ) as response:
                health = json.load(response)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        created = manager._runtime(payload["created"]).config
        self.assertEqual(created["cruise_line"], "royal-caribbean")
        self.assertEqual(created["ship"], "Freedom of the Seas")
        self.assertEqual(health, {"status": "ok", "version": "0.4.1"})

    def test_http_post_decodes_a_chunked_ingress_request_body(self):
        manager = server.CatalogManager(self.data_root, self.options_file)
        manager._ship_cache = (
            time.monotonic(),
            [
                {
                    "id": "freedom",
                    "name": "Freedom of the Seas",
                    "cruise_line": "royal-caribbean",
                }
            ],
        )
        manager._sailing_cache["freedom of the seas"] = (
            time.monotonic(),
            [
                {
                    "sail_date": self.first_date,
                    "display_date": date.fromisoformat(self.first_date).strftime(
                        "%m/%d/%y"
                    ),
                    "duration": 5,
                    "description": "5 Night Caribbean Getaway Cruise",
                }
            ],
        )
        manager.start_refresh = lambda _cruise_id=None, **_kwargs: False
        server.DashboardHandler.manager = manager
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            httpd.server_port,
            timeout=5,
        )
        try:
            encoded = json.dumps(
                {
                    "client_version": "0.4.1",
                    "cruise_line": "stale-client-value",
                    "ship_id": "freedom",
                    "ship": "Stale client ship name",
                    "sail_date": self.first_date,
                    "currency": "USD",
                    "refresh_interval_hours": 24,
                    "notifications_enabled": True,
                }
            ).encode("utf-8")
            split_at = len(encoded) // 2
            connection.request(
                "POST",
                "/api/cruises",
                body=iter((encoded[:split_at], encoded[split_at:])),
                headers={"Content-Type": "application/json"},
                encode_chunked=True,
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 201)
            payload = json.load(response)
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        created = manager._runtime(payload["created"]).config
        self.assertEqual(created["cruise_line"], "royal-caribbean")
        self.assertEqual(created["ship"], "Freedom of the Seas")

    def test_chunked_request_body_limit_is_enforced_before_reading_payload(self):
        handler = object.__new__(server.DashboardHandler)
        handler.headers = {"Transfer-Encoding": "chunked"}
        handler.rfile = io.BytesIO(b"f4241\r\n")

        with self.assertRaisesRegex(server.DashboardError, "too large"):
            handler._read_request_body()

    def test_http_creation_recovers_a_missing_line_from_the_discovered_ship(self):
        manager = server.CatalogManager(self.data_root, self.options_file)
        manager._ship_cache = (
            time.monotonic(),
            [
                {
                    "id": "freedom",
                    "name": "Freedom of the Seas",
                    "cruise_line": "royal-caribbean",
                }
            ],
        )
        manager._sailing_cache["freedom of the seas"] = (
            time.monotonic(),
            [
                {
                    "sail_date": self.first_date,
                    "display_date": date.fromisoformat(self.first_date).strftime(
                        "%m/%d/%y"
                    ),
                    "duration": 5,
                    "description": "5 Night Caribbean Getaway Cruise",
                }
            ],
        )

        cruise_id = manager.create_cruise(
            {
                "ship_id": "freedom",
                "ship": "Freedom of the Seas",
                "sail_date": self.first_date,
                "currency": "USD",
            },
            validate_discovery=True,
        )

        config = manager._runtime(cruise_id).config
        self.assertEqual(config["cruise_line"], "royal-caribbean")
        self.assertEqual(config["ship"], "Freedom of the Seas")
        self.assertEqual(config["duration"], 5)

    def test_removing_active_cruise_selects_another_and_deletes_scoped_data(self):
        manager = server.CatalogManager(self.data_root, self.options_file)
        first_id = self.add_cruise(
            manager,
            ship="Wonder of the Seas",
            sail_date=self.first_date,
            description="7 Night Bahamas Cruise",
        )
        second_id = self.add_cruise(
            manager,
            ship="Celebrity Beyond",
            sail_date=self.second_date,
            description="7 Night Caribbean Cruise",
        )
        second_runtime = manager._runtime(second_id)
        second_catalog = self.catalog_for("Celebrity Beyond", self.second_date)
        second_runtime.catalog = second_catalog
        server.write_json_atomic(second_runtime.catalog_file, second_catalog)
        manager._record_history(second_id)
        item = second_catalog["items"][0]
        self.assertEqual(
            manager.history.get_history(
                item=item,
                catalog=second_catalog,
                options=second_runtime.config,
            )["summary"]["events"],
            1,
        )

        removed, warnings = manager.remove_cruise(second_id)

        self.assertEqual(removed["id"], second_id)
        self.assertEqual(warnings, [])
        self.assertEqual(manager.state()["active_cruise_id"], first_id)
        self.assertFalse(second_runtime.directory.exists())
        self.assertEqual(
            manager.history.get_history(
                item=item,
                catalog=second_catalog,
                options=second_runtime.config,
            )["summary"]["events"],
            0,
        )
        reloaded = server.CatalogManager(self.data_root, self.options_file)
        self.assertEqual(
            [cruise["id"] for cruise in reloaded.state()["cruises"]],
            [first_id],
        )

    def test_removing_last_cruise_returns_to_onboarding(self):
        manager = server.CatalogManager(self.data_root, self.options_file)
        cruise_id = self.add_cruise(
            manager,
            ship="Wonder of the Seas",
            sail_date=self.first_date,
            description="7 Night Bahamas Cruise",
        )

        manager.remove_cruise(cruise_id)

        state = manager.state()
        self.assertTrue(state["setup_required"])
        self.assertIsNone(state["active_cruise_id"])
        self.assertEqual(state["cruises"], [])

    def test_refreshing_cruise_cannot_be_removed(self):
        manager = server.CatalogManager(self.data_root, self.options_file)
        cruise_id = self.add_cruise(
            manager,
            ship="Wonder of the Seas",
            sail_date=self.first_date,
            description="7 Night Bahamas Cruise",
        )
        manager._runtime(cruise_id).refreshing = True

        with self.assertRaisesRegex(server.DashboardError, "refresh"):
            manager.remove_cruise(cruise_id)

        self.assertEqual(manager.state()["active_cruise_id"], cruise_id)

    def test_completed_cruise_is_reported_and_excluded_from_refreshes(self):
        manager = server.CatalogManager(self.data_root, self.options_file)
        cruise_id = self.add_cruise(
            manager,
            ship="Wonder of the Seas",
            sail_date=self.first_date,
            description="7 Night Bahamas Cruise",
        )
        runtime = manager._runtime(cruise_id)
        today = datetime.now(timezone.utc).date()
        runtime.config["sail_date"] = (today - timedelta(days=7)).isoformat()
        runtime.config["duration"] = 7

        state = manager.state()
        summary = next(
            cruise for cruise in state["cruises"] if cruise["id"] == cruise_id
        )

        self.assertEqual(summary["return_date"], today.isoformat())
        self.assertTrue(summary["completed"])
        self.assertEqual(state["status"]["return_date"], today.isoformat())
        self.assertTrue(state["status"]["completed"])
        self.assertFalse(manager.is_refresh_due(cruise_id))
        self.assertNotIn(cruise_id, manager.due_cruise_ids())
        with self.assertRaisesRegex(server.DashboardError, "no longer refreshed"):
            manager.start_refresh(cruise_id)

    def test_future_cruise_without_a_catalog_remains_due(self):
        manager = server.CatalogManager(self.data_root, self.options_file)
        cruise_id = self.add_cruise(
            manager,
            ship="Wonder of the Seas",
            sail_date=self.first_date,
            description="7 Night Bahamas Cruise",
        )

        self.assertFalse(manager.state()["status"]["completed"])
        self.assertTrue(manager.is_refresh_due(cruise_id))
        self.assertIn(cruise_id, manager.due_cruise_ids())

    def test_refresh_cooldown_blocks_manual_and_scheduled_retries(self):
        manager = server.CatalogManager(self.data_root, self.options_file)
        cruise_id = self.add_cruise(
            manager,
            ship="Wonder of the Seas",
            sail_date=self.first_date,
            description="7 Night Bahamas Cruise",
        )
        runtime = manager._runtime(cruise_id)
        runtime.next_refresh_allowed_at = datetime.now(timezone.utc) + timedelta(
            minutes=5
        )

        with self.assertRaisesRegex(server.DashboardError, "Please wait 5 minutes"):
            manager.start_refresh(cruise_id, manual=True)

        state = manager.state()
        self.assertGreater(state["status"]["refresh_cooldown_seconds"], 0)
        self.assertIsNotNone(state["status"]["refresh_available_at"])
        self.assertFalse(manager.is_refresh_due(cruise_id))
        self.assertNotIn(cruise_id, manager.due_cruise_ids())

    def test_failed_refresh_uses_exponential_backoff(self):
        manager = server.CatalogManager(self.data_root, self.options_file)
        cruise_id = self.add_cruise(
            manager,
            ship="Wonder of the Seas",
            sail_date=self.first_date,
            description="7 Night Bahamas Cruise",
        )
        runtime = manager._runtime(cruise_id)

        with mock.patch.object(
            server,
            "run_browser",
            side_effect=server.DashboardError("Temporary upstream failure"),
        ), mock.patch.object(server.LOGGER, "exception"):
            runtime.refreshing = True
            manager._refresh_worker(cruise_id)
            first_cooldown = manager.state()["status"]["refresh_cooldown_seconds"]

            runtime.refreshing = True
            manager._refresh_worker(cruise_id)
            second_cooldown = manager.state()["status"]["refresh_cooldown_seconds"]

        self.assertFalse(runtime.refreshing)
        self.assertEqual(runtime.consecutive_refresh_failures, 2)
        self.assertEqual(runtime.last_error, "Temporary upstream failure")
        self.assertGreaterEqual(
            first_cooldown,
            server.REFRESH_FAILURE_BACKOFF_BASE_SECONDS - 1,
        )
        self.assertGreaterEqual(
            second_cooldown,
            (server.REFRESH_FAILURE_BACKOFF_BASE_SECONDS * 2) - 1,
        )

    def test_http_delete_removes_cruise_and_returns_updated_state(self):
        manager = server.CatalogManager(self.data_root, self.options_file)
        cruise_id = self.add_cruise(
            manager,
            ship="Wonder of the Seas",
            sail_date=self.first_date,
            description="7 Night Bahamas Cruise",
        )
        server.DashboardHandler.manager = manager
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{httpd.server_port}/api/cruises/{cruise_id}",
                method="DELETE",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.load(response)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        self.assertEqual(payload["removed"]["id"], cruise_id)
        self.assertEqual(payload["warnings"], [])
        self.assertTrue(payload["state"]["setup_required"])

    def test_legacy_migration_copies_data_and_retains_original_files(self):
        legacy_options = {
            "ship": "Wonder of the Seas",
            "sail_date": self.first_date,
            "currency": "USD",
            "refresh_interval_hours": 24,
            "notifications_enabled": False,
        }
        self.options_file.write_text(json.dumps(legacy_options), encoding="utf-8")
        catalog = self.catalog_for("Wonder of the Seas", self.first_date)
        preferences = {"hidden": ["ZH01"], "watching": {}}
        server.write_json_atomic(self.data_root / "catalog.json", catalog)
        server.write_json_atomic(self.data_root / "preferences.json", preferences)

        manager = server.CatalogManager(self.data_root, self.options_file)

        self.assertFalse(manager.state()["setup_required"])
        self.assertEqual(manager.preferences["pinned"], [])
        self.assertNotIn("hidden", manager.preferences)
        self.assertTrue((self.data_root / "catalog.json").is_file())
        self.assertTrue((self.data_root / "preferences.json").is_file())
        self.assertTrue(manager.catalog_file.is_file())
        self.assertTrue(manager.preferences_file.is_file())


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temp.name)
        self.options_file = self.data_root / "options.json"
        self.options_file.write_text(
            json.dumps(
                {
                    "ship": "Wonder of the Seas",
                    "sail_date": "2031-06-20",
                    "currency": "USD",
                    "refresh_interval_hours": 24,
                    "notifications_enabled": False,
                }
            ),
            encoding="utf-8",
        )
        self.manager = server.CatalogManager(self.data_root, self.options_file)
        self.manager.catalog = server.parse_browser_output(
            SAMPLE_OUTPUT,
            ship="Wonder of the Seas",
            sail_date="2031-06-20",
            currency="USD",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_pin_is_independent_from_watch(self):
        self.manager.set_watching("3222", True)
        self.manager.set_pinned("3222", True)
        state = self.manager.state()
        self.assertIn("3222", state["preferences"]["pinned"])
        self.assertIn("3222", state["preferences"]["watching"])
        self.assertEqual(
            state["preferences"]["watching"]["3222"]["target_price"],
            95.99,
        )

    def test_watch_export_uses_upstream_fields(self):
        self.manager.set_watching("3222", True, 79.99)
        exported = self.manager.export_watchlist()
        self.assertIn('name: "Deluxe Beverage Package"', exported)
        self.assertIn('prefix: "beverage"', exported)
        self.assertIn('product: "3222"', exported)
        self.assertIn("price: 79.99", exported)

    def test_target_change_resets_prior_alert_marker(self):
        self.manager.set_watching("3222", True)
        self.manager.preferences["watching"]["3222"]["last_alerted_price"] = 80.0
        self.manager.set_target("3222", 75.0)
        watch = self.manager.state()["preferences"]["watching"]["3222"]
        self.assertEqual(watch["target_price"], 75.0)
        self.assertIsNone(watch["last_alerted_price"])

    def test_only_explicit_watches_can_be_alert_candidates(self):
        self.manager.set_watching("3222", True, 100.0)
        self.manager.set_pinned("3222", True)

        candidates = self.manager._alert_candidates_locked()

        self.assertEqual([item["id"] for item in candidates], ["3222"])
        self.assertEqual(candidates[0]["target_price"], 100.0)
        self.assertNotIn(
            "ZH01",
            [item["id"] for item in candidates],
            "An unwatched catalog item must never create an alert.",
        )

    def test_repeated_price_does_not_repeat_an_alert(self):
        self.manager.set_watching("3222", True, 100.0)
        self.manager.preferences["watching"]["3222"]["last_alerted_price"] = 95.99

        self.assertEqual(self.manager._alert_candidates_locked(), [])


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temp.name)
        self.options = {
            "ship": "Wonder of the Seas",
            "sail_date": "2031-06-20",
            "currency": "USD",
            "refresh_interval_hours": 24,
            "notifications_enabled": False,
        }
        self.catalog = server.parse_browser_output(
            SAMPLE_OUTPUT,
            ship=self.options["ship"],
            sail_date=self.options["sail_date"],
            currency=self.options["currency"],
        )
        self.catalog["generated_at"] = "2026-08-27T03:44:55+00:00"
        self.store = server.HistoryStore(self.data_root / "history.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def item(self, item_id):
        return next(item for item in self.catalog["items"] if item["id"] == item_id)

    def test_initial_catalog_is_recorded_once(self):
        self.assertEqual(self.store.record_catalog(self.catalog, self.options), 3)
        self.assertEqual(self.store.record_catalog(self.catalog, self.options), 0)

        history = self.store.get_history(
            item=self.item("3222"),
            catalog=self.catalog,
            options=self.options,
        )
        self.assertEqual(len(history["points"]), 1)
        self.assertEqual(history["summary"]["current_price"], 95.99)
        self.assertEqual(history["summary"]["lowest_price"], 95.99)

    def test_price_and_availability_changes_are_recorded(self):
        self.store.record_catalog(self.catalog, self.options)

        discounted = copy.deepcopy(self.catalog)
        discounted["generated_at"] = "2026-08-28T03:44:55+00:00"
        deluxe = next(item for item in discounted["items"] if item["id"] == "3222")
        deluxe["price"] = 79.99
        self.assertEqual(self.store.record_catalog(discounted, self.options), 1)

        unavailable = copy.deepcopy(discounted)
        unavailable["generated_at"] = "2026-08-29T03:44:55+00:00"
        deluxe = next(item for item in unavailable["items"] if item["id"] == "3222")
        deluxe["price"] = None
        deluxe["price_available"] = False
        self.assertEqual(self.store.record_catalog(unavailable, self.options), 1)

        history = self.store.get_history(
            item=deluxe,
            catalog=unavailable,
            options=self.options,
        )
        self.assertEqual(
            [point["price"] for point in history["points"]],
            [95.99, 79.99, None],
        )
        self.assertEqual(history["summary"]["lowest_price"], 79.99)
        self.assertEqual(history["summary"]["highest_price"], 95.99)
        self.assertIsNone(history["summary"]["current_price"])

    def test_histories_are_isolated_by_sailing(self):
        self.store.record_catalog(self.catalog, self.options)
        other_options = {**self.options, "sail_date": "2027-02-01"}
        other_catalog = copy.deepcopy(self.catalog)
        other_catalog["sailing"]["sail_date"] = "2027-02-01"
        other_catalog["generated_at"] = "2026-08-28T03:44:55+00:00"
        self.assertEqual(self.store.record_catalog(other_catalog, other_options), 3)

        history = self.store.get_history(
            item=next(item for item in other_catalog["items"] if item["id"] == "3222"),
            catalog=other_catalog,
            options=other_options,
        )
        self.assertEqual(len(history["points"]), 1)
        self.assertEqual(history["sailing"]["sail_date"], "2027-02-01")

    def test_history_expires_thirty_days_after_sailing(self):
        old_options = {**self.options, "sail_date": "2020-01-01"}
        old_catalog = copy.deepcopy(self.catalog)
        old_catalog["sailing"]["sail_date"] = "2020-01-01"
        self.store.record_catalog(old_catalog, old_options)

        self.assertEqual(self.store.purge_expired(date(2020, 1, 30)), 0)
        self.assertEqual(self.store.purge_expired(date(2020, 1, 31)), 3)

    def test_manager_seeds_history_from_an_existing_cached_catalog(self):
        options_file = self.data_root / "options.json"
        options_file.write_text(json.dumps(self.options), encoding="utf-8")
        server.write_json_atomic(self.data_root / "catalog.json", self.catalog)

        manager = server.CatalogManager(self.data_root, options_file)
        history = manager.history_for("3222")

        self.assertEqual(len(history["points"]), 1)
        self.assertEqual(history["points"][0]["price"], 95.99)


if __name__ == "__main__":
    unittest.main()
