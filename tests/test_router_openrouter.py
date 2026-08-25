"""El enrutador: un unico punto de control de gasto, sin sorpresas.

Contratado el 2026-08-25 tras la auditoria que encontro 262 EUR de 300 en un
solo agente. El objetivo del enrutador no es abaratar por token: es que el gasto
tenga UN sitio donde verse y capearse, y poder cambiar de modelo sin tocar repos.
"""

import json

import pytest

from nickname_common import llm


@pytest.fixture(autouse=True)
def _entorno_limpio(monkeypatch):
    for var in (llm.ROUTER_ENV, llm.ROUTER_KEY_ENV, llm.ROUTER_AGENT_ENV,
                llm.XAI_KEY_ENV, llm.XAI_LIVE_ENV):
        monkeypatch.delenv(var, raising=False)


def _respuesta(contenido="ok", usage=None):
    capturado = {}

    def _post(url, headers, body, timeout):
        capturado["url"] = url
        capturado["headers"] = headers
        capturado["body"] = body
        return 200, json.dumps({
            "choices": [{"message": {"content": contenido}}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    return _post, capturado


class TestFailSafe:
    def test_por_defecto_no_enruta(self):
        assert llm.router_enabled() is False, "encender el enrutador tiene que ser explicito"

    def test_una_var_cualquiera_no_enciende_el_enrutador(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_ENV, "true")
        assert llm.router_enabled() is False, "solo el valor 'openrouter' enruta"

    def test_enrutador_sin_key_falla_en_vez_de_ir_por_detras(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
        with pytest.raises(llm.RouterNotAvailable) as exc:
            llm.complete("gemini_flash", [{"role": "user", "content": "hola"}])
        assert llm.ROUTER_KEY_ENV in str(exc.value)
        assert "a medias" in str(exc.value), (
            "enrutar a medias deja gasto fuera del unico punto de control"
        )


class TestEnrutado:
    def test_gemini_pasa_por_openrouter_conservando_familia(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-test")
        post, cap = _respuesta()

        res = llm.complete("gemini_pro", [{"role": "user", "content": "hola"}], http_post=post)

        assert cap["url"] == llm.OPENROUTER_CHAT_URL
        assert cap["body"]["model"] == "google/gemini-2.5-pro", (
            "enrutar no puede cambiar de modelo a escondidas"
        )
        assert res.provider == "openrouter:gemini"

    def test_el_agente_viaja_para_poder_desglosar_el_gasto(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-test")
        monkeypatch.setenv(llm.ROUTER_AGENT_ENV, "nickname-ig-agent")
        post, cap = _respuesta()

        llm.complete("gemini_flash", [{"role": "user", "content": "x"}], http_post=post)

        assert cap["headers"]["X-Title"] == "nickname-ig-agent"

    def test_sin_nombre_de_agente_se_marca_en_vez_de_mentir(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-test")
        post, cap = _respuesta()

        llm.complete("gemini_flash", [{"role": "user", "content": "x"}], http_post=post)

        assert cap["headers"]["X-Title"] == "nickname-sin-identificar"

    def test_se_pide_el_coste_no_solo_los_tokens(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-test")
        post, cap = _respuesta()

        llm.complete("gemini_flash", [{"role": "user", "content": "x"}], http_post=post)

        assert cap["body"]["usage"] == {"include": True}

    def test_el_coste_devuelto_llega_a_usage(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-test")
        post, _ = _respuesta(usage={"prompt_tokens": 1, "completion_tokens": 1,
                                    "total_tokens": 2, "cost": 0.0123})

        res = llm.complete("gemini_flash", [{"role": "user", "content": "x"}], http_post=post)

        assert res.usage["cost_micros"] == 12300

    def test_json_schema_sigue_funcionando_enrutado(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-test")
        post, cap = _respuesta(contenido='{"ok": true}')

        res = llm.complete(
            "gemini_flash", [{"role": "user", "content": "x"}],
            json_schema={"type": "object"}, http_post=post,
        )

        assert cap["body"]["response_format"]["type"] == "json_schema"
        assert res.parsed_json == {"ok": True}


class TestLoQueNoSeEnruta:
    def test_la_generacion_de_imagen_no_se_cuela_por_el_chat(self):
        with pytest.raises(llm.RouterNotAvailable) as exc:
            llm.router_slug("imagen")
        assert "paridad" in str(exc.value), (
            "colar imagen por un API de chat romperia el carrusel en silencio"
        )

    def test_imagen_enrutada_falla_en_lugar_de_devolver_basura(self, monkeypatch):
        monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-test")
        with pytest.raises(llm.RouterNotAvailable):
            llm.complete("imagen", [{"role": "user", "content": "x"}])


class TestSinEnrutador:
    def test_el_camino_xai_de_siempre_sigue_intacto(self, monkeypatch):
        monkeypatch.setenv(llm.XAI_KEY_ENV, "xai-test")
        monkeypatch.setenv(llm.XAI_LIVE_ENV, "1")
        post, cap = _respuesta()

        res = llm.complete("grok_fast", [{"role": "user", "content": "x"}], http_post=post)

        assert cap["url"] == llm.XAI_CHAT_URL
        assert res.provider == "xai"

    def test_gemini_sin_enrutador_sigue_diciendo_que_use_su_sdk(self):
        with pytest.raises(NotImplementedError) as exc:
            llm.complete("gemini_flash", [{"role": "user", "content": "x"}])
        assert llm.ROUTER_ENV in str(exc.value), "el error debe decir como enrutarlo"
