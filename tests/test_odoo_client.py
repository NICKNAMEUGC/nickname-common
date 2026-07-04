"""Tests para nickname_common.odoo_client — circuit breaker vs. fallos de autenticación."""

from unittest.mock import MagicMock, patch

import pytest

from nickname_common.odoo_client import OdooService


def _make_service():
    return OdooService(
        url="https://fake-odoo.example.com",
        db="fakedb",
        username="fakeuser",
        api_key="fake-revoked-key",
        company_id=2,
    )


class TestAuthFailureDoesNotOpenCircuit:
    """Escenario del hallazgo: ODOO_API_KEY rotada/expirada -> authenticate()
    devuelve uid falsy -> NO debe tratarse como fallo de red/circuit breaker.
    """

    def test_execute_raises_authentication_error_without_opening_circuit(self):
        service = _make_service()

        fake_common = MagicMock()
        fake_common.authenticate.return_value = False  # credenciales invalidas

        with patch(
            "nickname_common.odoo_client._make_proxy", return_value=fake_common
        ):
            with pytest.raises(Exception) as exc_info:
                service.execute("res.partner", "search_read", [])

        # No debe degradar a "circuit breaker abierto" / ConnectionError generico:
        # el caller necesita distinguir esto de una caida de red.
        assert not isinstance(exc_info.value, ConnectionError)
        assert "autenticar" in str(exc_info.value).lower()

        # El circuito NO debe haberse abierto: is_available debe seguir en True,
        # de lo contrario cualquier retry (aunque la key ya este corregida)
        # seguiria bloqueado 60s fingiendo que Odoo esta caido.
        assert service.is_available is True
        assert service._circuit_open_until == 0.0

    def test_execute_with_context_raises_authentication_error_without_opening_circuit(self):
        service = _make_service()

        fake_common = MagicMock()
        fake_common.authenticate.return_value = None  # credenciales invalidas

        with patch(
            "nickname_common.odoo_client._make_proxy", return_value=fake_common
        ):
            with pytest.raises(Exception) as exc_info:
                service.execute_with_context("res.partner", "search_read", [[]])

        assert not isinstance(exc_info.value, ConnectionError)
        assert service.is_available is True
        assert service._circuit_open_until == 0.0

    def test_real_connection_error_still_opens_circuit(self):
        """Control: un fallo de RED real (no de auth) debe seguir abriendo el circuito."""
        service = _make_service()

        fake_common = MagicMock()
        fake_common.authenticate.side_effect = ConnectionError("timeout de red")

        with patch(
            "nickname_common.odoo_client._make_proxy", return_value=fake_common
        ):
            with pytest.raises(ConnectionError):
                service.execute("res.partner", "search_read", [])

        assert service.is_available is False
        assert service._circuit_open_until > 0.0
