"""Discoverable speech pilot catalog; factory performs no data or credential I/O."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ai_bench.contracts import (
    BenchmarkDefinition,
    JsonValue,
    Modality,
    ModalityArtifact,
    RendererDescriptor,
    SchedulingPolicy,
    Terminology,
)

from .adapters import ManagedTranscriptionSolution
from .dataset import SpeechDataset
from .evaluation import WordErrorEvaluator
from .pricing import SpeechPricing
from .upload import SpeechDatasetImporter


class TranscriptViewer:
    descriptor = RendererDescriptor(
        id="transcript-alignment",
        version="1.1.0",
        modality=Modality.AUDIO,
        media_types=("audio/flac", "audio/wav", "audio/mpeg", "text/plain", "application/json"),
        title="Audio and transcript evidence",
    )

    def render(self, artifact: ModalityArtifact) -> Mapping[str, JsonValue]:
        return {
            "supported": artifact.media_type in self.descriptor.media_types,
            "renderer": self.descriptor.id,
            "download": True,
        }


@dataclass(frozen=True)
class SpeechPack:
    definition: BenchmarkDefinition
    dataset: SpeechDataset
    solutions: tuple[ManagedTranscriptionSolution, ...]
    evaluator: WordErrorEvaluator
    viewer: TranscriptViewer
    dataset_importer: SpeechDatasetImporter = field(default_factory=SpeechDatasetImporter)
    pricing_provider: SpeechPricing = field(default_factory=SpeechPricing)
    scheduling_policy: SchedulingPolicy = field(default_factory=SchedulingPolicy)


def get_pack() -> SpeechPack:
    return SpeechPack(
        definition=BenchmarkDefinition(
            id="speech_to_text",
            version="1.1.0",
            name="Speech-to-Text Studio",
            description=(
                "Five managed transcription solutions on the preserved 26-sample LibriSpeech "
                "test-clean pilot or an explicitly selected immutable English upload."
            ),
            input_modalities=(Modality.AUDIO,),
            reference_modalities=(Modality.TEXT,),
            terminology=Terminology(
                benchmark_singular="transcription pilot",
                sample_singular="utterance",
                solution_singular="transcription solution",
                reference_singular="transcript",
            ),
            tags=("speech", "26-sample-pilot"),
        ),
        dataset=SpeechDataset(),
        solutions=(
            ManagedTranscriptionSolution("azure-speech-fast"),
            ManagedTranscriptionSolution("gpt-transcribe"),
            ManagedTranscriptionSolution("managed-whisper"),
            ManagedTranscriptionSolution("mai-transcribe-1-5"),
            ManagedTranscriptionSolution("mai-transcribe-2"),
        ),
        evaluator=WordErrorEvaluator(),
        viewer=TranscriptViewer(),
    )
