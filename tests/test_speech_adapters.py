from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_bench.contracts import (
    AudioArtifact,
    BenchmarkSample,
    ExecutionRequest,
    Modality,
    RetryPolicy,
    TextArtifact,
)
from ai_bench.failures import ProviderFailure, retry_delay
from ai_bench.hashing import content_digest
from benchmark_packs.speech_to_text import adapters
from benchmark_packs.speech_to_text.adapters import (
    HttpResponse,
    HttpxTransport,
    ManagedTranscriptionSolution,
    SolutionId,
)
from benchmark_packs.speech_to_text.configuration import (
    HOSTED_SETTINGS,
    SpeechProviderConfiguration,
    load_configuration,
)
from tests.speech_support import make_audio, make_flac


def provider_configuration() -> SpeechProviderConfiguration:
    return SpeechProviderConfiguration(
        schema_version="1",
        speech_endpoint="https://example.cognitiveservices.azure.com",
        openai_endpoint="https://example.openai.azure.com",
        mai_endpoint="https://mai-example.cognitiveservices.azure.com",
        gpt_transcribe_deployment="explicit-gpt",
        whisper_deployment="explicit-whisper",
    )


def audio_request() -> ExecutionRequest:
    audio = make_flac(800)
    return ExecutionRequest(
        run_id="a" * 64,
        task_id="b" * 64,
        idempotency_key="b" * 64,
        input_bytes=audio,
        sample=BenchmarkSample(
            id="1-1-0001",
            input=AudioArtifact(
                id=content_digest(audio),
                modality=Modality.AUDIO,
                media_type="audio/flac",
                size_bytes=len(audio),
                uri=f"artifact://sha256/{content_digest(audio)}",
            ),
        ),
    )


@pytest.mark.parametrize(
    ("solution_id", "path", "field", "definition"),
    [
        (
            "azure-speech-fast",
            "/speechtotext/transcriptions:transcribe?api-version=2025-10-15",
            "audio",
            {"locales": ["en-US"]},
        ),
        (
            "mai-transcribe-1-5",
            "/speechtotext/transcriptions:transcribe?api-version=2025-10-15",
            "audio",
            {"locales": ["en"], "enhancedMode": {"enabled": True, "model": "MAI-Transcribe-1.5"}},
        ),
        (
            "mai-transcribe-2",
            "/speechtotext/transcriptions:transcribe?api-version=2025-10-15",
            "audio",
            {"locales": ["en"], "enhancedMode": {"enabled": True, "model": "MAI-Transcribe-2"}},
        ),
        (
            "gpt-transcribe",
            "/openai/deployments/explicit-gpt/audio/transcriptions?api-version=2025-03-01-preview",
            "file",
            None,
        ),
        (
            "managed-whisper",
            "/openai/deployments/explicit-whisper/audio/transcriptions?api-version=2025-03-01-preview",
            "file",
            None,
        ),
    ],
)
@pytest.mark.parametrize("extension", [".flac", ".wav", ".mp3"])
def test_exact_probed_multipart_contract(
    solution_id: SolutionId,
    path: str,
    field: str,
    definition: dict[str, Any] | None,
    extension: str,
) -> None:
    physical_requests: list[httpx.Request] = []
    original = make_audio(extension, sample_rate=24000, channels=2)
    media_type = {".flac": "audio/flac", ".wav": "audio/wav", ".mp3": "audio/mpeg"}[extension]
    request = audio_request().model_copy(
        update={
            "input_bytes": original,
            "sample": BenchmarkSample(
                id="private-original-name",
                input=AudioArtifact(
                    id=content_digest(original),
                    modality=Modality.AUDIO,
                    media_type=media_type,
                    size_bytes=len(original),
                    uri=f"artifact://sha256/{content_digest(original)}",
                    metadata={
                        "original_filename": "private-original-name",
                        "reference": "SECRET REFERENCE",
                        "prompt": "SECRET PROMPT",
                    },
                ),
                metadata={"reference_text": "SECRET REFERENCE METADATA"},
            ),
        }
    )

    def send(post: httpx.Request) -> httpx.Response:
        physical_requests.append(post)
        assert str(post.url).endswith(path)
        assert post.headers["Authorization"] == "Bearer invented-token"
        body = post.read()
        assert request.input_bytes in body
        opaque = content_digest(request.sample.id.encode())[:32]
        assert f'name="{field}"; filename="sample-{opaque}{extension}"'.encode() in body
        assert f"Content-Type: {media_type}".encode() in body
        assert b'name="reference"' not in body and b'name="prompt"' not in body
        assert b"private-original-name" not in body
        assert b"SECRET" not in body
        assert b"reference_text" not in body and b"original_filename" not in body
        if definition is not None:
            assert b'name="definition"' in body
            assert json.dumps(definition, separators=(",", ":")).encode() in body
            return httpx.Response(
                200, json={"combinedPhrases": [{"text": "Invented"}, {"text": "speech"}]}
            )
        assert b'name="language"\r\n\r\nen\r\n' in body
        return httpx.Response(200, json={"text": "Invented speech"})

    solution = ManagedTranscriptionSolution(
        solution_id,
        configuration=provider_configuration(),
        transport=HttpxTransport(httpx.MockTransport(send)),
        token_provider=lambda: "invented-token",
    )
    output = solution.execute(request)
    assert solution.parse(output).value == {
        "schema_version": "transcript-v1",
        "text": "Invented speech",
    }
    assert len(physical_requests) == 1
    assert output.raw
    assert output.usage.dimensions["calls"] == 1
    assert output.usage.dimensions["submitted_audio_seconds"] == Decimal("0.2")
    assert output.usage.complete is False
    assert solution.pricing.estimate(output.usage).amount is None
    assert output.provider_latency_ms is not None


@pytest.mark.parametrize(
    ("headers", "message", "seconds", "source"),
    [
        ({"Retry-After": "12.5"}, None, "12.5", "retry-after-seconds"),
        ({"retry-after": "Wed, 16 Sep 2026 12:00:03 GMT"}, None, "3", "retry-after-date"),
        ({"retry-after": "Wed, 16 Sep 2026 11:00:00 GMT"}, None, "0", "retry-after-date"),
        ({"retry-after-ms": "1250"}, None, "1.25", "retry-after-ms"),
        ({"x-ms-retry-after-ms": "2500"}, None, "2.5", "x-ms-retry-after-ms"),
        ({}, "RateLimitReached. Please retry after 7 seconds.", "7", "provider-message"),
        ({"retry-after": "10"}, "Please retry after 20 seconds", "10", "retry-after-seconds"),
        ({"retry-after": "invalid"}, "Please retry after 8 seconds", "8", "provider-message"),
    ],
)
def test_retry_delay_variants(
    headers: dict[str, str],
    message: str | None,
    seconds: str,
    source: str,
) -> None:
    delay = retry_delay(
        headers,
        provider_message=message,
        policy=RetryPolicy(),
        attempt_number=2,
        now=datetime(2026, 9, 16, 12, tzinfo=UTC),
    )
    assert delay.scheduled_seconds == Decimal(seconds)
    assert delay.provider_seconds == Decimal(seconds)
    assert delay.source == source
    assert delay.fallback_reason is None


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "100000000000000", "invalid"])
def test_invalid_delays_have_a_bounded_recorded_fallback(value: str) -> None:
    delay = retry_delay(
        {"Retry-After": value},
        provider_message=None,
        policy=RetryPolicy(),
        attempt_number=20,
    )
    assert delay.scheduled_seconds == 60
    assert delay.provider_seconds is None
    assert delay.source == "fallback" and "invalid" in (delay.fallback_reason or "")


def test_no_hidden_retry_and_safe_429_evidence() -> None:
    calls = 0
    raw = b'{"error":{"code":"RateLimitReached","message":"Please retry after 9 seconds"}}'

    def send(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            content=raw,
            headers={
                "Content-Type": "application/json",
                "apim-request-id": "synthetic-request",
            },
        )

    solution = ManagedTranscriptionSolution(
        "gpt-transcribe",
        configuration=provider_configuration(),
        transport=HttpxTransport(httpx.MockTransport(send)),
        token_provider=lambda: "invented-token",
    )
    with pytest.raises(ProviderFailure) as caught:
        solution.execute(audio_request())
    failure = caught.value
    assert calls == 1
    assert failure.raw == raw
    assert failure.evidence.classification == "rate-limit"
    assert failure.evidence.http_status == 429
    assert failure.evidence.provider_request_id == "synthetic-request"
    assert failure.evidence.retry_delay is not None
    assert failure.evidence.retry_delay.scheduled_seconds == 9
    assert failure.evidence.retry_delay.source == "provider-message"
    assert failure.evidence.usage.complete is False


def test_failed_body_redacts_credentials_and_urls() -> None:
    raw = json.dumps(
        {
            "error": {"message": "Bearer invented-secret https://example.invalid/?sig=secret"},
            "api_key": "invented-secret",
        }
    ).encode()
    safe, redacted = adapters._safe_error_body(raw)
    assert redacted
    assert b"invented-secret" not in safe and b"sig=secret" not in safe
    assert b"[redacted" in safe


@pytest.mark.parametrize("solution_id", ["azure-speech-fast", "gpt-transcribe"])
def test_empty_success_is_not_a_provider_failure(solution_id: SolutionId) -> None:
    response: dict[str, Any] = (
        {"text": ""} if solution_id == "gpt-transcribe" else {"combinedPhrases": []}
    )

    class EmptyTransport:
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
            return HttpResponse(
                200, {"Content-Type": "application/json"}, json.dumps(response).encode()
            )

    solution = ManagedTranscriptionSolution(
        solution_id,
        configuration=provider_configuration(),
        transport=EmptyTransport(),
        token_provider=lambda: "invented-token",
    )
    assert solution.parse(solution.execute(audio_request())).value == {
        "schema_version": "transcript-v1",
        "text": "",
    }


def test_scope_and_behavior_pins_are_explicit() -> None:
    configuration = provider_configuration()
    identifiers: tuple[SolutionId, ...] = (
        "azure-speech-fast",
        "gpt-transcribe",
        "managed-whisper",
        "mai-transcribe-1-5",
        "mai-transcribe-2",
    )
    solutions = {
        identifier: ManagedTranscriptionSolution(identifier, configuration)
        for identifier in identifiers
    }
    assert (
        solutions["mai-transcribe-1-5"].throttle_scope
        == solutions["mai-transcribe-2"].throttle_scope
    )
    assert len({solution.throttle_scope for solution in solutions.values()}) == 4
    settings = solutions["gpt-transcribe"].execution_settings()
    assert settings["deployment"] == "explicit-gpt"
    assert settings["deployment_model_version"] == "2026-07-28"
    assert settings["transport_retries"] == 0
    assert settings["provider_weights_immutable"] is False
    changed = ManagedTranscriptionSolution(
        "gpt-transcribe",
        configuration.model_copy(update={"gpt_transcribe_deployment": "different"}),
    )
    assert changed.throttle_scope != solutions["gpt-transcribe"].throttle_scope
    assert changed.execution_settings() != settings


def test_provider_configuration_is_external_strict_and_key_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("AI_BENCH_SPEECH_PROVIDER_CONFIG", raising=False)
    with pytest.raises(ValueError, match="Set AI_BENCH"):
        load_configuration()
    path = tmp_path / "provider-config.json"
    path.write_text(provider_configuration().model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("AI_BENCH_SPEECH_PROVIDER_CONFIG", str(path))
    assert load_configuration() == provider_configuration()
    content = json.loads(path.read_text())
    content["api_key"] = "invented-rejected-value"
    path.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid"):
        load_configuration()


def test_hosted_credentials_never_use_default_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Credential:
        def __init__(self, **_kwargs: Any) -> None:
            calls.append("managed")

        def __enter__(self) -> Credential:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def get_token(self, scope: str) -> Any:
            assert scope == adapters.TOKEN_SCOPE
            return type("Token", (), {"token": "invented-token"})()

    def forbidden_default() -> None:
        raise AssertionError("hosted execution must not use DefaultAzureCredential")

    monkeypatch.setattr("azure.identity.ManagedIdentityCredential", Credential)
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", forbidden_default)
    monkeypatch.setenv("AI_BENCH_RUNTIME", "azure")
    assert adapters.entra_token() == "invented-token"
    assert calls == ["managed"]


def configure_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    values = provider_configuration().model_dump()
    monkeypatch.setenv("AI_BENCH_RUNTIME", "azure")
    monkeypatch.setenv("AI_BENCH_AUTH_MODE", "entra")
    monkeypatch.delenv("AI_BENCH_SPEECH_PROVIDER_CONFIG", raising=False)
    for field, environment in HOSTED_SETTINGS.items():
        monkeypatch.setenv(environment, str(values[field]))


def test_hosted_configuration_requires_all_actual_app_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_hosted(monkeypatch)
    assert load_configuration() == provider_configuration()
    for environment in HOSTED_SETTINGS.values():
        with monkeypatch.context() as missing:
            missing.delenv(environment)
            with pytest.raises(ValueError, match="all five"):
                load_configuration()


@pytest.mark.parametrize("mode", ["key", "offline", "unknown"])
def test_hosted_authentication_cannot_fall_back(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    configure_hosted(monkeypatch)
    monkeypatch.setenv("AI_BENCH_AUTH_MODE", mode)
    with pytest.raises(ValueError, match="Entra"):
        load_configuration()
    with pytest.raises(ValueError, match="Entra"):
        adapters.entra_token()


def test_hosted_rejects_json_even_when_endpoints_are_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_hosted(monkeypatch)
    monkeypatch.setenv("AI_BENCH_SPEECH_PROVIDER_CONFIG", "must-not-be-read.json")
    with pytest.raises(ValueError, match="fallback"):
        load_configuration()
    with pytest.raises(ValueError, match="fallback"):
        ManagedTranscriptionSolution(
            "gpt-transcribe", configuration=provider_configuration()
        ).validate_execution()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.openai.azure.com",
        "https://example.invalid",
        "https://example.openai.azure.com/path",
        "https://example.openai.azure.com?query=value",
        "https://example.openai.azure.com#fragment",
        "https://username@example.openai.azure.com",
        "https://example.openai.azure.com:444",
        "https://bad..openai.azure.com",
        "https://-bad.openai.azure.com",
        "https://example.openai.azure.com\n",
    ],
)
def test_hosted_endpoints_are_validated_together(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    configure_hosted(monkeypatch)
    monkeypatch.setenv("AI_BENCH_MAI_ENDPOINT", endpoint)
    with pytest.raises(ValueError, match="invalid"):
        load_configuration()


@pytest.mark.parametrize("change", ["mime", "length", "hash", "reference", "corrupt"])
def test_invalid_audio_request_cannot_obtain_credentials_or_send(change: str) -> None:
    request = audio_request()
    sample = request.sample
    if change == "reference":
        sample = sample.model_copy(
            update={
                "reference": TextArtifact(
                    id="0" * 64,
                    modality=Modality.TEXT,
                    media_type="text/plain",
                    size_bytes=6,
                    uri="artifact://sha256/" + "0" * 64,
                    metadata={"text": "SECRET"},
                )
            }
        )
    else:
        updates: dict[str, Any] = {}
        if change == "mime":
            updates["media_type"] = "text/plain"
        elif change == "length":
            updates["size_bytes"] = 1
        elif change == "hash":
            updates["id"] = "0" * 64
        else:
            request = request.model_copy(update={"input_bytes": b"corrupt"})
            updates = {"id": content_digest(b"corrupt"), "size_bytes": 7}
        sample = sample.model_copy(update={"input": sample.input.model_copy(update=updates)})
    request = request.model_copy(update={"sample": sample})

    def forbidden() -> str:
        raise AssertionError("Invalid input must fail before any token or provider I/O")

    solution = ManagedTranscriptionSolution(
        "gpt-transcribe", provider_configuration(), token_provider=forbidden
    )
    with pytest.raises(ValueError):
        solution.execute(request)


def test_transcription_redirect_is_never_followed() -> None:
    calls: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://untrusted.invalid"})

    solution = ManagedTranscriptionSolution(
        "gpt-transcribe",
        provider_configuration(),
        HttpxTransport(httpx.MockTransport(redirect)),
        token_provider=lambda: "invented-token",
    )
    with pytest.raises(ProviderFailure) as caught:
        solution.execute(audio_request())
    assert len(calls) == 1
    assert caught.value.evidence.http_status == 307
    assert caught.value.evidence.retryable is False
