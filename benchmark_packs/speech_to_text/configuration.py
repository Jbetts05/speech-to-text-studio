"""Explicit, non-secret endpoint/deployment configuration; no provider clients."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from ai_bench.contracts import Contract

from .dataset import require_external

CONFIG_ENV = "AI_BENCH_SPEECH_PROVIDER_CONFIG"
HOSTED_SETTINGS = {
    "speech_endpoint": "AI_BENCH_SPEECH_ENDPOINT",
    "openai_endpoint": "AI_BENCH_OPENAI_ENDPOINT",
    "mai_endpoint": "AI_BENCH_MAI_ENDPOINT",
    "gpt_transcribe_deployment": "AI_BENCH_GPT_TRANSCRIBE_DEPLOYMENT",
    "whisper_deployment": "AI_BENCH_WHISPER_DEPLOYMENT",
}


class SpeechProviderConfiguration(Contract):
    schema_version: Literal["1"]
    speech_endpoint: str
    openai_endpoint: str
    mai_endpoint: str
    gpt_transcribe_deployment: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    whisper_deployment: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

    @field_validator("speech_endpoint", "openai_endpoint", "mai_endpoint")
    @classmethod
    def explicit_azure_endpoint(cls, endpoint: str) -> str:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or endpoint != endpoint.strip()
            or any(ord(character) < 33 or ord(character) > 126 for character in endpoint)
            or not parsed.hostname
            or re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                r"\.(?:cognitiveservices\.azure\.com|openai\.azure\.com|services\.ai\.azure\.com)",
                parsed.hostname,
            )
            is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.port not in {None, 443}
            or not parsed.hostname.endswith(
                (
                    ".cognitiveservices.azure.com",
                    ".openai.azure.com",
                    ".services.ai.azure.com",
                )
            )
        ):
            raise ValueError(
                "Use an explicit HTTPS Azure AI account endpoint, "
                "without credentials or query parameters."
            )
        return endpoint.rstrip("/")


def load_configuration() -> SpeechProviderConfiguration:
    runtime = os.getenv("AI_BENCH_RUNTIME", "local").lower()
    if runtime not in {"local", "azure"}:
        raise ValueError("AI_BENCH_RUNTIME must be local or azure.")
    if runtime == "azure":
        if os.getenv("AI_BENCH_AUTH_MODE", "entra").lower() != "entra":
            raise ValueError("Hosted speech providers require Entra managed identity.")
        if os.getenv(CONFIG_ENV):
            raise ValueError("Hosted speech providers reject local configuration file fallback.")
        values = {field: os.getenv(setting) for field, setting in HOSTED_SETTINGS.items()}
        if not all(values.values()):
            raise ValueError("Hosted speech requires all five explicit provider app settings.")
        try:
            return SpeechProviderConfiguration.model_validate({"schema_version": "1", **values})
        except ValueError as error:
            raise ValueError("Hosted speech provider app settings are invalid.") from error
    configured = os.getenv(CONFIG_ENV)
    if not configured:
        raise ValueError(f"Set {CONFIG_ENV} to an external Entra-only provider configuration file.")
    path = require_external(Path(configured))
    if not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("Provider configuration is missing or exceeds its size limit.")
    try:
        return SpeechProviderConfiguration.model_validate_json(path.read_bytes())
    except ValueError as error:
        raise ValueError(
            "Provider configuration is invalid; "
            "supply explicit endpoints and deployment names only."
        ) from error
