"""Unit tests for services/extraction/normalizer.py"""

import sys
import os
import types
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Stub infrastructure modules
for _mod in ("supabase", "jose", "jose.jwt"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_config_mod = types.ModuleType("core.config")
_config_mod.settings = MagicMock()  # type: ignore[attr-defined]
sys.modules["core.config"] = _config_mod
sys.modules["core.database"] = MagicMock()

import pytest
from services.extraction.normalizer import (
    normalize_category,
    normalize_brand,
    normalize_price,
    calculate_discount_pct,
    normalize_unit_price_measure,
    deduplicate_products,
    normalize_product,
)


def _single_format(weight: float, unit: str = "g") -> dict:
    return {
        "tipo": "confezione_singola",
        "peso_volume": weight,
        "unita_misura": unit,
    }


# ---------------------------------------------------------------------------
# normalize_category
# ---------------------------------------------------------------------------

class TestNormalizeCategory:
    def test_valid_enum_value_passthrough(self):
        assert normalize_category("alimentari-freschi") == "alimentari-freschi"
        assert normalize_category("dispensa") == "dispensa"
        assert normalize_category("surgelati") == "surgelati"

    def test_alias_mapping(self):
        assert normalize_category("frutta") == "alimentari-freschi"
        assert normalize_category("carne") == "alimentari-freschi"
        assert normalize_category("latticini") == "alimentari-freschi"
        assert normalize_category("pane") == "alimentari-freschi"
        assert normalize_category("surgelato") == "surgelati"
        assert normalize_category("bibite") == "bevande"
        assert normalize_category("pasta") == "dispensa"
        assert normalize_category("igiene") == "cura-persona-salute"
        assert normalize_category("pulizia") == "cura-casa"
        assert normalize_category("pet") == "prodotti-animali"

    def test_none_returns_altro(self):
        assert normalize_category(None) == "altro"

    def test_unknown_returns_altro(self):
        assert normalize_category("xyz-random-category") == "altro"

    def test_case_insensitive_enum(self):
        assert normalize_category("ALIMENTARI-FRESCHI") == "alimentari-freschi"

    def test_empty_string_returns_altro(self):
        assert normalize_category("") == "altro"


# ---------------------------------------------------------------------------
# normalize_brand
# ---------------------------------------------------------------------------

class TestNormalizeBrand:
    def test_none_returns_none(self):
        assert normalize_brand(None) is None

    def test_empty_returns_none(self):
        assert normalize_brand("") is None

    def test_title_cased(self):
        assert normalize_brand("barilla") == "Barilla"
        assert normalize_brand("marca sconosciuta") == "Marca Sconosciuta"
        assert normalize_brand("rio mare") == "Rio Mare"

    def test_strips_whitespace(self):
        assert normalize_brand("  barilla  ") == "Barilla"


# ---------------------------------------------------------------------------
# normalize_price
# ---------------------------------------------------------------------------

class TestNormalizePrice:
    def test_float_passthrough(self):
        assert normalize_price(4.99) == pytest.approx(4.99)

    def test_integer_input(self):
        assert normalize_price(5) == pytest.approx(5.0)

    def test_string_with_comma(self):
        assert normalize_price("1,99") == pytest.approx(1.99)

    def test_string_with_euro_symbol(self):
        assert normalize_price("€ 2.49") == pytest.approx(2.49)

    def test_string_with_euro_and_comma(self):
        assert normalize_price("€1,49") == pytest.approx(1.49)

    def test_none_returns_none(self):
        assert normalize_price(None) is None

    def test_zero_returns_none(self):
        assert normalize_price(0) is None

    def test_negative_returns_none(self):
        assert normalize_price(-1.0) is None


# ---------------------------------------------------------------------------
# normalize_unit_price_measure
# ---------------------------------------------------------------------------

class TestNormalizeUnitPriceMeasure:
    def test_normalizes_kg(self):
        assert normalize_unit_price_measure("Kg") == "kg"

    def test_normalizes_liters(self):
        assert normalize_unit_price_measure("L") == "l"

    def test_normalizes_drained_weight(self):
        assert normalize_unit_price_measure("kg sgocc.") == "kg sgocc"

    def test_unknown_measure_returns_none(self):
        assert normalize_unit_price_measure("pezzo") is None


# ---------------------------------------------------------------------------
# calculate_discount_pct
# ---------------------------------------------------------------------------

class TestCalculateDiscountPct:
    def test_simple_discount(self):
        assert calculate_discount_pct(10.0, 7.0) == 30

    def test_no_original_returns_none(self):
        assert calculate_discount_pct(None, 7.0) is None

    def test_offer_equals_original(self):
        assert calculate_discount_pct(5.0, 5.0) is None

    def test_offer_greater_than_original(self):
        assert calculate_discount_pct(3.0, 5.0) is None

    def test_zero_original_returns_none(self):
        assert calculate_discount_pct(0, 2.0) is None


# ---------------------------------------------------------------------------
# deduplicate_products
# ---------------------------------------------------------------------------

class TestDeduplicateProducts:
    def _p(self, name: str, brand: str = "") -> dict:
        return {
            "name": name,
            "brand": brand,
            "format": _single_format(500),
            "format_key": "v1:{}",
            "price_offer": 1.0,
        }

    def test_no_duplicates(self):
        products = [self._p("Mela"), self._p("Pera")]
        assert len(deduplicate_products(products)) == 2

    def test_exact_duplicates_removed(self):
        products = [self._p("Mela"), self._p("Mela")]
        assert len(deduplicate_products(products)) == 1

    def test_keeps_first_occurrence(self):
        p1 = {"name": "Latte", "brand": "Parmalat", "format": _single_format(1, "L"), "format_key": "v1:1L", "price_offer": 1.0}
        p2 = {"name": "Latte", "brand": "Parmalat", "format": _single_format(1, "L"), "format_key": "v1:1L", "price_offer": 1.5}
        result = deduplicate_products([p1, p2])
        assert len(result) == 1
        assert result[0]["price_offer"] == 1.0

    def test_case_insensitive_dedup(self):
        products = [self._p("MELA"), self._p("mela")]
        assert len(deduplicate_products(products)) == 1

    def test_same_name_brand_different_format_deduped(self):
        # Format is an offer attribute — same name+brand = same product regardless of format
        p1 = {"name": "Latte", "brand": "Granarolo", "format": _single_format(1, "L"), "format_key": "v1:1L", "price_offer": 1.0}
        p2 = {"name": "Latte", "brand": "Granarolo", "format": _single_format(500, "ml"), "format_key": "v1:500ml", "price_offer": 0.6}
        assert len(deduplicate_products([p1, p2])) == 1


# ---------------------------------------------------------------------------
# normalize_product
# ---------------------------------------------------------------------------

class TestNormalizeProduct:
    def test_full_pipeline(self):
        raw = {
            "name": "Petto di pollo  ",
            "brand": "barilla",
            "category": "carne",
            "format": _single_format(500),
            "price_offer": "3,99",
            "price_original": "5,49",
            "offer_notes": None,
            "valid_from": "2026-04-07",
            "valid_to": "2026-04-13",
        }
        result = normalize_product(raw)
        assert result["name"] == "Petto di pollo"
        assert result["brand"] == "Barilla"
        assert result["category"] == "alimentari-freschi"
        assert result["format"]["tipo"] == "confezione_singola"
        assert result["format_label"] == "500 g"
        assert result["price_offer"] == pytest.approx(3.99)
        assert result["price_original"] == pytest.approx(5.49)
        assert result["discount_pct"] == 27  # (5.49 - 3.99) / 5.49 ≈ 27%

    def test_missing_optional_fields_become_none(self):
        raw = {"name": "Pane", "price_offer": 1.5}
        result = normalize_product(raw)
        assert result["brand"] is None
        assert result["format"] == {}
        assert result["format_label"] == ""

    def test_prompt_v2_fields_are_accepted_and_mapped(self):
        raw = {
            "name": "Filetti di tonno",
            "brand": "rio mare",
            "category_main": "Dispensa",
            "category_sub": "Conserve Ittiche e di Carne",
            "format": {
                "tipo": "multipack_omogeneo",
                "quantita": 2,
                "peso_volume": 80,
                "unita_misura": "g",
            },
            "price_current": "4,99",
            "price_original": "6,79",
            "discount_percentage": 26,
            "price_per_unit": "31,19",
            "price_per_unit_measure": "Kg",
            "offer_notes": "Solo carta",
            "valid_from": "2026-04-07",
            "valid_to": "2026-04-13",
        }

        result = normalize_product(raw)

        assert result["brand"] == "Rio Mare"
        assert result["category"] == "dispensa"
        assert result["subcategory"] == "Conserve Ittiche e di Carne"
        assert result["price_offer"] == pytest.approx(4.99)
        assert result["price_original"] == pytest.approx(6.79)
        assert result["discount_pct"] == 26
        assert result["unit_price_value"] == pytest.approx(31.19)
        assert result["unit_price_unit"] == "kg"
        assert result["format_label"] == "2x80 g"

    def test_prompt_v2_liter_unit_is_lowercased_for_offers(self):
        raw = {
            "name": "Detersivo liquido",
            "category_main": "Cura della Casa",
            "format": {
                "tipo": "confezione_singola",
                "peso_volume": 1,
                "unita_misura": "L",
            },
            "price_current": "2,12",
            "price_per_unit": "2,12",
            "price_per_unit_measure": "L",
        }

        result = normalize_product(raw)

        assert result["unit_price_unit"] == "l"
        assert result["unit_price"] == "2,12 €/l"

    def test_plain_text_format_is_rejected(self):
        with pytest.raises(ValueError, match="Plain text product format"):
            normalize_product({
                "name": "Pasta",
                "format": "500g",
                "price_offer": 1.0,
            })

    def test_single_pack_with_unknown_measure_keeps_compact_format(self):
        result = normalize_product({
            "name": "Pasta",
            "format": {"tipo": "confezione_singola"},
            "price_offer": 1.0,
        })

        assert result["format"] == {"tipo": "confezione_singola"}
        assert result["format_label"] == ""
        assert result["format_key"] == 'v1:{"tipo":"confezione_singola"}'
