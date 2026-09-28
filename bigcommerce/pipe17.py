"""Pipe17 REST API client.

Credentials are read from environment variables (see .env.example):
    PIPE17_API_KEY   - Pipe17 API key (X-Pipe17-Key)
    PIPE17_API_BASE  - optional, defaults to https://api-v3.pipe17.com/api/v3
"""

import os
import time

import requests
from dotenv import load_dotenv

from logger import AppLogger

load_dotenv()

log = AppLogger(__name__)

PIPE17_API_BASE_DEFAULT = "https://api-v3.pipe17.com/api/v3"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3


class Pipe17Client:
    def __init__(self, api_key=None, api_base=None):
        self.api_key = api_key or os.getenv("PIPE17_API_KEY")
        self.api_base = (api_base or os.getenv("PIPE17_API_BASE", PIPE17_API_BASE_DEFAULT)).rstrip("/")

        if not self.api_key:
            log.critical("Missing Pipe17 credentials (PIPE17_API_KEY env var).")
            raise ValueError("Pipe17 API key is required.")

        self.session = requests.Session()
        self.session.headers.update({
            "X-Pipe17-Key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

        log.debug("Initialized Pipe17 client")

    def _request(self, path, params=None, method="GET", json_body=None):
        url = f"{self.api_base}/{path.lstrip('/')}"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.request(
                    method, url, params=params, json=json_body, timeout=REQUEST_TIMEOUT
                )
            except requests.exceptions.RequestException as exc:
                log.error(f"Network error calling Pipe17 API (attempt {attempt}/{MAX_RETRIES}): {exc}")
                if attempt == MAX_RETRIES:
                    raise
                time.sleep(2 ** attempt)
                continue

            if response.status_code == 429:
                log.warning(f"Rate limited by Pipe17 API (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(2 ** attempt)
                continue

            if response.status_code in (401, 403):
                log.critical(f"Pipe17 authentication failed ({response.status_code}). Check PIPE17_API_KEY.")
                response.raise_for_status()

            if not response.ok:
                log.error(f"Pipe17 API returned {response.status_code} for {url}: {response.text[:500]}")
                response.raise_for_status()

            if response.status_code == 204 or not response.content:
                return {}

            payload = response.json()
            if not payload.get("success", True):
                log.error(f"Pipe17 API reported failure for {url}: {payload}")
            return payload

        raise RuntimeError("Exceeded max retries calling Pipe17 API.")

    def get_orders_by_ext_order_id(self, ext_order_id):
        """Return all Pipe17 orders whose extOrderId matches `ext_order_id` (e.g. the BigCommerce order id)."""
        orders = []
        page_index = 1

        while True:
            params = {"extOrderId": str(ext_order_id)}
            payload = self._request("orders", params)
            orders.extend(payload.get("orders") or [])

            pagination = payload.get("pagination") or {}
            if pagination.get("last", True) or page_index >= pagination.get("pages", page_index):
                break
            page_index += 1

        log.info(f"Order #{ext_order_id} | found {len(orders)} Pipe17 order(s)")
        return orders

    def get_order_by_ext_order_id(self, ext_order_id):
        """Return the single Pipe17 order for `ext_order_id`, or None if Pipe17 has no such order."""
        orders = self.get_orders_by_ext_order_id(ext_order_id)
        if len(orders) > 1:
            log.warning(f"Order #{ext_order_id} | multiple Pipe17 orders matched; using the first one.")
        return orders[0] if orders else None

    def get_order_id_by_ext_order_id(self, ext_order_id):
        """Return Pipe17's internal "orderId" for `ext_order_id`, or None if Pipe17 has no such order."""
        order = self.get_order_by_ext_order_id(ext_order_id)
        if not order:
            log.warning(f"Order #{ext_order_id} | no matching Pipe17 order found.")
            return None

        order_id = order.get("orderId")
        if not order_id:
            log.error(f"Order #{ext_order_id} | Pipe17 order is missing 'orderId': {order}")
            return None

        log.debug(f"Order #{ext_order_id} | Pipe17 orderId={order_id}")
        return order_id

    def update_order_custom_fields(self, order_id, carrier, carrier_code):
        """Set the "Carrier" and "Code" custom fields on the Pipe17 order with internal id `order_id`.

        Returns True if Pipe17 reports the update succeeded, False otherwise.
        """
        body = {
            "customFields": [
                {"name": "Carrier", "value": carrier},
                {"name": "Code", "value": carrier_code},
            ]
        }
        try:
            payload = self._request(f"orders/{order_id}", method="PUT", json_body=body)
        except requests.exceptions.RequestException as exc:
            log.error(f"Pipe17 order {order_id} | failed to update customFields: {exc}")
            return False

        # An empty body (e.g. 204) means the PUT was accepted; otherwise trust the "success" flag.
        success = payload.get("success", True) is True
        if success:
            log.info(f"Pipe17 order {order_id} | updated customFields Carrier='{carrier}' Code='{carrier_code}'")
        else:
            log.error(
                f"Pipe17 order {order_id} | customFields update rejected: "
                f"code={payload.get('code')} message='{payload.get('message')}'"
            )
        return success
