"""Contract validation for the bundled synthetic pack."""

from ai_bench.packs import validate_pack

from .pack import PACK


def test_pack_contract() -> None:
    validate_pack(PACK)
