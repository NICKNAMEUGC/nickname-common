"""
HubSpotService base — Cliente REST para HubSpot CRM con rate limiting.

Extraído de nickname-odoo-agent/src/services/hubspot_service.py.

Uso:
    from nickname_common.hubspot_client import HubSpotService
    hs = HubSpotService()  # Lee HUBSPOT_ACCESS_TOKEN de env
    deals = hs.get('/crm/v3/objects/deals?limit=10')

Portal: 147084424 (EU region)
"""

import json
import os
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

from nickname_common.logging import setup_logger

log = setup_logger("hubspot-client")


class HubSpotService:
    """Cliente REST para HubSpot CRM con retry automático en rate limit (429)."""

    BASE_URL = "https://api.hubapi.com"

    def __init__(self, token: str = None, max_retries: int = 3):
        self.token = token or os.getenv("HUBSPOT_ACCESS_TOKEN")
        if not self.token:
            raise ValueError("HUBSPOT_ACCESS_TOKEN no configurado")
        self.max_retries = max_retries

    def _request(self, method: str, path: str, body: dict = None,
                 max_retries: int = None) -> dict:
        """Ejecuta un request HTTP a HubSpot con retry en rate limit."""
        url = f"{self.BASE_URL}{path}"
        retries = max_retries or self.max_retries
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode() if body else None

        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < retries - 1:
                    wait = 10 * (attempt + 1)
                    log.warning(
                        f"Rate limited (429), esperando {wait}s antes de retry "
                        f"{attempt + 2}/{retries}"
                    )
                    time.sleep(wait)
                    continue
                raise

    def get(self, path: str) -> dict:
        """GET request a HubSpot API."""
        return self._request("GET", path)

    def post(self, path: str, body: dict) -> dict:
        """POST request a HubSpot API."""
        return self._request("POST", path, body)

    def patch(self, path: str, body: dict) -> dict:
        """PATCH request a HubSpot API."""
        return self._request("PATCH", path, body)

    def delete(self, path: str) -> dict:
        """DELETE request a HubSpot API."""
        return self._request("DELETE", path)

    # --- Paginación ---

    def search_all(self, object_type: str, body: dict) -> List[Dict]:
        """Busca todos los registros de un tipo con paginación automática.

        Args:
            object_type: 'deals', 'companies', 'contacts', etc.
            body: Body del search (properties, filterGroups, sorts, etc.)
                  El campo 'after' se gestiona automáticamente.

        Returns:
            Lista completa de resultados.
        """
        all_results = []
        after = None

        while True:
            request_body = dict(body)
            if after:
                request_body["after"] = after

            result = self.post(f"/crm/v3/objects/{object_type}/search", request_body)
            all_results.extend(result.get("results", []))

            paging = result.get("paging", {})
            after = paging.get("next", {}).get("after")
            if not after:
                break

        return all_results

    def search_modified(self, object_type: str, since_ms: int,
                        properties: List[str], limit: int = 100) -> List[Dict]:
        """Busca registros modificados desde un timestamp (para sync incremental).

        Args:
            object_type: 'deals', 'companies', 'contacts', etc.
            since_ms: Epoch timestamp en milisegundos (0 = fetch all)
            properties: Lista de propiedades a devolver
            limit: Batch size por request (max 100)
        """
        props = list(properties)
        if "hs_lastmodifieddate" not in props:
            props.append("hs_lastmodifieddate")

        body = {
            "properties": props,
            "limit": min(limit, 100),
            "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
        }

        if since_ms > 0:
            body["filterGroups"] = [{
                "filters": [{
                    "propertyName": "hs_lastmodifieddate",
                    "operator": "GTE",
                    "value": str(since_ms),
                }]
            }]

        return self.search_all(object_type, body)

    def get_associations(self, from_type: str, to_type: str,
                         object_ids: List[str]) -> Dict[str, List[str]]:
        """Asociaciones entre objetos en batch.

        Returns:
            Dict mapping source_id → [target_id, ...]
        """
        associations = {}

        for batch_start in range(0, len(object_ids), 100):
            batch = object_ids[batch_start:batch_start + 100]
            inputs = [{"id": str(i)} for i in batch]

            try:
                result = self.post(
                    f"/crm/v4/associations/{from_type}/{to_type}/batch/read",
                    {"inputs": inputs},
                )
                for item in result.get("results", []):
                    from_id = item["from"]["id"]
                    to_ids = [t["toObjectId"] for t in item.get("to", [])]
                    if to_ids:
                        associations[from_id] = to_ids
            except Exception as e:
                log.warning(
                    f"Error obteniendo asociaciones {from_type}→{to_type} batch: {e}"
                )

        return associations

    def test_connection(self) -> Dict:
        """Test de conexión a HubSpot. Devuelve status dict."""
        try:
            t0 = time.time()
            result = self.get("/crm/v3/objects/deals?limit=1")
            latency = round((time.time() - t0) * 1000)
            return {
                "status": "connected",
                "latency_ms": latency,
                "detail": f"{len(result.get('results', []))} deals returned",
            }
        except Exception as e:
            return {"status": "error", "error": str(e)[:200]}
