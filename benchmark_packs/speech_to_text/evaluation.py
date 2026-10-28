"""Versioned English normalization and deterministic word-error evidence."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from decimal import Decimal
from typing import Final, Literal

from pydantic import Field

from ai_bench.contracts import (
    AggregateEvaluation,
    BenchmarkSample,
    Contract,
    EvaluationRecord,
    EvaluationResult,
    MetricDefinition,
    MetricFamily,
    MetricValue,
    ParsedOutput,
)
from ai_bench.hashing import canonical_json, content_digest

NORMALIZATION_VERSION: Final = "librispeech-english-v1"


class Transcript(Contract):
    schema_version: Literal["transcript-v1"] = "transcript-v1"
    text: str = Field(max_length=65536)


class AlignmentWord(Contract):
    operation: Literal["match", "substitution", "deletion", "insertion"]
    reference: str | None
    hypothesis: str | None


class TranscriptComparison(Contract):
    schema_version: Literal["1"] = "1"
    kind: Literal["transcript-alignment"] = "transcript-alignment"
    normalization_version: Literal["librispeech-english-v1"] = NORMALIZATION_VERSION
    reference_text: str
    hypothesis_text: str
    reference_tokens: tuple[str, ...]
    hypothesis_tokens: tuple[str, ...]
    substitutions: int
    deletions: int
    insertions: int
    reference_words: int
    word_error_rate: Decimal
    alignment: tuple[AlignmentWord, ...]


def normalize_english(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = normalized.translate({code: "'" for code in (0x2018, 0x2019, 0x02BC, 0xFF07)})
    return tuple(re.findall(r"[^\W_]+(?:'[^\W_]+)*", normalized))


def align_words(
    reference: tuple[str, ...], hypothesis: tuple[str, ...]
) -> tuple[AlignmentWord, ...]:
    rows, columns = len(reference) + 1, len(hypothesis) + 1
    costs = [[0] * columns for _ in range(rows)]
    choices = [["match"] * columns for _ in range(rows)]
    for index in range(1, rows):
        costs[index][0], choices[index][0] = index, "deletion"
    for index in range(1, columns):
        costs[0][index], choices[0][index] = index, "insertion"
    for row in range(1, rows):
        for column in range(1, columns):
            match = reference[row - 1] == hypothesis[column - 1]
            cost, _, operation = min(
                (
                    costs[row - 1][column - 1] + (not match),
                    0 if match else 1,
                    "match" if match else "substitution",
                ),
                (costs[row - 1][column] + 1, 2, "deletion"),
                (costs[row][column - 1] + 1, 3, "insertion"),
            )
            costs[row][column], choices[row][column] = cost, operation
    result: list[AlignmentWord] = []
    row, column = len(reference), len(hypothesis)
    while row or column:
        operation = choices[row][column]
        result.append(
            AlignmentWord.model_validate(
                {
                    "operation": operation,
                    "reference": None if operation == "insertion" else reference[row - 1],
                    "hypothesis": None if operation == "deletion" else hypothesis[column - 1],
                }
            )
        )
        row -= operation != "insertion"
        column -= operation != "deletion"
    return tuple(reversed(result))


class WordErrorEvaluator:
    id = "english-word-error"
    version = "1.0.0"
    metric_definitions = (
        MetricDefinition(
            id="word_error_rate",
            name="Word error rate",
            family=MetricFamily.QUALITY,
            unit="ratio",
            higher_is_better=False,
            description=(
                "Corpus sum of substitutions, deletions and insertions divided by "
                "reference words; may exceed 100%."
            ),
        ),
        *(
            MetricDefinition(
                id=identifier,
                name=name,
                family=MetricFamily.QUALITY,
                unit="words",
                higher_is_better=False if identifier != "reference_words" else None,
                description=f"Evaluated-case {name.lower()}; inspect coverage separately.",
            )
            for identifier, name in (
                ("substitutions", "Substitutions"),
                ("deletions", "Deletions"),
                ("insertions", "Insertions"),
                ("reference_words", "Reference words"),
            )
        ),
    )

    def evaluate(self, sample: BenchmarkSample, output: ParsedOutput) -> EvaluationResult:
        if sample.reference is None:
            raise ValueError("Word error evaluation requires a reference.")
        reference = sample.reference.metadata.get("text")
        if (
            not isinstance(reference, str)
            or content_digest(reference.encode("utf-8")) != sample.reference.id
        ):
            raise ValueError("Reference text does not match its persisted artifact identity.")
        transcript = Transcript.model_validate_json(canonical_json(output.value))
        reference_tokens = normalize_english(reference)
        hypothesis_tokens = normalize_english(transcript.text)
        if not reference_tokens:
            raise ValueError("The normalized reference must contain at least one word.")
        if (len(reference_tokens) + 1) * (len(hypothesis_tokens) + 1) > 4_000_000:
            raise ValueError("Transcript alignment exceeds the bounded evaluation limit.")
        alignment = align_words(reference_tokens, hypothesis_tokens)
        substitutions = sum(item.operation == "substitution" for item in alignment)
        deletions = sum(item.operation == "deletion" for item in alignment)
        insertions = sum(item.operation == "insertion" for item in alignment)
        comparison = TranscriptComparison(
            reference_text=reference,
            hypothesis_text=transcript.text,
            reference_tokens=reference_tokens,
            hypothesis_tokens=hypothesis_tokens,
            substitutions=substitutions,
            deletions=deletions,
            insertions=insertions,
            reference_words=len(reference_tokens),
            word_error_rate=Decimal(substitutions + deletions + insertions) / len(reference_tokens),
            alignment=alignment,
        )
        values = {
            "word_error_rate": comparison.word_error_rate,
            "substitutions": Decimal(substitutions),
            "deletions": Decimal(deletions),
            "insertions": Decimal(insertions),
            "reference_words": Decimal(len(reference_tokens)),
        }
        return EvaluationResult(
            metrics=tuple(
                MetricValue(
                    metric_id=definition.id,
                    family=definition.family,
                    unit=definition.unit,
                    value=values[definition.id],
                )
                for definition in self.metric_definitions
            ),
            comparison=comparison.model_dump(mode="json"),
        )

    def aggregate(self, records: Sequence[EvaluationRecord]) -> AggregateEvaluation:
        comparisons = [
            TranscriptComparison.model_validate_json(canonical_json(record.comparison))
            for record in records
        ]
        substitutions = sum(item.substitutions for item in comparisons)
        deletions = sum(item.deletions for item in comparisons)
        insertions = sum(item.insertions for item in comparisons)
        words = sum(item.reference_words for item in comparisons)
        values = {
            "word_error_rate": Decimal(substitutions + deletions + insertions) / words
            if words
            else None,
            "substitutions": Decimal(substitutions) if comparisons else None,
            "deletions": Decimal(deletions) if comparisons else None,
            "insertions": Decimal(insertions) if comparisons else None,
            "reference_words": Decimal(words) if comparisons else None,
        }
        return AggregateEvaluation(
            metrics=tuple(
                MetricValue(
                    metric_id=definition.id,
                    family=definition.family,
                    unit=definition.unit,
                    value=values[definition.id],
                    unknown_reason="No successfully evaluated cases."
                    if values[definition.id] is None
                    else None,
                )
                for definition in self.metric_definitions
            )
        )
