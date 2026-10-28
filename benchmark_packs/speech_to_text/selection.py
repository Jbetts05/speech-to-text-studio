"""Selection depends only on duration, identity, and already-selected coverage."""

from __future__ import annotations

from collections import Counter

from ai_bench.hashing import sha256_digest

from .data_contracts import (
    PILOT_SIZE,
    SELECTOR_SEED,
    UTTERANCES,
    Inventory,
    PilotSelection,
    SelectedUtterance,
    SelectionCoverage,
)


def select_pilot(inventory: Inventory) -> PilotSelection:
    ordered = sorted(inventory.entries, key=lambda entry: (entry.duration, entry.id))
    boundaries = [index * UTTERANCES // PILOT_SIZE for index in range(PILOT_SIZE + 1)]
    selected: dict[int, int] = {}
    speakers: Counter[str] = Counter()
    chapters: Counter[tuple[str, str]] = Counter()

    def choose(stratum: int, rank: int) -> None:
        entry = ordered[rank]
        selected[stratum] = rank
        speakers[entry.speaker] += 1
        chapters[(entry.speaker, entry.chapter)] += 1

    for rank in (0, 1309, 2619):
        stratum = next(
            index
            for index in range(PILOT_SIZE)
            if boundaries[index] <= rank < boundaries[index + 1]
        )
        choose(stratum, rank)

    for stratum in range(PILOT_SIZE):
        if stratum in selected:
            continue
        rank = min(
            range(boundaries[stratum], boundaries[stratum + 1]),
            key=lambda index: (
                speakers[ordered[index].speaker],
                chapters[(ordered[index].speaker, ordered[index].chapter)],
                sha256_digest(
                    {
                        "seed": SELECTOR_SEED,
                        "release": inventory.release,
                        "id": ordered[index].id,
                    }
                ),
            ),
        )
        choose(stratum, rank)

    entries = [ordered[selected[stratum]] for stratum in range(PILOT_SIZE)]
    return PilotSelection(
        inventory_hash=sha256_digest(inventory),
        selected=tuple(
            SelectedUtterance(
                id=entry.id,
                rank=selected[stratum],
                stratum=stratum,
                input_hash=entry.input_hash,
                reference_hash=entry.reference_hash,
            )
            for stratum, entry in enumerate(entries)
        ),
        coverage=SelectionCoverage(
            speakers=len(speakers),
            chapters=len(chapters),
            total_frames=sum(entry.frames for entry in entries),
            minimum_frames=min(entry.frames for entry in entries),
            maximum_frames=max(entry.frames for entry in entries),
        ),
    )
