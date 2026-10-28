"""The five probed managed transcription contracts, each with one physical POST."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.parse import quote

from ai_bench.contracts import (
    Compatibility,
    CompatibilityLevel,
    ExecutionOutput,
    ExecutionRequest,
    JsonValue,
    Modality,
    ParsedOutput,
    PipelineType,
    PricingModel,
    ProviderFailureEvidence,
    StandalonePipeline,
    Usage,
)
from ai_bench.failures import ProviderFailure, retry_delay
from ai_bench.hashing import content_digest, sha256_digest

from .configuration import SpeechProviderConfiguration, load_configuration
from .evaluation import NORMALIZATION_VERSION, Transcript
from .upload_media import MEDIA_TYPES, validate_audio

if TYPE_CHECKING:
    import httpx

SPEECH_API_VERSION = "2025-10-15"
OPENAI_API_VERSION = "2025-03-01-preview"
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
REQUEST_TIMEOUT_SECONDS = 180
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
type SolutionId = Literal[
    "azure-speech-fast",
    "gpt-transcribe",
    "managed-whisper",
    "mai-transcribe-1-5",
    "mai-transcribe-2",
]


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    elapsed_ms: Decimal = Decimal(0)


class PhysicalRequestError(RuntimeError):
    def __init__(
        self, classification: Literal["timeout", "transport"], elapsed_ms: Decimal
    ) -> None:
        self.classification = classification
        self.elapsed_ms = elapsed_ms
        super().__init__("The physical transcription request did not return a response.")


class TranscriptionTransport(Protocol):
    def post(
        self,
        url: str,
        *,
        token: str,
        audio_field: str,
        filename: str,
        audio: bytes,
        media_type: str,
        fields: Mapping[str, str],
    ) -> HttpResponse: ...


class HttpxTransport:
    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.transport = transport

    def post(
        self,
        url: str,
        *,
        token: str,
        audio_field: str,
        filename: str,
        audio: bytes,
        media_type: str,
        fields: Mapping[str, str],
    ) -> HttpResponse:
        import httpx

        started = time.perf_counter()
        try:
            with (
                httpx.Client(
                    transport=self.transport or httpx.HTTPTransport(retries=0),
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    "POST",
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    files={audio_field: (filename, audio, media_type)},
                    data=dict(fields),
                ) as response,
            ):
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk[: MAX_RESPONSE_BYTES + 1 - len(content)])
                    if len(content) > MAX_RESPONSE_BYTES:
                        break
            return HttpResponse(
                status_code=response.status_code,
                headers=dict(response.headers),
                content=bytes(content),
                elapsed_ms=Decimal(str((time.perf_counter() - started) * 1000)),
            )
        except httpx.TimeoutException:
            raise PhysicalRequestError(
                "timeout", Decimal(str((time.perf_counter() - started) * 1000))
            ) from None
        except httpx.TransportError:
            raise PhysicalRequestError(
                "transport", Decimal(str((time.perf_counter() - started) * 1000))
            ) from None


def entra_token() -> str:
    from azure.core.exceptions import AzureError
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

    runtime = os.getenv("AI_BENCH_RUNTIME", "local").lower()
    if runtime not in {"local", "azure"}:
        raise ValueError("AI_BENCH_RUNTIME must be local or azure.")
    if runtime == "azure" and os.getenv("AI_BENCH_AUTH_MODE", "entra").lower() != "entra":
        raise ValueError("Hosted speech authentication requires managed identity in Entra mode.")
    credential = (
        ManagedIdentityCredential(client_id=os.getenv("AI_BENCH_AZURE_MANAGED_IDENTITY_CLIENT_ID"))
        if runtime == "azure"
        else DefaultAzureCredential()
    )
    try:
        with credential:
            return credential.get_token(TOKEN_SCOPE).token
    except AzureError:
        raise ProviderFailure(
            ProviderFailureEvidence(
                classification="authentication",
                message="Entra authentication failed before transcription.",
                retryable=False,
                usage=Usage(
                    dimensions={"calls": Decimal(0)},
                    complete=False,
                    uncertainty="No transcription was sent; provider billing is not observed.",
                ),
            )
        ) from None


def _request_id(headers: Mapping[str, str]) -> str | None:
    normalized = {key.lower(): value for key, value in headers.items()}
    for header in ("apim-request-id", "x-request-id", "request-id", "x-ms-request-id"):
        value = normalized.get(header)
        if value is not None and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
            return value
    return None


def _safe_error_body(content: bytes) -> tuple[bytes, bool]:
    """Keep safe response bytes exactly; redact secrets if a service echoes them."""
    text = content[:MAX_RESPONSE_BYTES].decode("utf-8", errors="replace")
    redacted = re.sub(
        r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1[redacted]",
        text,
    )
    redacted = re.sub(
        r'(?i)("(?:api[-_]?key|access[-_]?token|authorization|secret|password)"\s*:\s*")[^"]*',
        r"\1[redacted]",
        redacted,
    )
    redacted = re.sub(r'https?://[^\s"<>]+', "[redacted-url]", redacted)
    changed = redacted != text or len(content) > MAX_RESPONSE_BYTES
    return (redacted.encode("utf-8") if changed else content), changed


def _observed_usage(body: object, base: Usage) -> Usage:
    dimensions = dict(base.dimensions)
    invalid_usage = False
    if isinstance(body, dict):
        usage = body.get("usage")
        if isinstance(usage, dict):
            for name in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "audio_tokens",
                "audio_seconds",
            ):
                value = usage.get(name)
                if name not in usage:
                    continue
                if isinstance(value, str | int | float) and not isinstance(value, bool):
                    try:
                        amount = Decimal(str(value))
                    except InvalidOperation:
                        invalid_usage = True
                        continue
                    if amount.is_finite() and amount >= 0:
                        dimensions[f"provider_{name}"] = amount
                    else:
                        invalid_usage = True
                else:
                    invalid_usage = True
    return base.model_copy(
        update={
            "dimensions": dimensions,
            "uncertainty": (
                f"{base.uncertainty} Some reported usage values could not be interpreted."
                if invalid_usage
                else base.uncertainty
            ),
        }
    )


@dataclass(frozen=True)
class ManagedTranscriptionSolution:
    id: SolutionId
    configuration: SpeechProviderConfiguration | None = None
    transport: TranscriptionTransport | None = None
    token_provider: Callable[[], str] | None = None
    version = "1.1.0"
    networked = True
    compatibility = Compatibility(
        level=CompatibilityLevel.FULL,
        comparable_quality=True,
        reason="Produces a complete transcript from validated unchanged original FLAC, WAV or MP3.",
    )

    @property
    def display_name(self) -> str:
        return {
            "azure-speech-fast": "Azure Speech Fast",
            "gpt-transcribe": "GPT-Transcribe",
            "managed-whisper": "Managed Whisper",
            "mai-transcribe-1-5": "MAI-Transcribe-1.5",
            "mai-transcribe-2": "MAI-Transcribe-2",
        }[self.id]

    @property
    def pipeline(self) -> StandalonePipeline:
        return StandalonePipeline(pipeline_type=PipelineType.STANDALONE, adapter_id=self.id)

    @property
    def pricing(self) -> PricingModel:
        return PricingModel(
            id=f"{self.id}-unknown-price",
            version="2026-09-16",
            currency="USD",
            region="eastus" if self.id.startswith("mai-") else "swedencentral",
            effective_at=datetime(2026, 9, 16, tzinfo=UTC),
            rates=(),
            source="No reviewed, exact regional price is pinned for this pilot; cost is unknown.",
        )

    def _configuration(self) -> SpeechProviderConfiguration:
        if os.getenv("AI_BENCH_RUNTIME", "local").lower() == "azure":
            return load_configuration()
        return self.configuration or load_configuration()

    def validate_execution(self) -> None:
        self._configuration()
        if os.getenv("AI_BENCH_AUTH_MODE", "entra").lower() not in {"entra", "offline"}:
            raise ValueError("The speech pilot supports Entra only, never provider keys.")

    def _selectors(self) -> tuple[str, str | None, str, str, str | None]:
        configuration = self._configuration()
        if self.id == "gpt-transcribe":
            return (
                configuration.openai_endpoint,
                configuration.gpt_transcribe_deployment,
                "en",
                "gpt-transcribe",
                "2026-07-28",
            )
        if self.id == "managed-whisper":
            return (
                configuration.openai_endpoint,
                configuration.whisper_deployment,
                "en",
                "whisper",
                "001",
            )
        if self.id == "azure-speech-fast":
            return configuration.speech_endpoint, None, "en-US", "standard-fast", None
        model = "MAI-Transcribe-1.5" if self.id == "mai-transcribe-1-5" else "MAI-Transcribe-2"
        return configuration.mai_endpoint, None, "en", model, None

    def execution_settings(self) -> Mapping[str, JsonValue]:
        endpoint, deployment, locale, model, model_version = self._selectors()
        options: dict[str, JsonValue] = dict(self._fields(deployment, locale, model))
        return {
            "api_version": OPENAI_API_VERSION if deployment else SPEECH_API_VERSION,
            "endpoint_hash": content_digest(endpoint.lower().encode("utf-8")),
            "deployment": deployment,
            "model": model,
            "deployment_model_version": model_version,
            "locale": locale,
            "request_options": options,
            "timeout_seconds": REQUEST_TIMEOUT_SECONDS,
            "transport_retries": 0,
            "follow_redirects": False,
            "input_media_types": list(MEDIA_TYPES.values()),
            "input_validation_version": "speech-upload-v1",
            "normalization_version": NORMALIZATION_VERSION,
            "auth_mode": "entra",
            "identity_mode": os.getenv("AI_BENCH_RUNTIME", "local").lower(),
            "provider_weights_immutable": False,
        }

    @property
    def throttle_scope(self) -> str:
        endpoint, deployment, _, _, _ = self._selectors()
        return sha256_digest(
            {
                "endpoint": endpoint.lower(),
                "service": "openai-audio" if deployment else "speech-fast",
                "deployment": deployment,
            }
        )

    @staticmethod
    def _fields(deployment: str | None, locale: str, model: str) -> dict[str, str]:
        if deployment:
            return {"language": locale}
        definition: dict[str, JsonValue] = {"locales": [locale]}
        if model.startswith("MAI-"):
            definition["enhancedMode"] = {"enabled": True, "model": model}
        return {"definition": json.dumps(definition, separators=(",", ":"))}

    def execute(self, request: ExecutionRequest) -> ExecutionOutput:
        self.validate_execution()
        if (
            request.sample.reference is not None
            or request.sample.input.modality != Modality.AUDIO
            or request.sample.input.media_type not in MEDIA_TYPES.values()
            or len(request.input_bytes) != request.sample.input.size_bytes
            or content_digest(request.input_bytes) != request.sample.input.id
        ):
            raise ValueError(
                "Transcription requires verified persisted audio bytes and an input-only request."
            )
        extension = next(
            extension
            for extension, media_type in MEDIA_TYPES.items()
            if media_type == request.sample.input.media_type
        )
        audio = validate_audio(request.input_bytes, extension, request.sample.input.media_type)
        usage = Usage(
            dimensions={"calls": Decimal(1), "submitted_audio_seconds": audio.duration_seconds},
            complete=False,
            uncertainty="Submitted audio and calls are known; billed usage is not established.",
        )
        endpoint, deployment, locale, model, _ = self._selectors()
        url = (
            f"{endpoint}/openai/deployments/{quote(deployment, safe='')}"
            f"/audio/transcriptions?api-version={OPENAI_API_VERSION}"
            if deployment
            else f"{endpoint}/speechtotext/transcriptions:transcribe"
            f"?api-version={SPEECH_API_VERSION}"
        )
        try:
            response = (self.transport or HttpxTransport()).post(
                url,
                token=(self.token_provider or entra_token)(),
                audio_field="file" if deployment else "audio",
                filename=f"sample-{content_digest(request.sample.id.encode('utf-8'))[:32]}{extension}",
                audio=request.input_bytes,
                media_type=audio.media_type,
                fields=self._fields(deployment, locale, model),
            )
        except PhysicalRequestError as error:
            raise ProviderFailure(
                ProviderFailureEvidence(
                    classification=error.classification,
                    message=str(error),
                    retryable=True,
                    usage=usage,
                    retry_delay=retry_delay(
                        {},
                        provider_message=None,
                        policy=request.retry_policy,
                        attempt_number=request.attempt_number,
                    ),
                    provider_latency_ms=error.elapsed_ms,
                )
            ) from None
        request_id = _request_id(response.headers)
        body: object = None
        try:
            body = json.loads(response.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Raw evidence still precedes the separate parse operation or retry fallback.
            body = None
        usage = _observed_usage(body, usage)
        media_type = next(
            (
                value.split(";")[0].strip()
                for key, value in response.headers.items()
                if key.lower() == "content-type"
            ),
            "application/json",
        )
        if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]{1,80}/[A-Za-z0-9!#$&^_.+-]{1,80}", media_type):
            media_type = "application/octet-stream"
        if not 200 <= response.status_code < 300 or len(response.content) > MAX_RESPONSE_BYTES:
            message: str | None = None
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                candidate = body["error"].get("message")
                message = candidate if isinstance(candidate, str) else None
            retryable = response.status_code in {408, 409, 429, 500, 502, 503, 504}
            classification: Literal[
                "rate-limit", "authentication", "provider-http", "invalid-response"
            ] = (
                "rate-limit"
                if response.status_code == 429
                else "authentication"
                if response.status_code in {401, 403}
                else "invalid-response"
                if len(response.content) > MAX_RESPONSE_BYTES
                else "provider-http"
            )
            raw, redacted = _safe_error_body(response.content)
            raise ProviderFailure(
                ProviderFailureEvidence(
                    classification=classification,
                    message=f"Transcription returned HTTP {response.status_code}.",
                    http_status=response.status_code,
                    provider_request_id=request_id,
                    retryable=retryable,
                    usage=usage,
                    response_redacted=redacted,
                    response_truncated=len(response.content) > MAX_RESPONSE_BYTES,
                    provider_latency_ms=response.elapsed_ms,
                    retry_delay=retry_delay(
                        response.headers,
                        provider_message=message,
                        policy=request.retry_policy,
                        attempt_number=request.attempt_number,
                    )
                    if retryable
                    else None,
                ),
                raw=raw,
                media_type=media_type,
                modality=Modality.STRUCTURED if media_type == "application/json" else Modality.TEXT,
            )
        return ExecutionOutput(
            raw=response.content,
            modality=Modality.STRUCTURED if media_type == "application/json" else Modality.TEXT,
            media_type=media_type,
            usage=usage,
            provider_request_id=request_id,
            provider_latency_ms=response.elapsed_ms,
        )

    def parse(self, output: ExecutionOutput) -> ParsedOutput:
        body = json.loads(output.raw)
        if not isinstance(body, dict):
            raise ValueError("Transcription response must be a JSON object.")
        if self.id in {"gpt-transcribe", "managed-whisper"}:
            text = body.get("text")
            if not isinstance(text, str):
                raise ValueError("Managed Audio response must contain a text string.")
        else:
            phrases = body.get("combinedPhrases")
            if not isinstance(phrases, list) or any(
                not isinstance(phrase, dict) or not isinstance(phrase.get("text"), str)
                for phrase in phrases
            ):
                raise ValueError("Speech response must contain typed combinedPhrases.")
            text = " ".join(phrase["text"] for phrase in phrases)
        return ParsedOutput(value=Transcript(text=text).model_dump(mode="json"))
