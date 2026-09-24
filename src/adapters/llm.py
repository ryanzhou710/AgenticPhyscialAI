"""Minimal multimodal LLM client for CFD agent.

It supports Codex OAuth and OpenAI API Key Responses transports with rich image
content and strict Pydantic validation. Local audit records include prompt text
and model output; authentication headers and image bytes are excluded. Bearer
tokens and image data URLs are redacted from recorded text.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence, TypeAlias, TypeVar

import requests
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ValidationError

from src.config import PRODUCTION_MODEL, RuntimeConfig

T = TypeVar("T", bound=BaseModel)

_SUPPORTED_IMAGE_FORMATS = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}
_IMAGE_DETAILS = {"auto", "low", "high", "original"}
_DEFAULT_INSTRUCTIONS = "You assist CFD agent with geometry interpretation and meshing. Follow the supplied task instructions."

_CANDIDATE_ID_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:Body|Face|Edge|Loop)_\d+"
    r"|(?<![A-Za-z0-9_])[BFEL]\d+(?![A-Za-z0-9_])"
)
_DATA_URL_PATTERN = re.compile(
    r"data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+",
    flags=re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", flags=re.IGNORECASE)


class VisionStatus(str, Enum):
    """Observed vision capability for one provider/model client."""

    VERIFIED = "verified"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ModelCapabilities:
    provider: str
    model: str
    vision: VisionStatus
    structured_output: bool = True


@dataclass(frozen=True)
class VisionProbeResult:
    status: VisionStatus
    reason: str
    model_calls: int = 0


class GroundingLLMError(RuntimeError):
    """Base class for model-client failures."""


class CodexOAuthLoginError(GroundingLLMError):
    """The Codex CLI login flow could not create a readable OAuth cache."""


class StructuredOutputError(GroundingLLMError):
    """The provider response was not exactly valid data for the schema."""


class VisionUnavailableError(GroundingLLMError):
    """Raised instead of silently removing images from a request."""

    def __init__(self, status: VisionStatus):
        self.status = status
        super().__init__(
            f"Vision input is {status.value}; a successful probe is required for image requests"
        )


class ProviderRequestError(GroundingLLMError):
    """Sanitized transport error that never includes provider response bodies."""

    def __init__(
        self,
        *,
        status_code: int | None,
        capability_status: VisionStatus,
        reason: str,
        transport_error_type: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.capability_status = capability_status
        self.reason = reason
        self.transport_error_type = transport_error_type
        status = str(status_code) if status_code is not None else "transport"
        diagnostic = f" [{transport_error_type}]" if transport_error_type else ""
        super().__init__(f"Provider request failed ({status}): {reason}{diagnostic}")


@dataclass(frozen=True)
class CodexOAuthCredentials:
    access_token: str = field(repr=False)
    account_id: str | None = None


@dataclass(frozen=True)
class ModelImage:
    """Validated image bytes that can be encoded as a Responses data URL."""

    data: bytes = field(repr=False)
    media_type: str
    detail: Literal["auto", "low", "high", "original"] = "high"

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        detail: Literal["auto", "low", "high", "original"] = "high",
    ) -> "ModelImage":
        return cls.from_bytes(Path(path).read_bytes(), detail=detail)

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        detail: Literal["auto", "low", "high", "original"] = "high",
    ) -> "ModelImage":
        if detail not in _IMAGE_DETAILS:
            raise ValueError(f"Unsupported image detail: {detail}")
        if not data:
            raise ValueError("Image data is empty")
        try:
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
                image_format = str(image.format or "").upper()
        except Exception as error:  # Pillow exposes several decoder-specific errors.
            raise ValueError("Image data is invalid") from error
        media_type = _SUPPORTED_IMAGE_FORMATS.get(image_format)
        if media_type is None:
            raise ValueError(f"Unsupported image format: {image_format or 'unknown'}")
        return cls(data=bytes(data), media_type=media_type, detail=detail)

    def to_data_url(self) -> str:
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.media_type};base64,{encoded}"


ImageSource: TypeAlias = ModelImage | str | Path


def _normalise_images(images: Sequence[ImageSource]) -> tuple[ModelImage, ...]:
    return tuple(
        image if isinstance(image, ModelImage) else ModelImage.from_path(image) for image in images
    )


class ResponsesTransport(Protocol):
    def complete(self, payload: dict[str, Any]) -> str:
        """Return the complete output text for a Responses request."""


def _load_auth_json(path: Path) -> CodexOAuthCredentials:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Codex OAuth cache must contain a JSON object")

    candidates: list[str] = []
    account_id: str | None = None

    def add_token(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    add_token(data.get("access_token"))
    add_token(data.get("token"))
    if isinstance(data.get("account_id"), str):
        account_id = data["account_id"].strip() or None

    for key in ("auth", "credentials", "session"):
        nested = data.get(key)
        if isinstance(nested, dict):
            add_token(nested.get("access_token"))
            add_token(nested.get("token"))
            if account_id is None and isinstance(nested.get("account_id"), str):
                account_id = nested["account_id"].strip() or None

    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        add_token(tokens.get("access_token"))
        add_token(tokens.get("token"))
        if account_id is None and isinstance(tokens.get("account_id"), str):
            account_id = tokens["account_id"].strip() or None

    if not candidates:
        raise ValueError("Codex OAuth cache does not contain an access token")
    return CodexOAuthCredentials(candidates[0], account_id)


def _load_clawdbot_auth(path: Path) -> CodexOAuthCredentials:
    data = json.loads(path.read_text(encoding="utf-8"))
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, dict):
        raise ValueError("Codex OAuth profiles cache is invalid")

    ordered_keys = ["openai-codex:default", "openai-codex"]
    ordered_profiles = [profiles.get(key) for key in ordered_keys]
    ordered_profiles.extend(value for key, value in profiles.items() if key not in ordered_keys)
    for profile in ordered_profiles:
        if not isinstance(profile, dict):
            continue
        token = profile.get("access")
        if isinstance(token, str) and token.strip():
            account_id = profile.get("accountId")
            return CodexOAuthCredentials(
                token.strip(),
                account_id.strip() if isinstance(account_id, str) and account_id.strip() else None,
            )
    raise ValueError("Codex OAuth profiles cache does not contain an access token")


def load_codex_oauth(auth_path: str | Path | None = None) -> CodexOAuthCredentials:
    """Load supported local Codex OAuth credential-cache formats."""

    if auth_path is not None:
        path = Path(auth_path).expanduser()
        return _load_auth_json(path)

    explicit = os.getenv("FOAMAGENT_CODEX_AUTH_PATH")
    if explicit:
        return _load_auth_json(Path(explicit).expanduser())

    candidates: list[Path] = []
    codex_home = os.getenv("CODEX_HOME")
    if codex_home:
        candidates.append(Path(codex_home).expanduser() / "auth.json")
    candidates.append(Path.home() / ".codex" / "auth.json")
    candidates.append(
        Path.home() / ".clawdbot" / "agents" / "main" / "agent" / "auth-profiles.json"
    )

    for path in candidates:
        if not path.exists():
            continue
        if path.name == "auth-profiles.json":
            return _load_clawdbot_auth(path)
        return _load_auth_json(path)
    raise FileNotFoundError("No Codex OAuth cache was found")


def load_openai_api_key(api_key: str | None = None) -> str:
    """Load an OpenAI API key from an explicit value or OPENAI_API_KEY."""

    value = api_key or os.getenv("OPENAI_API_KEY")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "OpenAI API Key mode requires the OPENAI_API_KEY environment variable"
        )
    return value.strip()


def _device_auth_requested() -> bool:
    value = os.getenv("FOAMAGENT_CODEX_DEVICE_AUTH", "")
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _run_codex_login(*, device_auth: bool = False) -> None:
    """Delegate browser/device authentication to the installed Codex CLI."""

    executable = shutil.which("codex")
    if executable is None:
        raise CodexOAuthLoginError(
            "Codex OAuth credentials are missing and the 'codex' CLI was not found. "
            "Install Codex CLI, then run the CFD Agent again."
        )

    command = [executable, "login"]
    if device_auth:
        command.append("--device-auth")
        print("[Auth] Starting Codex device-code login. Complete authorization in your browser.")
    else:
        print("[Auth] Starting Codex login. Complete authorization in the browser window.")

    try:
        result = subprocess.run(command, check=False)
    except OSError as error:
        raise CodexOAuthLoginError(f"Could not start Codex login: {type(error).__name__}") from error
    if result.returncode != 0:
        raise CodexOAuthLoginError(
            f"Codex login failed with exit code {result.returncode}. "
            "Run 'codex login' manually and try again."
        )


def ensure_codex_oauth(
    auth_path: str | Path | None = None,
    *,
    device_auth: bool | None = None,
) -> CodexOAuthCredentials:
    """Load OAuth credentials, starting Codex login once when the cache is absent."""

    try:
        return load_codex_oauth(auth_path)
    except FileNotFoundError:
        _run_codex_login(
            device_auth=_device_auth_requested() if device_auth is None else device_auth
        )
        try:
            return load_codex_oauth(auth_path)
        except (FileNotFoundError, ValueError) as error:
            raise CodexOAuthLoginError(
                "Codex login completed, but CFD Agent still cannot read an OAuth cache. "
                "Ensure Codex stores credentials in auth.json and that CODEX_HOME or "
                "FOAMAGENT_CODEX_AUTH_PATH points to it."
            ) from error
    except ValueError:
        raise


def _extract_output_text(response_json: dict[str, Any]) -> str:
    output_text = response_json.get("output_text")
    if isinstance(output_text, str):
        return output_text
    texts: list[str] = []
    output = response_json.get("output", [])
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            continue
        content = item.get("content", [])
        for block in content if isinstance(content, list) else []:
            if (
                isinstance(block, dict)
                and block.get("type") in {"output_text", "text"}
                and isinstance(block.get("text"), str)
            ):
                texts.append(block["text"])
    return "".join(texts)


def _http_failure(status_code: int, response_body: str) -> ProviderRequestError:
    body = response_body.casefold()
    image_markers = ("input_image", "image input", "vision", "image modality", "images")
    unsupported_markers = ("unsupported", "not support", "does not support", "invalid type")
    rejected_image = status_code in {400, 404, 415, 422} and (
        any(marker in body for marker in image_markers)
        and any(marker in body for marker in unsupported_markers)
    )
    if rejected_image:
        return ProviderRequestError(
            status_code=status_code,
            capability_status=VisionStatus.UNSUPPORTED,
            reason="provider_rejected_image_input",
        )
    return ProviderRequestError(
        status_code=status_code,
        capability_status=VisionStatus.BLOCKED,
        reason="provider_or_environment_blocked",
    )


class CodexOAuthResponsesTransport:
    """Streaming transport for the ChatGPT/Codex OAuth Responses endpoint."""

    def __init__(
        self,
        credentials: CodexOAuthCredentials,
        *,
        base_url: str = "https://chatgpt.com/backend-api/codex",
        timeout_seconds: int | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._credentials = credentials
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds or int(os.getenv("FOAMAGENT_HTTP_TIMEOUT", "300"))
        self._session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._credentials.access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "CFD-Agent",
        }
        if self._credentials.account_id:
            headers["ChatGPT-Account-Id"] = self._credentials.account_id
        return headers

    def complete(self, payload: dict[str, Any]) -> str:
        try:
            response = self._session.post(
                f"{self._base_url}/responses",
                headers=self._headers(),
                json=payload,
                timeout=self._timeout_seconds,
                stream=True,
            )
        except requests.RequestException as error:
            raise ProviderRequestError(
                status_code=None,
                capability_status=VisionStatus.BLOCKED,
                reason="provider_transport_blocked",
                transport_error_type=type(error).__name__,
            ) from error

        if not 200 <= int(response.status_code) < 300:
            try:
                body = str(response.text or "")[:4000]
            except requests.RequestException:
                body = ""
            response.close()
            raise _http_failure(int(response.status_code), body)

        chunks: list[str] = []
        try:
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                if isinstance(raw_line, bytes):
                    raw_line = raw_line.decode("utf-8", errors="ignore")
                line = str(raw_line).strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                if event_type in {"response.failed", "response.incomplete", "error"}:
                    response_data = event.get("response") or event
                    details = (
                        response_data.get("error") or response_data.get("incomplete_details") or {}
                    )
                    code = details.get("code") or details.get("reason") or event_type
                    # Keep the provider's diagnostic identifier, never its body,
                    # which may echo private request data.
                    code = re.sub(r"[^A-Za-z0-9_.-]", "_", str(code))[:100]
                    raise ProviderRequestError(
                        status_code=None,
                        capability_status=VisionStatus.BLOCKED,
                        reason="provider_stream_failed:" + code,
                    )
                if event_type == "response.output_text.delta" and isinstance(
                    event.get("delta"), str
                ):
                    chunks.append(event["delta"])
                elif (
                    event_type == "response.output_text.done"
                    and not chunks
                    and isinstance(event.get("text"), str)
                ):
                    chunks.append(event["text"])
                elif event_type == "response.completed":
                    if not chunks and isinstance(event.get("response"), dict):
                        fallback = _extract_output_text(event["response"])
                        if fallback:
                            chunks.append(fallback)
                    break
        except requests.RequestException as error:
            raise ProviderRequestError(
                status_code=None,
                capability_status=VisionStatus.BLOCKED,
                reason="provider_stream_transport_blocked",
                transport_error_type=type(error).__name__,
            ) from error
        finally:
            response.close()
        return "".join(chunks).strip()


class OpenAIAPIKeyResponsesTransport(CodexOAuthResponsesTransport):
    """Streaming transport for the public OpenAI Responses API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: int | None = None,
        session: requests.Session | None = None,
    ) -> None:
        super().__init__(
            CodexOAuthCredentials(api_key),
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            session=session,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._credentials.access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "CFD-Agent",
        }


class _VisionProbeAnswer(BaseModel):
    model_config = {"extra": "forbid"}

    code: str
    black_square_quadrant: Literal["top_left", "top_right", "bottom_left", "bottom_right"]


@dataclass(frozen=True)
class _VisionChallenge:
    image: ModelImage
    code: str
    quadrant: str


def _make_vision_challenge() -> _VisionChallenge:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(secrets.choice(alphabet) for _ in range(6))
    quadrant = secrets.choice(("top_left", "top_right", "bottom_left", "bottom_right"))

    canvas = Image.new("RGB", (640, 360), "white")
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default(size=48)
    except TypeError:  # Pillow versions before the ``size`` argument.
        font = ImageFont.load_default()
    draw.text((210, 145), code, fill="black", font=font)
    positions = {
        "top_left": (45, 45, 115, 115),
        "top_right": (525, 45, 595, 115),
        "bottom_left": (45, 245, 115, 315),
        "bottom_right": (525, 245, 595, 315),
    }
    draw.rectangle(positions[quadrant], fill="black")
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return _VisionChallenge(ModelImage.from_bytes(buffer.getvalue()), code, quadrant)


class GroundingLLMClient:
    """Strict structured-output client used by CFD agent."""

    def __init__(
        self,
        *,
        model: str,
        transport: ResponsesTransport,
        instructions: str = _DEFAULT_INSTRUCTIONS,
        provider: str = "openai-codex",
        audit_dir: str | Path | None = None,
    ) -> None:
        self.model = model
        self.provider = provider
        self._transport = transport
        self._instructions = instructions or _DEFAULT_INSTRUCTIONS
        self._vision_status = VisionStatus.UNVERIFIED
        self._vision_reason = "not_probed"
        self._request_count = 0
        self._audit_session_dir: Path | None = None
        if audit_dir is not None:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            self._audit_session_dir = (
                Path(audit_dir).expanduser().resolve() / f"run-{timestamp}-{os.getpid()}"
            )

    @classmethod
    def from_codex_oauth(
        cls,
        *,
        model: str = PRODUCTION_MODEL,
        auth_path: str | Path | None = None,
        auto_login: bool = True,
        instructions: str = _DEFAULT_INSTRUCTIONS,
        base_url: str = "https://chatgpt.com/backend-api/codex",
        timeout_seconds: int | None = None,
        audit_dir: str | Path | None = None,
    ) -> "GroundingLLMClient":
        credentials = (
            ensure_codex_oauth(auth_path) if auto_login else load_codex_oauth(auth_path)
        )
        transport = CodexOAuthResponsesTransport(
            credentials,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )
        return cls(
            model=model,
            transport=transport,
            instructions=instructions,
            audit_dir=audit_dir,
        )

    @classmethod
    def from_openai_api_key(
        cls,
        *,
        model: str = PRODUCTION_MODEL,
        api_key: str | None = None,
        instructions: str = _DEFAULT_INSTRUCTIONS,
        base_url: str | None = None,
        timeout_seconds: int | None = None,
        audit_dir: str | Path | None = None,
    ) -> "GroundingLLMClient":
        key = load_openai_api_key(api_key)
        transport = OpenAIAPIKeyResponsesTransport(
            key,
            base_url=base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            timeout_seconds=timeout_seconds,
        )
        return cls(
            model=model,
            transport=transport,
            instructions=instructions,
            provider="openai-api-key",
            audit_dir=audit_dir,
        )

    @classmethod
    def from_runtime_config(
        cls,
        *,
        config: RuntimeConfig,
        audit_dir: str | Path | None = None,
    ) -> "GroundingLLMClient":
        """Create the configured model client without placing secrets in RuntimeConfig."""

        if config.auth_mode == "api_key":
            return cls.from_openai_api_key(
                model=config.model,
                timeout_seconds=config.model_timeout_s,
                audit_dir=audit_dir,
            )
        return cls.from_codex_oauth(
            model=config.model,
            timeout_seconds=config.model_timeout_s,
            audit_dir=audit_dir,
        )

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            provider=self.provider,
            model=self.model,
            vision=self._vision_status,
            structured_output=True,
        )

    @property
    def request_count(self) -> int:
        """Number of provider requests attempted by this client instance."""
        return self._request_count

    @property
    def audit_session_dir(self) -> Path | None:
        """Directory containing this client's redacted per-call audit records."""
        return self._audit_session_dir

    @staticmethod
    def _candidate_ids(user_prompt: str) -> list[str]:
        return sorted(set(_CANDIDATE_ID_PATTERN.findall(user_prompt)))

    @staticmethod
    def _audit_safe_text(value: str) -> str:
        value = _DATA_URL_PATTERN.sub("<redacted-image-data-url>", value)
        return _BEARER_PATTERN.sub("Bearer <redacted>", value)

    @staticmethod
    def _image_labels(images: Sequence[ImageSource]) -> tuple[str, ...]:
        labels: list[str] = []
        for index, image in enumerate(images, start=1):
            if isinstance(image, ModelImage):
                labels.append(f"<in-memory-image-{index}>")
            else:
                labels.append(str(Path(image).expanduser().resolve()))
        return tuple(labels)

    def _write_audit_json(self, path: Path, value: object) -> None:
        try:
            path.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as error:
            raise GroundingLLMError("Unable to write the required LLM audit record") from error

    def _start_audit(
        self,
        *,
        call_index: int,
        system_prompt: str,
        user_prompt: str,
        images: Sequence[ModelImage],
        image_labels: Sequence[str],
        response_model: type[BaseModel],
    ) -> Path | None:
        if self._audit_session_dir is None:
            return None
        call_dir = self._audit_session_dir / f"call-{call_index:06d}"
        try:
            call_dir.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            raise GroundingLLMError("Unable to create the required LLM audit directory") from error
        summary = {
            "call_index": call_index,
            "provider": self.provider,
            "model": self.model,
            "modality": "text+image" if images else "text",
            "system_prompt": self._audit_safe_text(system_prompt),
            "prompt": self._audit_safe_text(user_prompt),
            "candidate_ids": self._candidate_ids(user_prompt),
            "images": [
                {
                    "index": index,
                    "source": label,
                    "media_type": image.media_type,
                    "detail": image.detail,
                }
                for index, (label, image) in enumerate(
                    zip(image_labels, images, strict=True), start=1
                )
            ],
            "response_schema": response_model.__name__,
        }
        self._write_audit_json(call_dir / "request-summary.json", summary)
        return call_dir

    def _write_audit_text(self, path: Path, value: str) -> None:
        try:
            path.write_text(self._audit_safe_text(value), encoding="utf-8")
        except OSError as error:
            raise GroundingLLMError("Unable to write the required LLM audit record") from error

    def _write_audit_error(self, call_dir: Path | None, error: BaseException) -> None:
        if call_dir is None:
            return
        record: dict[str, object] = {
            "error_type": type(error).__name__,
            "error": self._audit_safe_text(str(error)),
        }
        if isinstance(error, ProviderRequestError):
            record.update(
                {
                    "status_code": error.status_code,
                    "capability_status": error.capability_status.value,
                    "reason": error.reason,
                    "transport_error_type": error.transport_error_type,
                }
            )
        self._write_audit_json(call_dir / "error.json", record)

    def _payload(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        images: Sequence[ModelImage],
        response_model: type[BaseModel],
    ) -> dict[str, Any]:
        schema = json.dumps(
            response_model.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        schema_instruction = (
            "Return exactly one JSON object and nothing else. Do not use markdown fences. "
            f"The object must satisfy this JSON Schema: {schema}"
        )
        system_text = (
            f"{system_prompt.strip()}\n\n{schema_instruction}"
            if system_prompt.strip()
            else schema_instruction
        )
        user_content: list[dict[str, Any]] = [{"type": "input_text", "text": user_prompt}]
        user_content.extend(
            {
                "type": "input_image",
                "image_url": image.to_data_url(),
                "detail": image.detail,
            }
            for image in images
        )
        return {
            "model": self.model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": system_text}],
                },
                {"role": "user", "content": user_content},
            ],
            "instructions": self._instructions,
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "reasoning": {"summary": "auto"},
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
        }

    def _invoke_unchecked(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        images: Sequence[ModelImage],
        image_labels: Sequence[str],
        response_model: type[T],
    ) -> T:
        call_index = self._request_count + 1
        call_dir = self._start_audit(
            call_index=call_index,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=images,
            image_labels=image_labels,
            response_model=response_model,
        )
        self._request_count = call_index
        payload = self._payload(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=images,
            response_model=response_model,
        )
        try:
            raw = self._transport.complete(payload)
        except Exception as error:
            self._write_audit_error(call_dir, error)
            raise
        if call_dir is not None:
            self._write_audit_text(call_dir / "raw-response.txt", raw)
        try:
            parsed = response_model.model_validate_json(raw, strict=True)
        except (ValidationError, ValueError) as error:
            structured_error = StructuredOutputError(
                "Provider response was not exactly valid JSON for the requested schema"
            )
            self._write_audit_error(call_dir, structured_error)
            raise structured_error from error
        if call_dir is not None:
            self._write_audit_json(
                call_dir / "parsed-result.json",
                parsed.model_dump(mode="json"),
            )
        return parsed

    def invoke(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
        images: Sequence[ImageSource] = (),
    ) -> T:
        """Invoke the model; image requests require a verified vision probe."""

        if images and self._vision_status is not VisionStatus.VERIFIED:
            raise VisionUnavailableError(self._vision_status)
        image_labels = self._image_labels(images)
        normalised_images = _normalise_images(images)
        return self._invoke_unchecked(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=normalised_images,
            image_labels=image_labels,
            response_model=response_model,
        )

    def probe_vision(
        self,
        output_dir: str | Path | None = None,
        *,
        force: bool = False,
    ) -> VisionProbeResult:
        """Send a hidden-answer visual challenge and cache the observed result."""

        # The challenge stays in memory and only records capability status.
        del output_dir

        if not force and self._vision_status is not VisionStatus.UNVERIFIED:
            return VisionProbeResult(self._vision_status, self._vision_reason, 0)

        calls_before = self._request_count
        challenge = _make_vision_challenge()
        try:
            answer = self._invoke_unchecked(
                system_prompt="Inspect the attached image carefully.",
                user_prompt=(
                    "Return the six-character code shown in the image and the quadrant "
                    "containing the solid black square relative to the image center."
                ),
                images=(challenge.image,),
                image_labels=("<in-memory-vision-probe>",),
                response_model=_VisionProbeAnswer,
            )
        except ProviderRequestError as error:
            self._vision_status = error.capability_status
            self._vision_reason = error.reason
        except StructuredOutputError:
            self._vision_status = VisionStatus.UNVERIFIED
            self._vision_reason = "probe_response_invalid"
        else:
            if (
                answer.code.strip().upper() == challenge.code
                and answer.black_square_quadrant == challenge.quadrant
            ):
                self._vision_status = VisionStatus.VERIFIED
                self._vision_reason = "probe_passed"
            else:
                self._vision_status = VisionStatus.UNVERIFIED
                self._vision_reason = "probe_answer_incorrect"
        return VisionProbeResult(
            self._vision_status,
            self._vision_reason,
            self._request_count - calls_before,
        )


__all__ = [
    "CodexOAuthCredentials",
    "CodexOAuthLoginError",
    "CodexOAuthResponsesTransport",
    "GroundingLLMClient",
    "GroundingLLMError",
    "ModelCapabilities",
    "ModelImage",
    "ImageSource",
    "ProviderRequestError",
    "PRODUCTION_MODEL",
    "StructuredOutputError",
    "VisionProbeResult",
    "VisionStatus",
    "VisionUnavailableError",
    "ensure_codex_oauth",
    "load_openai_api_key",
    "load_codex_oauth",
    "OpenAIAPIKeyResponsesTransport",
]
