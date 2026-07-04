"""Tests para nickname_common.hubspot_client — cap de 10.000 resultados de Search API."""

from unittest.mock import patch

import pytest

from nickname_common.hubspot_client import HubSpotService


def _make_service():
    return HubSpotService(token="fake-token")


class TestSearchAllCapDetection:
    """Escenario del hallazgo: la Search API de HubSpot deja de paginar tras
    10.000 resultados totales (no devuelve mas 'paging.next.after'), aunque
    la busqueda real tenga mas registros. search_all debe detectar esto y
    advertir/exponerlo explicitamente en vez de devolver una lista 'completa'
    truncada en silencio.
    """

    def test_search_all_warns_when_hitting_10000_cap(self, caplog):
        service = _make_service()

        # 100 paginas de 100 resultados = exactamente 10.000 (el cap real de HubSpot).
        # La API deja de mandar 'paging.next.after' al llegar al cap, aunque
        # en realidad haya mas registros modificados en la ventana.
        pages = []
        for i in range(100):
            page = {"results": [{"id": str(i * 100 + j)} for j in range(100)]}
            if i < 99:
                page["paging"] = {"next": {"after": str((i + 1) * 100)}}
            pages.append(page)

        with patch.object(HubSpotService, "post", side_effect=pages):
            with caplog.at_level("WARNING"):
                results = service.search_all("deals", {"properties": ["dealname"]})

        assert len(results) == 10000
        assert any(
            "10000" in record.message or "10.000" in record.message
            for record in caplog.records
        ), "search_all debe loguear un warning explicito al tocar el cap de 10k"

    def test_search_all_no_warning_when_under_cap(self, caplog):
        """Caso normal (sin tocar el cap): no debe haber warning de truncado."""
        service = _make_service()

        pages = [
            {"results": [{"id": "1"}], "paging": {"next": {"after": "1"}}},
            {"results": [{"id": "2"}]},
        ]

        with patch.object(HubSpotService, "post", side_effect=pages):
            with caplog.at_level("WARNING"):
                results = service.search_all("deals", {"properties": ["dealname"]})

        assert len(results) == 2
        assert not any("10000" in record.message or "10.000" in record.message for record in caplog.records)
