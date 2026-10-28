from __future__ import annotations

from decimal import Decimal

import pytest

from ai_bench.contracts import (
    BenchmarkSample,
    EvaluationRecord,
    Modality,
    ParsedOutput,
    TextArtifact,
)
from ai_bench.hashing import content_digest
from benchmark_packs.speech_to_text.evaluation import (
    Transcript,
    WordErrorEvaluator,
    align_words,
    normalize_english,
)
from tests.test_speech_adapters import audio_request


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("HELLO, World!", ("hello", "world")),
        ("  one\tTWO\n three  ", ("one", "two", "three")),
        ("DON'T CAN'T WE'RE", ("don't", "can't", "we're")),
        ("DON\u2019T", ("don't",)),
        ("'quoted' dogs' dog\u2019s", ("quoted", "dogs", "dog's")),
        ("twenty-one 21 21.5", ("twenty", "one", "21", "21", "5")),
        ("Mr. Gray & Ms. Blue", ("mr", "gray", "ms", "blue")),
        ("", ()),
        ("... -- !", ()),
    ],
)
def test_versioned_english_normalization(text: str, tokens: tuple[str, ...]) -> None:
    assert normalize_english(text) == tokens
    assert normalize_english(" ".join(tokens)) == tokens


def sample_with_reference(text: str) -> BenchmarkSample:
    content = text.encode("utf-8")
    return audio_request().sample.model_copy(
        update={
            "reference": TextArtifact(
                id=content_digest(content),
                modality=Modality.TEXT,
                media_type="text/plain",
                size_bytes=len(content),
                uri=f"artifact://sha256/{content_digest(content)}",
                metadata={"text": text},
            ),
        }
    )


def evaluate(reference: str, hypothesis: str) -> EvaluationRecord:
    result = WordErrorEvaluator().evaluate(
        sample_with_reference(reference),
        ParsedOutput(value=Transcript(text=hypothesis).model_dump(mode="json")),
    )
    return EvaluationRecord(
        sample_id="synthetic",
        solution_id="synthetic",
        metrics=result.metrics,
        comparison=result.comparison,
    )


def test_alignment_has_explicit_deterministic_tie_break() -> None:
    alignment = align_words(("a", "b"), ("b", "a"))
    assert [item.operation for item in alignment] == ["substitution", "substitution"]
    assert alignment == align_words(("a", "b"), ("b", "a"))
    assert [item.operation for item in align_words(("a",), ("a",))] == ["match"]


def test_corpus_wer_uses_reference_word_weighting_and_can_exceed_one() -> None:
    short = evaluate("ONE", "TWO THREE FOUR")
    long = evaluate(
        "ONE TWO THREE FOUR FIVE SIX SEVEN EIGHT NINE",
        "ONE TWO THREE FOUR FIVE SIX SEVEN EIGHT NINE",
    )
    aggregate = WordErrorEvaluator().aggregate((short, long))
    values = {metric.metric_id: metric.value for metric in aggregate.metrics}
    assert values["word_error_rate"] == Decimal("0.3")
    assert values["substitutions"] == 1 and values["insertions"] == 2
    assert short.metrics[0].value == 3
    assert short.metrics[0].value != aggregate.metrics[0].value


def test_successful_empty_hypothesis_is_all_deletions() -> None:
    record = evaluate("ONE TWO THREE", "")
    values = {metric.metric_id: metric.value for metric in record.metrics}
    assert values["word_error_rate"] == 1
    assert values["deletions"] == 3
    assert isinstance(record.comparison, dict)
    assert record.comparison["hypothesis_tokens"] == []
    assert record.comparison["reference_tokens"] == ["one", "two", "three"]
    assert WordErrorEvaluator().aggregate(()).metrics[0].value is None


def test_numerals_and_contractions_are_not_guessed() -> None:
    assert evaluate("TWENTY ONE", "21").metrics[0].value == 1
    assert evaluate("DO NOT", "don't").metrics[0].value == 1


def test_empty_normalized_reference_is_invalid() -> None:
    with pytest.raises(ValueError, match="at least one word"):
        evaluate("...", "one")
