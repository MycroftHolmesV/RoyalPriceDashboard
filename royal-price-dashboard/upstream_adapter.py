from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable


DESCRIPTION_MARKER = "__ROYAL_PRICE_DASHBOARD_DESCRIPTION__ "
PINNED_SCRIPT_ENV = "ROYAL_PRICE_PINNED_SCRIPT"
DEFAULT_PINNED_SCRIPT = Path("/opt/upstream/BrowseRoyalCaribbeanPrice.py")


def extend_product_query(payload: Any) -> Any:
    """Request product descriptions, page boundaries, and typed catalog errors."""
    if not isinstance(payload, dict):
        return payload
    if payload.get("operationName") != "WebProductsByCategory":
        return payload

    query = payload.get("query")
    if not isinstance(query, str):
        return payload

    updated_query, replacements = re.subn(
        r"(commerceProducts\s*\{\s*id\s+title)(\s+variantOptions)",
        r"\1 description\2",
        query,
        count=1,
    )
    if replacements != 1:
        raise RuntimeError(
            "The pinned upstream product query has an unexpected shape."
        )

    updated = copy.deepcopy(payload)
    success_fragment = "... on CommerceProductResultSuccess {"
    if updated_query.count(success_fragment) != 1:
        raise RuntimeError("The pinned upstream product result has an unexpected shape.")
    updated["query"] = updated_query.replace(
        success_fragment,
        "... on CommerceProductExceptions { exceptions { __typename code } }\n"
        + success_fragment + "\npageInfo { hasNextPage }",
    )
    return updated


def emit_description_markers(
    products: Any,
    logger: Callable[[str], Any],
) -> None:
    """Emit compact parser markers for described products and their variants."""
    if not isinstance(products, list):
        return

    seen: set[str] = set()
    for product in products:
        if not isinstance(product, dict):
            continue
        description = product.get("description")
        if not isinstance(description, str) or not description.strip():
            continue

        product_ids = [product.get("id")]
        variant_options = product.get("variantOptions")
        if isinstance(variant_options, list):
            product_ids.extend(
                option.get("code")
                for option in variant_options
                if isinstance(option, dict)
            )

        for raw_product_id in product_ids:
            product_id = str(raw_product_id or "").strip()
            if not product_id or product_id in seen:
                continue
            seen.add(product_id)
            marker = json.dumps(
                {"id": product_id, "description": description},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            logger(DESCRIPTION_MARKER + marker)


def validate_catalog_response(operation: str, response: Any, page: int = 0) -> bool | None:
    """Validate the response and return hasNextPage for a product request."""
    # The pinned browser treats a failed request just like an empty final page.
    if response is None:
        raise RuntimeError("A product catalog page request failed.")
    body = response.json()
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("A product catalog page had no result.")
    errors = body.get("errors")
    if operation == "WebCategories":
        container = data.get("categories")
        categories = container.get("categories") if isinstance(container, dict) else None
        if errors or not isinstance(categories, list) or any(
            not isinstance(category, dict) or not category.get("id") or not category.get("name")
            for category in categories
        ):
            raise RuntimeError("The product catalog category list was incomplete.")
        return None

    container = data.get("products")
    if isinstance(container, dict) and "exceptions" in container:
        # A shore-excursion day with no products returns this typed 404 on page zero.
        # A missing later page contradicts the prior response's hasNextPage.
        if not errors and page == 0 and container["exceptions"] == [
            {"__typename": "CommerceProductNotFound", "code": "404"}
        ]:
            return False
        raise RuntimeError("A product catalog page returned an unexpected product exception.")
    if not isinstance(container, dict) or "commerceProducts" not in container:
        raise RuntimeError("A product catalog page had no product result.")
    products = container["commerceProducts"]
    if products is not None and not isinstance(products, list):
        raise RuntimeError("A product catalog page had an invalid product list.")
    # A missing optional description leaves the product identity and quote intact.
    for error in errors or []:
        path = error.get("path") if isinstance(error, dict) else None
        if not (
            isinstance(path, list) and len(path) == 4
            and path[:2] == ["products", "commerceProducts"]
            and isinstance(path[2], int) and isinstance(products, list)
            and 0 <= path[2] < len(products) and path[3] == "description"
        ):
            raise RuntimeError("A product catalog page returned GraphQL errors.")
    page_info = container.get("pageInfo")
    has_next = page_info.get("hasNextPage") if isinstance(page_info, dict) else None
    if not isinstance(has_next, bool) or (not products and (has_next or page > 0)):
        raise RuntimeError("A product catalog page had incomplete pagination information.")
    # The pinned browser loops over range(100), so it cannot fetch a 101st page.
    if has_next and page == 99:
        raise RuntimeError("The product catalog exceeded the upstream browser's page limit.")
    return has_next


def install_extensions(namespace: dict[str, Any]) -> None:
    """Extend the exact pinned module without modifying its source file."""
    original_request = namespace.get("_execute_api_request")
    original_print = namespace.get("print_and_sort_products")
    if not callable(original_request) or not callable(original_print):
        raise RuntimeError("The pinned upstream browser is missing required functions.")

    past_last_page: dict[str, Any] | None = None

    def request_catalog_page(*args: Any, **kwargs: Any) -> Any:
        nonlocal past_last_page
        payload = kwargs.get("json_data")
        operation = payload.get("operationName") if isinstance(payload, dict) else None
        if operation == "WebProductsByCategory":
            variables = payload["variables"]
            if past_last_page is not None and variables == past_last_page:
                # End the pinned browser's loop before it requests an out-of-range page.
                past_last_page = None
                return None
            kwargs["json_data"] = extend_product_query(payload)
        past_last_page = None
        response = original_request(*args, **kwargs)
        if operation == "WebCategories":
            validate_catalog_response(operation, response)
        elif operation == "WebProductsByCategory":
            page = variables["currentPage"]
            if not validate_catalog_response(operation, response, page):
                past_last_page = {**kwargs["json_data"]["variables"], "currentPage": page + 1}
        return response

    def print_with_description_markers(
        products: Any,
        sort_key: str,
        sort_order: str,
        currency: str,
        key: str,
        show_watchlist_codes: bool,
    ) -> None:
        original_print(
            products,
            sort_key,
            sort_order,
            currency,
            key,
            show_watchlist_codes,
        )
        logger = namespace.get("log")
        if show_watchlist_codes and callable(logger):
            emit_description_markers(products, logger)

    namespace["_execute_api_request"] = request_catalog_page
    namespace["print_and_sort_products"] = print_with_description_markers


def main(args: list[str] | None = None) -> None:
    pinned_script = Path(
        os.environ.get(PINNED_SCRIPT_ENV, str(DEFAULT_PINNED_SCRIPT))
    ).resolve()
    if not pinned_script.is_file():
        raise RuntimeError(f"Pinned upstream browser is missing: {pinned_script}")
    if pinned_script == Path(__file__).resolve():
        raise RuntimeError(
            "The upstream adapter cannot load itself as the pinned browser."
        )

    module_name = "royal_price_dashboard_pinned_upstream"
    spec = importlib.util.spec_from_file_location(module_name, pinned_script)
    if spec is None or spec.loader is None:
        raise RuntimeError("The pinned upstream browser could not be loaded.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    namespace = vars(module)
    install_extensions(namespace)
    upstream_main = namespace.get("main")
    if not callable(upstream_main):
        raise RuntimeError("The pinned upstream browser has no main entry point.")
    upstream_main(sys.argv[1:] if args is None else args)


if __name__ == "__main__":
    main()
