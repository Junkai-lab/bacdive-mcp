"""About ten questions checked against live BacDive, SPARQL, and NCBI.

Run with: pytest -o addopts= -m live
"""

from __future__ import annotations

import pytest

from bacdive_mcp.http import PoliteHttp
from bacdive_mcp.service import BacdiveService

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def service() -> BacdiveService:
    return BacdiveService(PoliteHttp.from_env())


def test_strain_24493_is_phaeobacter_dsm_26640_gram_negative(service: BacdiveService) -> None:
    strain = service.get_strain(24493)
    assert strain["name"]["species"] == "Phaeobacter gallaeciensis"
    assert strain["dsm_number"] == 26640
    assert strain["page"] == "https://bacdive.dsmz.de/strain/24493"
    assert strain["morphology"]["gram_stain"]["gram_stain"] == "negative"
    assert strain["morphology"]["gram_stain"]["value_kind"] == "measured"
    assert "ATCC 700781" in strain["collection_numbers"]
    assert "CIP 105210" in strain["collection_numbers"]


def test_predictions_for_24493_are_labelled_predictions(service: BacdiveService) -> None:
    growth = service.get_growth_conditions(24493)
    predicted = {row["trait"]: row for row in growth["predicted"]["values"]}
    assert predicted["anaerobic"]["prediction"] == "no"
    assert predicted["anaerobic"]["value_kind"] == "predicted"
    assert isinstance(predicted["anaerobic"]["confidence"], (int, float))
    assert growth["measured"]["oxygen"] == []
    assert all(row["value_kind"] == "measured" for row in growth["measured"]["temperature"])
    temperatures = {row["temperature"]: row["growth"] for row in growth["measured"]["temperature"]}
    assert temperatures["25"] == "positive"
    assert temperatures["37"] == "negative"
    assert growth["measured"]["optimum_temperature"] == []


def test_collection_number_dsm_26640(service: BacdiveService) -> None:
    found = service.lookup_by_collection_number("DSM 26640")
    assert found["total_count"] == 1
    assert found["results"][0]["bacdive_id"] == 24493
    assert found["results"][0]["page"].endswith("/24493")


def test_16s_af000162_is_lacticaseibacillus(service: BacdiveService) -> None:
    found = service.lookup_by_sequence("AF000162", sequence_type="16s")
    hit = found["hits"][0]["results"][0]
    assert hit["bacdive_id"] == 6583
    assert "manihotivorans" in hit["species"].lower()
    strain = service.get_strain(6583)
    accessions = [item["accession"] for item in strain["sequences_16s"]]
    assert "AF000162" in accessions


def test_genome_gca_000511385_is_24493(service: BacdiveService) -> None:
    found = service.lookup_by_sequence("GCA_000511385", sequence_type="genome")
    assert found["hits"][0]["results"][0]["bacdive_id"] == 24493


def test_16s_y13244_is_24493(service: BacdiveService) -> None:
    found = service.lookup_by_sequence("Y13244", sequence_type="16s")
    assert found["hits"][0]["results"][0]["bacdive_id"] == 24493


def test_citation_has_strain_doi_and_a_reference(service: BacdiveService) -> None:
    citation = service.get_citation(24493)
    assert citation["strain_doi"].startswith("10.13145/bacdive24493.")
    assert citation["page"] == "https://bacdive.dsmz.de/strain/24493"
    assert citation["bacdive_database_citation"].startswith("Schober I")
    assert "10.1093/nar/gkae959" in citation["bacdive_database_citation"]
    assert any(ref.get("doi_or_url") for ref in citation["references"])
    assert citation["literature"]
    for paper in citation["literature"]:
        assert "bacdive_reference_id" in paper
        assert "link" in paper
        doi = paper.get("doi")
        if isinstance(doi, str) and doi.strip().lower().startswith("10."):
            assert paper["link"].startswith("https://doi.org/")
        if paper["bacdive_reference_id"] is not None:
            assert isinstance(paper["bacdive_reference_id"], int)


def test_ncbi_normalises_roseobacter_synonym(service: BacdiveService) -> None:
    result = service.normalize_taxon_name("Roseobacter gallaeciensis")
    assert result["scientific_name"] == "Phaeobacter gallaeciensis"
    assert result["ncbi_tax_id"] == "60890"
    assert result["status"] == "normalized_from_synonym"


def test_soil_and_ph_below_5_returns_recorded_rows(service: BacdiveService) -> None:
    found = service.query_by_traits(isolation_source="soil", ph_below=5, limit=5)
    assert found["results"]
    assert found["source"] == "bacdive-sparql"
    assert found["returned"] == len(found["results"])
    assert found["returned"] <= found["limit"]
    assert isinstance(found["total_matched"], int)
    assert found["total_matched"] > found["returned"]
    assert found["truncated"] is True
    for row in found["results"]:
        assert row["value_kind"] == "recorded"
        assert row["ph_start"] < 5
        assert "soil" in row["sources"].lower()
        assert row["bacdive_id"]
        assert row["page"].endswith(str(row["bacdive_id"]))
        assert "confidence" not in row


def test_e_coli_k12_mg1655_has_growth_37_and_no_invented_optimum(service: BacdiveService) -> None:
    found = service.lookup_by_collection_number("DSM 18039")
    assert found["results"][0]["bacdive_id"] == 4902
    assert "coli" in found["results"][0]["species"].lower()
    growth = service.get_growth_conditions(4902)
    assert "K12" in (growth["strain_designation"] or "").replace("-", "")
    measured = growth["measured"]["temperature"]
    assert any(row["temperature"] == "37" and row["growth"] == "positive" for row in measured)
    assert all(row["value_kind"] == "measured" for row in measured)
    assert growth["measured"]["optimum_temperature"] == []
    assert any("optimum temperature" in note.lower() for note in growth["notes"])


def test_partial_genus_search_finds_phaeobacter(service: BacdiveService) -> None:
    found = service.search_strains("Phaeo", match="startswith", limit=5)
    assert found["source"] == "bacdive-sparql"
    assert found["results"]
    assert any("Phaeobacter" in (row.get("species") or "") or row["bacdive_id"] for row in found["results"])
