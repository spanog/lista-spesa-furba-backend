"""Unit tests for services/product_format.py."""

from __future__ import annotations

import sys
import os
import types
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

for _mod in ("supabase", "jose", "jose.jwt"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

_config_mod = types.ModuleType("core.config")
_config_mod.settings = MagicMock()  # type: ignore[attr-defined]
sys.modules["core.config"] = _config_mod
sys.modules["core.database"] = MagicMock()

import pytest

from services.product_format import (
    NormalizedFormatBundle,
    build_format_bundle,
    explode_format_variants,
    format_to_key,
    format_to_label,
    normalize_format,
)


class TestNormalizeFormat:
    def test_normalizes_single_pack_units(self):
        result = normalize_format({
            "tipo": "confezione_singola",
            "peso_volume": 500,
            "unita_misura": "gr",
        })
        assert result["tipo"] == "confezione_singola"
        assert result["unita_misura"] == "g"

    def test_rejects_plain_text(self):
        with pytest.raises(ValueError, match="Plain text product format"):
            normalize_format("500g")

    def test_single_pack_accepts_unknown_measure(self):
        result = normalize_format({"tipo": "confezione_singola"})

        assert result["tipo"] == "confezione_singola"

    def test_single_pack_accepts_optional_piece_count(self):
        result = normalize_format({
            "tipo": "confezione_singola",
            "num_pezzi": 24,
            "unita_misura": "pezzo",
        })

        assert result["tipo"] == "confezione_singola"
        assert result["num_pezzi"] == 24
        assert result["unita_misura"] is None

    def test_single_pack_rejects_incomplete_weight_measure(self):
        with pytest.raises(ValueError, match="Missing required format fields"):
            normalize_format({"tipo": "confezione_singola", "peso_volume": 500})


class TestFormatKey:
    def test_equivalent_payloads_share_same_key(self):
        left = {"tipo": "confezione_singola", "peso_volume": 1, "unita_misura": "l"}
        right = {"tipo": "confezione_singola", "peso_volume": 1.0, "unita_misura": "L"}
        assert format_to_key(left) == format_to_key(right)

    def test_bundle_reuses_canonical_key(self):
        bundle = build_format_bundle({
            "tipo": "confezione_singola",
            "peso_volume": 500,
            "unita_misura": "g",
        })
        assert format_to_key(bundle) == bundle.format_key


class TestFormatLabel:
    def test_renders_bulk_label(self):
        assert format_to_label({"tipo": "sfuso", "unita_sfuso": "kg"}) == "al kg"

    def test_renders_single_pack_label(self):
        assert format_to_label({
            "tipo": "confezione_singola",
            "peso_volume": 500,
            "unita_misura": "g",
        }) == "500 g"

    def test_renders_single_pack_piece_label(self):
        assert format_to_label({
            "tipo": "confezione_singola",
            "num_pezzi": 24,
        }) == "24 pezzi"

    def test_renders_unknown_single_pack_label_as_empty(self):
        assert format_to_label({"tipo": "confezione_singola"}) == ""

    def test_bundle_reuses_canonical_label(self):
        bundle = build_format_bundle({"tipo": "sfuso", "unita_sfuso": "kg"})
        assert format_to_label(bundle) == bundle.format_label


class TestBuildFormatBundle:
    def test_sparse_and_full_payloads_share_same_canonical_bundle(self):
        sparse = build_format_bundle({
            "tipo": "confezione_singola",
            "peso_volume": 500,
            "unita_misura": "gr",
        })
        full = build_format_bundle({
            "tipo": "confezione_singola",
            "quantita": None,
            "peso_volume": 500,
            "unita_misura": "g",
            "peso_approssimativo": False,
            "peso_sgocciolato": None,
            "unita_misura_sgocciolato": None,
            "num_pezzi": None,
            "peso_volume_totale": None,
            "num_lavaggi": None,
            "quantita_totale": None,
            "unita_misura_quantita": None,
            "peso_volume_min": None,
            "peso_volume_max": None,
            "num_rotoli": None,
            "num_veli": None,
            "num_strappi_per_rotolo": None,
            "num_fogli_totali": None,
            "quantita_omaggio": None,
            "unita_sfuso": None,
            "componenti": None,
            "varianti": None,
        })

        assert sparse.format_normalized == full.format_normalized
        assert sparse.format_compact == full.format_compact == {
            "tipo": "confezione_singola",
            "peso_volume": 500.0,
            "unita_misura": "g",
        }
        assert sparse.format_key == full.format_key
        assert sparse.format_label == full.format_label == "500 g"

    def test_compact_format_omits_defaults(self):
        bundle = build_format_bundle({
            "tipo": "confezione_singola",
            "peso_volume": 500,
            "unita_misura": "g",
        })

        assert bundle.format_compact == {
            "tipo": "confezione_singola",
            "peso_volume": 500.0,
            "unita_misura": "g",
        }
        assert "peso_approssimativo" not in bundle.format_compact

    def test_single_pack_piece_count_compacts_without_piece_unit(self):
        bundle = build_format_bundle({
            "tipo": "confezione_singola",
            "peso_volume": None,
            "unita_misura": "pezzo",
            "num_pezzi": 24,
        })

        assert bundle.format_compact == {
            "tipo": "confezione_singola",
            "num_pezzi": 24,
        }
        assert bundle.format_key == 'v1:{"num_pezzi":24,"tipo":"confezione_singola"}'
        assert bundle.format_label == "24 pezzi"


class TestExplodeVariants:
    def test_explodes_each_variant_into_child_product(self):
        products = explode_format_variants({
            "name": "Sfoglie Gran Pavesi",
            "brand": "Gran Pavesi",
            "format": {
                "varianti": [
                    {
                        "nome_variante": "classiche",
                        "formato": {
                            "tipo": "confezione_singola",
                            "peso_volume": 180,
                            "unita_misura": "g",
                        },
                    },
                    {
                        "nome_variante": "mais lime e pepe",
                        "formato": {
                            "tipo": "confezione_singola",
                            "peso_volume": 150,
                            "unita_misura": "g",
                        },
                    },
                ],
            },
        })
        assert [product["name"] for product in products] == [
            "Sfoglie Gran Pavesi classiche",
            "Sfoglie Gran Pavesi mais lime e pepe",
        ]
        assert products[0]["format"]["peso_volume"] == 180
        assert products[1]["format"]["peso_volume"] == 150

    def test_attaches_bundle_and_keeps_compact_format(self):
        [product] = explode_format_variants({
            "name": "Pasta Barilla",
            "brand": "Barilla",
            "format": {
                "tipo": "confezione_singola",
                "peso_volume": 500,
                "unita_misura": "g",
                "peso_approssimativo": False,
            },
        })

        assert product["format"] == {
            "tipo": "confezione_singola",
            "peso_volume": 500.0,
            "unita_misura": "g",
        }
        assert isinstance(product["_format_bundle"], NormalizedFormatBundle)
