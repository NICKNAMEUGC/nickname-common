"""Cascada de fallback multi-proveedor (mandato Diego 2026-08-31).

Referencia canónica: `.ag/decisions_log.md` → LLM-FALLBACK-OPENROUTER-MANDATORY.
Ningún pipeline puede quedar parado por el fallo de un solo transporte LLM:
con NK_ROUTER=openrouter la cascada es

    openrouter → gemini_directo (solo tiers gemini) → openai → anthropic

Todo con http_post inyectado: cero red. Los tests verifican además que ningún
log ni excepción arrastra contenido (prompts/respuestas).
"""

import json
import logging

import pytest

from nickname_common import llm


@pytest.fixture(autouse=True)
def _entorno_limpio(monkeypatch):
    for var in (llm.ROUTER_ENV, llm.ROUTER_KEY_ENV, llm.ROUTER_AGENT_ENV,
                llm.XAI_KEY_ENV, llm.XAI_LIVE_ENV,
                llm.GEMINI_KEY_ENV, llm.GEMINI_KEY_FALLBACK_ENV,
                llm.OPENAI_KEY_ENV, llm.ANTHROPIC_KEY_ENV,
                llm.OPENAI_FALLBACK_MODEL_ENV, llm.ANTHROPIC_FALLBACK_MODEL_ENV):
        monkeypatch.delenv(var, raising=False)


def _enrutado(monkeypatch, con_key=True):
    monkeypatch.setenv(llm.ROUTER_ENV, "openrouter")
    if con_key:
        monkeypatch.setenv(llm.ROUTER_KEY_ENV, "sk-or-test")


class Transporte:
    """http_post falso enrutado por substring de URL. Registra cada llamada."""

    def __init__(self, plan):
        self.plan = plan  # substring_url -> (status, payload_dict) | Exception
        self.llamadas = []

    def __call__(self, url, headers, body, timeout):
        self.llamadas.append(
            {"url": url, "headers": dict(headers), "body": body, "timeout": timeout}
        )
        for clave, resultado in self.plan.items():
            if clave in url:
                if isinstance(resultado, Exception):
                    raise resultado
                status, payload = resultado
                return status, json.dumps(payload)
        raise AssertionError(f"URL inesperada en el test: {url}")


def _chat_ok(contenido, usage=None):
    """Respuesta estilo OpenAI/OpenRouter."""
    return {
        "choices": [{"message": {"content": contenido}}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5,
                           "total_tokens": 15},
    }


def _gemini_ok(contenido):
    return {
        "candidates": [{"content": {"parts": [{"text": contenido}]}}],
        "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3,
                          "totalTokenCount": 10},
    }


def _anthropic_ok(contenido):
    return {
        "content": [{"type": "text", "text": contenido}],
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }


_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


class TestLegGeminiDirecto:
    def test_openrouter_falla_y_gemini_directo_responde(self, monkeypatch):
        _enrutado(monkeypatch)
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-key-test")
        post = Transporte({
            "openrouter.ai": (500, {"error": "boom"}),
            "generativelanguage.googleapis.com": (200, _gemini_ok('{"ok": true}')),
        })

        res = llm.complete(
            "gemini_flash",
            [{"role": "user", "content": "hola"},
             {"role": "assistant", "content": "contexto previo"},
             {"role": "user", "content": "sigue"}],
            json_schema=_SCHEMA,
            http_post=post,
        )

        assert len(post.llamadas) == 2
        # URL del leg directo: modelo del registro + key en query.
        assert post.llamadas[1]["url"] == (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash:generateContent?key=g-key-test"
        )
        cuerpo = post.llamadas[1]["body"]
        # Conversión de messages: user→user, assistant→model.
        assert [c["role"] for c in cuerpo["contents"]] == ["user", "model", "user"]
        assert cuerpo["contents"][1]["parts"] == [{"text": "contexto previo"}]
        cfg = cuerpo["generationConfig"]
        assert cfg["responseMimeType"] == "application/json"
        # El schema viaja limpio: additionalProperties no lo soporta Gemini REST.
        assert "additionalProperties" not in json.dumps(cfg["responseSchema"])
        assert cfg["responseSchema"]["properties"]["ok"]["type"] == "boolean"
        # Tier flash: thinking_budget=0 (L-014, protege max_output_tokens).
        assert cfg["thinkingConfig"] == {"thinkingBudget": 0}
        assert res.provider == "google-direct"
        assert res.model == "gemini-2.5-flash"
        assert res.parsed_json == {"ok": True}
        assert res.usage == {"prompt_tokens": 7, "completion_tokens": 3,
                             "total_tokens": 10}

    def test_openrouter_sin_key_cae_a_gemini_con_warning(self, monkeypatch, caplog):
        """Antes: RouterNotAvailable. Mandato nuevo: la anomalía de config se
        loguea (sin contenido) y la cascada sigue por el leg directo."""
        _enrutado(monkeypatch, con_key=False)
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-key-test")
        post = Transporte({
            "generativelanguage.googleapis.com": (200, _gemini_ok("bien")),
        })

        with caplog.at_level(logging.WARNING, logger="nickname_common.llm"):
            res = llm.complete(
                "gemini_flash",
                [{"role": "user", "content": "texto-privado-del-pipeline"}],
                http_post=post,
            )

        assert res.provider == "google-direct"
        assert len(post.llamadas) == 1, "sin key no se llama a OpenRouter"
        assert llm.ROUTER_KEY_ENV in caplog.text, "la anomalía de config se registra"
        assert "texto-privado-del-pipeline" not in caplog.text, (
            "los logs de la cascada jamás llevan contenido"
        )

    def test_gemini_pro_no_manda_thinking_budget_cero(self, monkeypatch):
        """GOTCHA L-014: gemini-2.5-pro rechaza thinking_budget=0 con 400."""
        _enrutado(monkeypatch)
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-key-test")
        post = Transporte({
            "openrouter.ai": (429, {"error": "cap"}),
            "generativelanguage.googleapis.com": (200, _gemini_ok("ok")),
        })

        res = llm.complete(
            "gemini_pro", [{"role": "user", "content": "x"}], http_post=post
        )

        assert "gemini-2.5-pro:generateContent" in post.llamadas[1]["url"]
        assert "thinkingConfig" not in post.llamadas[1]["body"]["generationConfig"]
        assert res.provider == "google-direct"

    def test_acepta_el_nombre_alternativo_google_ai_api_key(self, monkeypatch):
        _enrutado(monkeypatch)
        monkeypatch.setenv(llm.GEMINI_KEY_FALLBACK_ENV, "goog-alt")
        post = Transporte({
            "openrouter.ai": (500, {}),
            "generativelanguage.googleapis.com": (200, _gemini_ok("ok")),
        })

        llm.complete("gemini_flash", [{"role": "user", "content": "x"}],
                     http_post=post)

        assert post.llamadas[1]["url"].endswith("?key=goog-alt")

    def test_tier_no_gemini_no_pasa_por_el_leg_google(self, monkeypatch):
        """El leg gemini_directo es SOLO para tiers de familia gemini."""
        _enrutado(monkeypatch)
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-key-test")
        post = Transporte({"openrouter.ai": (500, {})})

        with pytest.raises(llm.CascadeExhausted) as exc:
            llm.complete("claude_sonnet", [{"role": "user", "content": "x"}],
                         http_post=post)

        assert "google-direct(no-aplica)" in str(exc.value)
        assert len(post.llamadas) == 1, "con key de Gemini y todo, no aplica"


class TestLegsOpenaiAnthropic:
    def test_leg_openai_etiqueta_modelo_y_schema(self, monkeypatch):
        _enrutado(monkeypatch)
        monkeypatch.setenv(llm.OPENAI_KEY_ENV, "oa-test")
        post = Transporte({
            "openrouter.ai": (502, {}),
            "api.openai.com": (200, _chat_ok('{"ok": false}')),
        })

        res = llm.complete(
            "gemini_flash", [{"role": "user", "content": "x"}],
            json_schema=_SCHEMA, http_post=post,
        )

        llamada = post.llamadas[1]
        assert llamada["url"] == "https://api.openai.com/v1/chat/completions"
        assert llamada["headers"]["Authorization"] == "Bearer oa-test"
        assert llamada["body"]["response_format"]["type"] == "json_schema"
        assert res.provider == "openai-fallback"
        assert res.model == "gpt-4o-mini", "default de NK_MODEL_OPENAI_FALLBACK"
        assert res.parsed_json == {"ok": False}
        assert res.usage["total_tokens"] == 15

    def test_modelo_openai_por_env(self, monkeypatch):
        _enrutado(monkeypatch)
        monkeypatch.setenv(llm.OPENAI_KEY_ENV, "oa-test")
        monkeypatch.setenv(llm.OPENAI_FALLBACK_MODEL_ENV, "gpt-5-mini")
        post = Transporte({
            "openrouter.ai": (502, {}),
            "api.openai.com": (200, _chat_ok("ok")),
        })

        res = llm.complete("gemini_flash", [{"role": "user", "content": "x"}],
                           http_post=post)

        assert post.llamadas[1]["body"]["model"] == "gpt-5-mini"
        assert res.model == "gpt-5-mini"

    def test_leg_anthropic_headers_max_tokens_y_schema(self, monkeypatch):
        """Sin credencial de Gemini ni OpenAI se saltan en silencio esos legs
        y responde Anthropic con sus headers y max_tokens obligatorio."""
        _enrutado(monkeypatch)
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "ak-test")
        post = Transporte({
            "openrouter.ai": (503, {}),
            "api.anthropic.com": (200, _anthropic_ok('{"ok": true}')),
        })

        res = llm.complete(
            "gemini_flash",
            [{"role": "system", "content": "eres breve"},
             {"role": "user", "content": "x"}],
            json_schema=_SCHEMA, max_tokens=64, http_post=post,
        )

        assert len(post.llamadas) == 2, "gemini/openai sin credencial se saltan"
        llamada = post.llamadas[1]
        assert llamada["url"] == "https://api.anthropic.com/v1/messages"
        assert llamada["headers"]["x-api-key"] == "ak-test"
        assert llamada["headers"]["anthropic-version"] == "2023-06-01"
        cuerpo = llamada["body"]
        assert cuerpo["max_tokens"] == 64, "obligatorio en la API Messages"
        # Sin response_format nativo: el schema viaja como instrucción de sistema.
        assert "JSON Schema" in cuerpo["system"]
        assert "eres breve" in cuerpo["system"]
        assert all(m["role"] != "system" for m in cuerpo["messages"])
        assert res.provider == "anthropic-fallback"
        assert res.model == "claude-haiku-4-5-20251001"
        assert res.parsed_json == {"ok": True}
        assert res.usage == {"prompt_tokens": 4, "completion_tokens": 2,
                             "total_tokens": 6}


class TestReglasDeLaCascada:
    def test_todos_los_legs_fallan_resume_la_cadena_sin_contenido(self, monkeypatch):
        _enrutado(monkeypatch)
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g")
        monkeypatch.setenv(llm.OPENAI_KEY_ENV, "o")
        monkeypatch.setenv(llm.ANTHROPIC_KEY_ENV, "a")
        post = Transporte({
            "openrouter.ai": (429, {"error": "cuerpo-que-no-debe-filtrarse"}),
            "generativelanguage.googleapis.com": TimeoutError("lento"),
            "api.openai.com": (500, {"error": "cuerpo-que-no-debe-filtrarse"}),
            "api.anthropic.com": (529, {"error": "cuerpo-que-no-debe-filtrarse"}),
        })

        with pytest.raises(llm.CascadeExhausted) as exc:
            llm.complete("gemini_flash", [{"role": "user", "content": "x"}],
                         http_post=post)

        mensaje = str(exc.value)
        assert "openrouter:google(HTTP 429)" in mensaje
        assert "google-direct(TimeoutError)" in mensaje
        assert "openai-fallback(HTTP 500)" in mensaje
        assert "anthropic-fallback(HTTP 529)" in mensaje
        assert "cuerpo-que-no-debe-filtrarse" not in mensaje
        assert len(post.llamadas) == 4

    def test_200_con_json_invalido_no_encadena(self, monkeypatch):
        """Regla dura: solo errores de TRANSPORTE caen al siguiente leg. Un 200
        cuyo contenido no parsea se devuelve como hoy (parsed_json=None)."""
        _enrutado(monkeypatch)
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-key-test")  # fallback disponible
        post = Transporte({
            "openrouter.ai": (200, _chat_ok("esto no es json")),
        })

        res = llm.complete(
            "gemini_flash", [{"role": "user", "content": "x"}],
            json_schema=_SCHEMA, http_post=post,
        )

        assert len(post.llamadas) == 1, "no se relitiga en otro proveedor"
        assert res.provider == "openrouter:google"
        assert res.text == "esto no es json"
        assert res.parsed_json is None

    def test_excepcion_de_red_encadena(self, monkeypatch):
        _enrutado(monkeypatch)
        monkeypatch.setenv(llm.GEMINI_KEY_ENV, "g-key-test")
        post = Transporte({
            "openrouter.ai": ConnectionResetError("reset"),
            "generativelanguage.googleapis.com": (200, _gemini_ok("ok")),
        })

        res = llm.complete("gemini_flash", [{"role": "user", "content": "x"}],
                           http_post=post)

        assert res.provider == "google-direct"
        assert len(post.llamadas) == 2

    def test_el_camino_no_enrutado_no_cambia(self):
        """Sin NK_ROUTER los tiers gemini siguen diciendo que usen su SDK."""
        with pytest.raises(NotImplementedError):
            llm.complete("gemini_flash", [{"role": "user", "content": "x"}])
