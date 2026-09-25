"""Model transport, authentication and shared model configuration."""

import json
from types import SimpleNamespace

import pytest

from src.adapters.llm import (
    CodexOAuthCredentials,
    CodexOAuthResponsesTransport,
    ProviderRequestError,
)


def test_provider_error_code_is_retained_without_response_body():
    class Response:
        status_code = 200
        closed = False

        def iter_lines(self, **kwargs):
            yield "data: " + json.dumps(
                {
                    "type": "response.failed",
                    "response": {
                        "error": {"code": "server_is_overloaded", "message": "PRIVATE DATA"}
                    },
                }
            )

        def close(self):
            self.closed = True

    response = Response()

    class Session:
        def post(self, *args, **kwargs):
            return response

    transport = CodexOAuthResponsesTransport(CodexOAuthCredentials("test-only"), session=Session())
    with pytest.raises(ProviderRequestError, match="server_is_overloaded") as error:
        transport.complete({})
    assert "PRIVATE" not in str(error.value)
    assert response.closed


def test_custom_model_is_sent_by_existing_transport(monkeypatch):
    from src.adapters import llm
    from src.services.contracts import ConfirmationPayload

    payloads = []

    class Transport:
        def complete(self, payload):
            payloads.append(payload)
            return '{"action":"cancel","boundary_roles":{}}'

    monkeypatch.setattr(llm, "load_codex_oauth", lambda path: CodexOAuthCredentials("offline"))
    monkeypatch.setattr(llm, "CodexOAuthResponsesTransport", lambda *args, **kwargs: Transport())
    client = llm.GroundingLLMClient.from_codex_oauth(model="chosen-model")
    client.invoke(system_prompt="test", user_prompt="test", response_model=ConfirmationPayload)
    assert payloads[0]["model"] == "chosen-model"


def test_missing_codex_cache_starts_cli_login(monkeypatch):
    from src.adapters import llm

    credentials = iter(
        [FileNotFoundError("missing"), CodexOAuthCredentials("offline")]
    )
    login_calls = []

    def load(_path):
        value = next(credentials)
        if isinstance(value, Exception):
            raise value
        return value

    class Transport:
        def complete(self, payload):
            return "{}"

    monkeypatch.setattr(llm, "load_codex_oauth", load)
    monkeypatch.setattr(llm.shutil, "which", lambda name: "codex")
    monkeypatch.setattr(
        llm.subprocess,
        "run",
        lambda command, check: login_calls.append((command, check))
        or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(llm, "CodexOAuthResponsesTransport", lambda *args, **kwargs: Transport())

    llm.GroundingLLMClient.from_codex_oauth(model="chosen-model")

    assert login_calls == [(["codex", "login"], False)]


def test_device_auth_mode_is_forwarded_to_codex_cli(monkeypatch):
    from src.adapters import llm

    monkeypatch.setenv("FOAMAGENT_CODEX_DEVICE_AUTH", "1")
    login_calls = []
    monkeypatch.setattr(llm, "load_codex_oauth", lambda _path: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(llm.shutil, "which", lambda name: "codex")
    monkeypatch.setattr(
        llm.subprocess,
        "run",
        lambda command, check: login_calls.append(command) or SimpleNamespace(returncode=1),
    )

    with pytest.raises(llm.CodexOAuthLoginError, match="Codex login failed"):
        llm.GroundingLLMClient.from_codex_oauth(model="chosen-model")

    assert login_calls == [["codex", "login", "--device-auth"]]


def test_openai_api_key_transport_uses_standard_bearer_header():
    from src.adapters.llm import OpenAIAPIKeyResponsesTransport

    transport = OpenAIAPIKeyResponsesTransport("sk-test-only")

    assert transport._headers() == {
        "Authorization": "Bearer sk-test-only",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "CFD-Agent",
    }


def test_runtime_config_selects_api_key_client(monkeypatch):
    from src.adapters import llm
    from src.config import RuntimeConfig

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    monkeypatch.setattr(
        llm,
        "OpenAIAPIKeyResponsesTransport",
        lambda *args, **kwargs: object(),
    )

    client = llm.GroundingLLMClient.from_runtime_config(
        config=RuntimeConfig(model="api-model", auth_mode="api_key")
    )

    assert client.provider == "openai-api-key"
    assert client.model == "api-model"


def test_api_key_mode_requires_environment_variable(monkeypatch):
    from src.adapters import llm

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        llm.GroundingLLMClient.from_openai_api_key(model="api-model")


def test_selection_requirements_and_reviewer_share_configured_model(tmp_path, monkeypatch):
    from src.adapters.llm import GroundingLLMClient
    from src.config import RuntimeConfig
    from src.services import reviewer, selection
    from src.services.contracts import CadSelectionPlan, MeshRequirements, RepairDecision
    from src.services.geometry_catalog import GeometryCatalog

    selected = CadSelectionPlan(
        status="selected",
        reference_view="Front",
        explanation="offline",
        fluid_domain_action="extract",
        extraction_strategy="faces",
        seed_inner_wall_id="F2",
        openings=[
            {
                "selection_kind": "face",
                "object_ids": ["F1"],
                "role": "inlet",
                "name": "feed",
                "description": "opening",
                "reason": "offline",
            }
        ],
    )

    class Client:
        def invoke(self, **kwargs):
            schema = kwargs["response_model"]
            if schema is CadSelectionPlan:
                return selected
            if schema is MeshRequirements:
                return MeshRequirements()
            return RepairDecision(
                action="stop", target_step="query_geometry", diagnosis="offline", evidence="offline"
            )

    models = []

    def factory(**kwargs):
        models.append(kwargs["model"])
        return Client()

    monkeypatch.setattr(GroundingLLMClient, "from_codex_oauth", factory)
    catalog = GeometryCatalog(
        catalog_id="c",
        geometry_id="g",
        faces=[{"id": "F1", "kind": "face"}, {"id": "F2", "kind": "face"}],
    )
    settings = RuntimeConfig(model="chosen-model")
    plan = selection.plan_cad_selection(
        catalog=catalog, user_prompt="mesh", audit_dir=tmp_path, config=settings
    )
    selection.extract_mesh_requirements(
        catalog=catalog,
        user_prompt="mesh",
        boundary_names=[opening.name for opening in plan.openings],
        audit_dir=tmp_path,
        config=settings,
    )
    reviewer.diagnose_failure(
        {
            "run_dir": str(tmp_path),
            "runtime_config": settings.model_dump(),
            "max_repair_rounds": 1,
            "failed_step": "query_geometry",
            "error": "offline",
        }
    )
    assert models == ["chosen-model"] * 3
