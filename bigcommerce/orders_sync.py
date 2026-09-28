"""Fetch BigCommerce orders modified in the last N days and log status-significant changes.

Credentials are read from environment variables (see .env.example):
    BC_STORE_HASH    - the store hash, e.g. "abc123"
    BC_ACCESS_TOKEN  - API account access token (X-Auth-Token)
    BC_CLIENT_ID     - optional, only needed for OAuth-style apps
"""

import argparse
from email.utils import format_datetime
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
import urllib

from logger import AppLogger
from pipe17 import Pipe17Client
from shipperhq import ShipperHQClient, find_shipment_by_detail_name, get_carrier_code, map_to_wwex

load_dotenv()

log = AppLogger(__name__)

API_BASE = "https://api.bigcommerce.com/stores/{store_hash}/v2/orders"
V3_API_BASE = "https://api.bigcommerce.com/stores/{store_hash}/v3/orders"
PAGE_LIMIT = 250
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

# Order statuses worth surfacing louder than a routine INFO line.
STATUS_LOG_LEVELS = {
    "Cancelled": "warning",
    "Declined": "warning",
    "Refunded": "warning",
    "Partially Refunded": "warning",
    "Disputed": "error",
    "Manual Verification Required": "warning",
}


class BigCommerceClient:
    def __init__(self, store_hash=None, access_token=None, client_id=None):
        self.store_hash = store_hash or os.getenv("BC_STORE_HASH")
        self.access_token = access_token or os.getenv("BC_ACCESS_TOKEN")
        self.client_id = client_id or os.getenv("BC_CLIENT_ID")

        if not self.store_hash or not self.access_token:
            log.critical("Missing BigCommerce credentials (BC_STORE_HASH / BC_ACCESS_TOKEN env vars).")
            raise ValueError("BigCommerce store hash and access token are required.")

        self.session = requests.Session()
        self.session.headers.update({
            "X-Auth-Token": self.access_token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        if self.client_id:
            self.session.headers["X-Auth-Client"] = self.client_id

        log.debug(f"Initialized BigCommerce client for store {self.store_hash}")

    def _request(self, url, params=None, method="GET", json_body=None):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.request(
                    method, url, params=params, json=json_body, timeout=REQUEST_TIMEOUT
                )
            except requests.exceptions.RequestException as exc:
                log.error(f"Network error calling BigCommerce API (attempt {attempt}/{MAX_RETRIES}): {exc}")
                if attempt == MAX_RETRIES:
                    raise
                time.sleep(2 ** attempt)
                continue

            if response.status_code == 429:
                retry_after_ms = int(response.headers.get("X-Rate-Limit-Time-Reset-Ms", 1000))
                retry_after = retry_after_ms / 1000
                log.warning(
                    f"Rate limited by BigCommerce API. Retrying in {retry_after:.1f}s "
                    f"(attempt {attempt}/{MAX_RETRIES})"
                )
                time.sleep(retry_after)
                continue

            if response.status_code == 401:
                log.critical("BigCommerce authentication failed (401). Check BC_ACCESS_TOKEN / BC_STORE_HASH.")
                response.raise_for_status()

            if response.status_code == 204:
                return []

            if not response.ok:
                log.error(f"BigCommerce API returned {response.status_code} for {url}: {response.text[:500]}")
                response.raise_for_status()

            return response.json()

        raise RuntimeError("Exceeded max retries calling BigCommerce API.")
    

    def get_orders_modified_since(self, min_date_modified: int = None):
        """Yield orders with date_modified >= `min_date_modified` (Unix timestamp), handling pagination."""
        url = API_BASE.format(store_hash=self.store_hash)
        if min_date_modified is None:
            min_date_modified = (((datetime.now(timezone.utc) - timedelta(days=7))))
        
        page = 1
        total_fetched = 0

        log.info(f"Fetching orders modified since {min_date_modified}")

        while True:
            params = {
                "min_date_modified": format_datetime(min_date_modified),
                "page": page,
                "limit": PAGE_LIMIT,
            }
            log.debug(f"Requesting page {page} with params {params}")
            orders = self._request(url, params)

            if not orders:
                log.debug(f"No more orders returned at page {page}; stopping pagination.")
                break

            total_fetched += len(orders)
            log.info(f"Retrieved {len(orders)} orders on page {page} (total so far: {total_fetched})")

            for order in orders:
                yield order

            if len(orders) < PAGE_LIMIT:
                break
            page += 1

        log.info(f"Finished fetching orders. Total orders modified in range: {total_fetched}")

    def get_order_metafields(self, order_id: int):
        """Yield all metafields for a given order (V3 API), handling pagination."""
        url = f"{V3_API_BASE.format(store_hash=self.store_hash)}/{order_id}/metafields"
        page = 1

        while True:
            params = {"page": page, "limit": PAGE_LIMIT}
            response = self._request(url, params)

            data = response.get("data", []) if isinstance(response, dict) else []
            if not data:
                break

            for metafield in data:
                yield metafield

            pagination = response.get("meta", {}).get("pagination", {})
            total_pages = pagination.get("total_pages", page)
            if page >= total_pages:
                break
            page += 1

    def create_order_metafield(self, order_id: int, key: str, value: str,
                                namespace: str = "Pipe17", permission_set: str = "write"):
        """Create a metafield on the given order (V3 API)."""
        url = f"{V3_API_BASE.format(store_hash=self.store_hash)}/{order_id}/metafields"
        body = {
            "key": key,
            "value": value,
            "namespace": namespace,
            "permission_set": permission_set,
        }
        response = self._request(url, method="POST", json_body=body)
        log.info(f"Order #{order_id} | created metafield '{key}'='{value}' (namespace='{namespace}')")
        return response.get("data") if isinstance(response, dict) else response

    def update_order_metafield(self, order_id: int, metafield_id: int, value: str):
        """Update the value of an existing metafield on the given order (V3 API)."""
        url = f"{V3_API_BASE.format(store_hash=self.store_hash)}/{order_id}/metafields/{metafield_id}"
        response = self._request(url, method="PUT", json_body={"value": value})
        log.info(f"Order #{order_id} | updated metafield id={metafield_id} to '{value}'")
        return response.get("data") if isinstance(response, dict) else response

    def find_pipe17_updated_metafield(self, metafields: list):
        """Return this app's 'updated_in_pipe17' metafield from `metafields`, or None."""
        return next(
            (
                metafield for metafield in metafields
                if metafield.get("key") == "updated_in_pipe17"
                and str(metafield.get("owner_client_id")) == str(self.client_id)
            ),
            None,
        )

    def ensure_pipe17_updated_metafield(self, order_id: int, metafields: list):
        """Ensure the order has an 'updated_in_pipe17' metafield owned by this app; create it if missing."""
        has_flag = any(
            metafield.get("key") == "updated_in_pipe17"
            and str(metafield.get("owner_client_id")) == str(self.client_id)
            for metafield in metafields
        )
        if has_flag:
            log.debug(f"Order #{order_id} | 'updated_in_pipe17' metafield already present.")
            return None

        return self.create_order_metafield(order_id, "updated_in_pipe17", "created")

    def does_need_pipe17_processing(self, metafields: list):
        """Ensure the order has an 'updated_in_pipe17' metafield owned by this app; create it if missing."""
        has_flag = any(
            metafield.get("key") == "updated_in_pipe17"
            and str(metafield.get("owner_client_id")) == str(self.client_id)
            and metafield.get('value') == 'created'
            for metafield in metafields
        )

        return has_flag    


def log_order(order: dict):
    order_id = order.get("id")
    status = order.get("status", "Unknown")
    total = order.get("total_inc_tax")
    date_modified = order.get("date_modified")
    customer_id = order.get("customer_id")

    level_name = STATUS_LOG_LEVELS.get(status, "info")
    log_fn = getattr(log, level_name)

    log_fn(
        f"Order #{order_id} | status='{status}' | customer_id={customer_id} "
        f"| total={total} | modified={date_modified}"
    )


def log_metafields(order_id: int, metafields: list):
    if not metafields:
        log.debug(f"Order #{order_id} | no metafields")
        return

    log.info(f"Order #{order_id} | {len(metafields)} metafield(s)")
    for metafield in metafields:
        log.debug(
            f"Order #{order_id} | metafield id={metafield.get('id')} "
            f"namespace='{metafield.get('namespace')}' key='{metafield.get('key')}' "
            f"value='{metafield.get('value')}'"
        )


def main(days: int = 27):
    try:
        client = BigCommerceClient()
        shq_client = ShipperHQClient()
        pipe17_client = Pipe17Client()
    except ValueError:
        sys.exit(1)

    min_date_modified = (((datetime.now(timezone.utc) - timedelta(days=days))))


    order_count = 0
    try:
        orderList = list()

        for order in client.get_orders_modified_since(min_date_modified):
            log_order(order)

            order_id = order.get("id")
            if order.get("status") == "Awaiting Fulfillment":
                try:
                    metafields = list(client.get_order_metafields(order_id))
                    log_metafields(order_id, metafields)
                    created_metafield = client.ensure_pipe17_updated_metafield(order_id, metafields)
                    if created_metafield is not None:
                        orderList.append((order, created_metafield.get("id")))
                    else:
                        if client.does_need_pipe17_processing(metafields):
                            existing_metafield = client.find_pipe17_updated_metafield(metafields)
                            orderList.append((order, existing_metafield.get("id")))

                except requests.exceptions.HTTPError as exc:
                    log.error(f"Failed to sync metafields for order #{order_id}: {exc}")

            order_count += 1

        for order, metafield_id in orderList:
            order_id = order.get("id")
            try:
                shipments = shq_client.get_advanced_shipment(order_id)
                wwex_shipment = find_shipment_by_detail_name(shipments=shipments, name="World Wide Express 3PL")

                if wwex_shipment is not None:
                    # There should be only one shipment with wwex in order
                    carrier_code, method_code = get_carrier_code(shipment=wwex_shipment)
                    log.info(
                        f"Order #{order_id} | ShipperHQ advanced shipment: {shipments} "
                        f"Carrier Code: {carrier_code} Method Code: {method_code}"
                    )

                    pipe17_order_id = pipe17_client.get_order_id_by_ext_order_id(ext_order_id=order_id)
                    if pipe17_order_id is None:
                        log.error(f"pipe17_order_id can not be NULL" + f"Order #{order_id} | ShipperHQ advanced shipment: {shipments}")
                        continue
                        
                    wwex_carrier, wwex_carrier_code = map_to_wwex(carrier_code, method_code)
                    if wwex_carrier is None:
                        log.error(f"Order #{order_id} | skipping Pipe17 update; no WWEX mapping for {carrier_code}/{method_code}")
                        continue

                    updated = pipe17_client.update_order_custom_fields(
                        pipe17_order_id, carrier=wwex_carrier, carrier_code=wwex_carrier_code
                    )
                    if updated:
                        client.update_order_metafield(order_id, metafield_id, "updated")
                    else:
                        log.error(f"Order #{order_id} | Pipe17 update failed; will retry on next run.")
                    
                log.info(f"Order #{order_id} | ShipperHQ advanced shipment: {shipments}")
            except requests.exceptions.HTTPError as exc:
                log.error(f"Failed to fetch ShipperHQ advanced shipment for order #{order_id}: {exc}")

    except requests.exceptions.HTTPError as exc:
        log.critical(f"Aborting order sync due to unrecoverable API error: {exc}")
        sys.exit(1)
    except Exception as exc:
        log.critical(f"Unexpected error during order sync: {exc}")
        raise

    log.info(f"Order sync complete. Processed {order_count} orders modified in the last {days} day(s).")


if __name__ == "__main__":
    main()
