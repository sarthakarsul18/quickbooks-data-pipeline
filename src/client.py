import logging
import time
from typing import Any, Dict, Generator, List, Optional
import requests

from src.auth import QuickBooksAuth, auth_manager
from src.config import settings

logger = logging.getLogger(__name__)

# Maximum records allowed per query page by QuickBooks Online
MAX_PAGE_SIZE = 1000


class QuickBooksClient:
    """Read-only client for querying QuickBooks Online Sandbox and Production APIs."""

    def __init__(self, auth: Optional[QuickBooksAuth] = None):
        self.auth = auth or auth_manager

    def _request_with_retry(
        self,
        endpoint: str,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        data: Optional[str] = None,
        content_type: str = "application/json",
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """Executes a request against the QuickBooks API with retry & token refresh handling.

        STRICT SAFETY ENFORCEMENT:
        Only read-only endpoints ('query' and 'cdc') are permitted.
        No record mutation endpoints (POST to entity resources, PUT, DELETE) are ever allowed.
        """
        clean_endpoint = endpoint.lstrip("/").lower()
        if not (clean_endpoint.startswith("query") or clean_endpoint.startswith("cdc")):
            raise ValueError(
                f"Safety Violation: Endpoint '{endpoint}' is not permitted. Only 'query' and 'cdc' are allowed."
            )

        realm_id = self.auth.get_realm_id()
        base_url = settings.qbo_base_url.rstrip("/")
        url = f"{base_url}/v3/company/{realm_id}/{endpoint.lstrip('/')}"

        # Standard query parameters including minorversion
        effective_params = dict(params or {})
        if "minorversion" not in effective_params:
            effective_params["minorversion"] = settings.QBO_MINOR_VERSION

        for attempt in range(1, max_retries + 1):
            token = self.auth.get_valid_access_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": content_type,
            }

            try:
                if method.upper() == "POST":
                    response = requests.post(
                        url,
                        headers=headers,
                        params=effective_params,
                        data=data,
                        timeout=45,
                    )
                else:
                    response = requests.get(
                        url,
                        headers=headers,
                        params=effective_params,
                        timeout=45,
                    )

                # If token expired during long sync, refresh and retry
                if response.status_code == 401:
                    logger.warning("Received 401 Unauthorized from QBO. Refreshing token...")
                    self.auth.refresh_access_token()
                    token = self.auth.get_valid_access_token()
                    headers["Authorization"] = f"Bearer {token}"
                    if method.upper() == "POST":
                        response = requests.post(
                            url,
                            headers=headers,
                            params=effective_params,
                            data=data,
                            timeout=45,
                        )
                    else:
                        response = requests.get(
                            url,
                            headers=headers,
                            params=effective_params,
                            timeout=45,
                        )

                # Handle rate limiting (HTTP 429)
                if response.status_code == 429:
                    sleep_time = attempt * 5
                    logger.warning(f"Rate limited (429) by QuickBooks API. Retrying in {sleep_time}s...")
                    time.sleep(sleep_time)
                    continue

                if response.status_code != 200:
                    logger.error(
                        f"QuickBooks API Error: HTTP {response.status_code} on {endpoint} - {response.text}"
                    )
                    response.raise_for_status()

                data = response.json()
                # Check for QuickBooks API Fault payload even when HTTP status is 200
                if isinstance(data, dict) and "Fault" in data:
                    fault = data["Fault"]
                    errors = fault.get("Error", []) if isinstance(fault, dict) else []
                    error_details = []
                    for err in errors:
                        msg = err.get("Message", "")
                        detail = err.get("Detail", "")
                        code = err.get("code", "")
                        error_details.append(f"Code {code}: {msg} - {detail}".strip(" -"))
                    fault_summary = "; ".join(error_details) if error_details else str(fault)
                    error_msg = f"QuickBooks API Fault received on {endpoint}: {fault_summary}"
                    logger.error(error_msg)
                    raise RuntimeError(error_msg)

                return data

            except requests.exceptions.RequestException as e:
                logger.warning(f"HTTP request failed on attempt {attempt}/{max_retries}: {e}")
                if attempt == max_retries:
                    raise
                time.sleep(attempt * 2)

        raise RuntimeError(f"Failed to fetch data from {url} after {max_retries} attempts.")

    def query(self, entity_name: str, where_clause: Optional[str] = None) -> Generator[Dict[str, Any], None, None]:
        """Paginates through records for a given entity using the QuickBooks Query language via POST.

        Consistent with the verified Postman request:
            POST /v3/company/{realm_id}/query?minorversion=75
            Headers:
                Authorization: Bearer <access_token>
                Accept: application/json
                Content-Type: application/text
            Body:
                SELECT * FROM <Entity> STARTPOSITION <x> MAXRESULTS <y>
        """
        start_position = 1
        page_size = MAX_PAGE_SIZE

        while True:
            # Construct QuickBooks SQL query
            # Example: SELECT * FROM Bill STARTPOSITION 1 MAXRESULTS 1000
            query_str = f"SELECT * FROM {entity_name}"
            if where_clause:
                query_str += f" WHERE {where_clause}"
            query_str += f" STARTPOSITION {start_position} MAXRESULTS {page_size}"

            logger.info(f"Executing QBO Query (POST): {query_str}")
            data = self._request_with_retry(
                endpoint="query",
                method="POST",
                data=query_str,
                content_type="application/text",
            )

            query_response = data.get("QueryResponse", {})
            records: List[Dict[str, Any]] = query_response.get(entity_name, [])

            if not records:
                logger.info(f"No more records found for entity: {entity_name}")
                break

            for record in records:
                yield record

            count = len(records)
            logger.info(f"Fetched page of {count} records for {entity_name} (position {start_position}).")

            if count < page_size:
                # Last page reached
                break

            start_position += count

    def get_cdc(self, entities: List[str], changed_since: str) -> Dict[str, List[Dict[str, Any]]]:
        """Queries the QuickBooks Change Data Capture (CDC) API.

        Endpoint: GET /v3/company/<realmId>/cdc?entities=<list>&changedSince=<iso_timestamp>&minorversion=75
        Headers:
            Authorization: Bearer <access_token>
            Accept: application/json
            Content-Type: application/json

        CDC Specifications & Limitations:
        - HTTP Method: GET
        - changed_since must be within the last 30 days.
        - Parameters: 'entities' (comma-separated), 'changedSince' (ISO timestamp), 'minorversion'.
        - Response structure: {"CDCResponse": [{"QueryResponse": [{"EntityName": [...]}]}]}
        - Deletion behavior: Officially, deleted transactions include status="Deleted". Master entities
          (Account, Customer, Vendor) are typically soft-deleted via Active=False.
        - Important Note: Deletion handling should not be assumed fully verified until validated
          against actual CDC events in the live sandbox company.

        Returns a dictionary mapping entity name to list of changed/deleted records.
        """
        entities_str = ",".join(entities)
        params = {
            "entities": entities_str,
            "changedSince": changed_since,
        }

        logger.info(f"Executing CDC query (GET) for entities [{entities_str}] since {changed_since}...")
        data = self._request_with_retry(
            endpoint="cdc",
            method="GET",
            params=params,
            content_type="application/json",
        )

        results: Dict[str, List[Dict[str, Any]]] = {entity: [] for entity in entities}
        cdc_responses = data.get("CDCResponse", [])

        if isinstance(cdc_responses, dict):
            cdc_responses = [cdc_responses]

        for cdc_resp in cdc_responses:
            query_responses = cdc_resp.get("QueryResponse", [])
            if isinstance(query_responses, dict):
                query_responses = [query_responses]

            for query_resp in query_responses:
                if isinstance(query_resp, dict):
                    for entity in entities:
                        items = query_resp.get(entity, [])
                        if isinstance(items, dict):
                            items = [items]
                        if items:
                            results[entity].extend(items)

        for entity, items in results.items():
            logger.info(f"CDC found {len(items)} changed/deleted records for {entity}.")

        return results
