"""Tests de AutomationRunV1 / LLMUsageEventV1 (WI-1, SPEC §5).

El foco de estos tests es el invariante duro que la SPEC repite como criterio
global: "ausencia de métricas es null, nunca cero inventado". Aquí eso se
traduce en que el propio dataclass RECHAZA construirse en un estado
inconsistente (cost_source vs cost_micros), en vez de confiar en que cada
consumidor recuerde la regla.
"""

import pytest

from nickname_common.models import (
    AutomationRunV1,
    CostSource,
    LLMOutcome,
    LLMUsageEventV1,
    RunStatus,
    SourceStatus,
)


class TestAutomationRunV1:
    def _base(self, **overrides):
        kwargs = dict(
            job_id="roomservice_feedback_daily",
            run_id="11111111-1111-1111-1111-111111111111",
            idempotency_key="feedback:2026-09-10",
        )
        kwargs.update(overrides)
        return AutomationRunV1(**kwargs)

    def test_defaults(self):
        run = self._base()
        assert run.status is RunStatus.OK
        assert run.source_status is SourceStatus.OK
        assert run.changed is False
        assert run.llm_required is False
        assert run.llm_calls == 0
        assert run.schema_version == 1
        assert run.started_at  # se autorrellena
        assert run.completed_at == ""

    def test_enum_coercion_from_string(self):
        run = self._base(status="degraded", source_status="partial")
        assert run.status is RunStatus.DEGRADED
        assert run.source_status is SourceStatus.PARTIAL

    def test_mark_completed_sets_timestamp(self):
        run = self._base()
        assert run.completed_at == ""
        run.mark_completed()
        assert run.completed_at != ""

    def test_rejects_negative_llm_calls(self):
        with pytest.raises(ValueError):
            self._base(llm_calls=-1)

    def test_rejects_llm_calls_without_llm_required(self):
        with pytest.raises(ValueError):
            self._base(llm_required=False, llm_calls=1)

    def test_llm_calls_with_llm_required_ok(self):
        run = self._base(llm_required=True, llm_calls=1)
        assert run.llm_calls == 1

    def test_to_dict_shape_matches_spec(self):
        run = self._base(
            source_status=SourceStatus.OK,
            changed=True,
            llm_required=True,
            llm_calls=1,
            result_ref="report:2026-09-10",
        )
        run.mark_completed()
        d = run.to_dict()
        assert set(d) == {
            "schema_version", "job_id", "run_id", "idempotency_key",
            "started_at", "completed_at", "status", "source_status",
            "changed", "llm_required", "llm_calls", "result_ref", "error_code",
        }
        assert d["status"] == "ok"
        assert d["source_status"] == "ok"
        assert d["result_ref"] == "report:2026-09-10"
        assert d["error_code"] is None

    def test_zero_respuestas_es_ok_no_failed(self):
        """Caso explícito de la SPEC (§6.4): cero respuestas nuevas es un
        resultado OK con changed=False y llm_required=False — nunca 'failed'."""
        run = self._base(
            source_status=SourceStatus.OK, changed=False, llm_required=False
        )
        assert run.status is RunStatus.OK
        assert run.llm_calls == 0


class TestLLMUsageEventV1:
    def _base(self, **overrides):
        kwargs = dict(
            service="nickname-roomservice-feedback",
            job_id="roomservice_feedback_daily",
            run_id="11111111-1111-1111-1111-111111111111",
            reason="new_free_text_comments",
            provider="openrouter:google",
            requested_model="tier:gemini_flash_lite",
            outcome=LLMOutcome.SERVED,
        )
        kwargs.update(overrides)
        return LLMUsageEventV1(**kwargs)

    def test_defaults_are_all_none_or_unavailable(self):
        event = self._base()
        assert event.input_tokens is None
        assert event.cached_input_tokens is None
        assert event.output_tokens is None
        assert event.cost_micros is None
        assert event.cost_source is CostSource.UNAVAILABLE
        assert event.cost_currency == "USD"

    def test_enum_coercion_from_string(self):
        event = self._base(outcome="timeout", cost_source="catalog", cost_micros=1)
        assert event.outcome is LLMOutcome.TIMEOUT
        assert event.cost_source is CostSource.CATALOG

    def test_provider_response_requires_cost_micros(self):
        with pytest.raises(ValueError, match="cost_micros"):
            self._base(cost_source=CostSource.PROVIDER_RESPONSE, cost_micros=None)

    def test_catalog_requires_cost_micros(self):
        with pytest.raises(ValueError, match="cost_micros"):
            self._base(cost_source=CostSource.CATALOG, cost_micros=None)

    def test_unavailable_forbids_cost_micros(self):
        with pytest.raises(ValueError, match="cost_micros"):
            self._base(cost_source=CostSource.UNAVAILABLE, cost_micros=1)

    def test_provider_response_with_cost_is_valid(self):
        event = self._base(
            cost_source=CostSource.PROVIDER_RESPONSE,
            cost_micros=1500,
            input_tokens=100,
            output_tokens=20,
        )
        assert event.cost_micros == 1500

    def test_negative_token_counts_rejected(self):
        with pytest.raises(ValueError):
            self._base(input_tokens=-1)

    def test_negative_cost_rejected(self):
        with pytest.raises(ValueError):
            self._base(
                cost_source=CostSource.CATALOG, cost_micros=-1, output_tokens=1, input_tokens=1
            )

    def test_to_dict_shape_matches_spec(self):
        event = self._base(
            response_model="gemini-2.5-flash-lite",
            input_tokens=100,
            cached_input_tokens=0,
            output_tokens=40,
            cost_source=CostSource.PROVIDER_RESPONSE,
            cost_micros=350,
        )
        d = event.to_dict()
        assert set(d) == {
            "schema_version", "service", "job_id", "run_id", "reason",
            "provider", "requested_model", "response_model", "input_tokens",
            "cached_input_tokens", "output_tokens", "cost_micros",
            "cost_currency", "cost_source", "outcome",
        }
        assert d["outcome"] == "served"
        assert d["cost_source"] == "provider_response"

    def test_budget_blocked_factory(self):
        event = LLMUsageEventV1.budget_blocked(
            service="nickname-roomservice-feedback",
            job_id="roomservice_feedback_daily",
            run_id="r1",
            reason="month_budget_exhausted",
            requested_model="tier:gemini_flash_lite",
        )
        assert event.outcome is LLMOutcome.BUDGET_BLOCKED
        assert event.provider == "none"
        assert event.cost_source is CostSource.UNAVAILABLE
        assert event.cost_micros is None
