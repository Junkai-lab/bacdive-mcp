"""Turn BacDive records into short, sourced summaries.

Measured fields and genome-based predictions are never written into the same list.
"""

from __future__ import annotations

import re
from typing import Any

BACDIVE_PAGE = "https://bacdive.dsmz.de/strain/{bacdive_id}"
BACDIVE_CITATION = (
    "Schober I, Koblitz J, Sardà Carbasse J, Ebeling C, Schmidt ML, Podstawka A, "
    "Gupta R, Ilangovan V, Chamanara J, Overmann J, Reimer LC. 2025. "
    "BacDive in 2025: the core database for prokaryotic strain data. "
    "Nucleic Acids Research 53(D1): D748–D756. https://doi.org/10.1093/nar/gkae959"
)
GROWTH_PREDICTION_TRAITS = {
    "anaerobic",
    "aerobic",
    "thermophilic",
    "oxygen_tolerance",
}
MAX_LIST = 30
MAX_TEXT = 400


def page_url(bacdive_id: int) -> str:
    return BACDIVE_PAGE.format(bacdive_id=bacdive_id)


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def is_predicted_item(item: dict) -> bool:
    """BacDive mixes predictions into ordinary sections when predictions=1.

    Those objects carry ``confidence``. Measured objects in the same list do not.
    Checked on strain 24493: Gram stain and oxygen tolerance.
    """
    return "confidence" in item


def measured_items(value: Any) -> list[dict]:
    return [item for item in as_list(value) if isinstance(item, dict) and not is_predicted_item(item)]


def clip(value: Any, limit: int = MAX_TEXT) -> Any:
    if not isinstance(value, str):
        return value
    text = re.sub(r"</?[iI]>", "", value).replace("\r", "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def reference_index(record: dict) -> dict[int, dict]:
    index: dict[int, dict] = {}
    for item in as_list(record.get("Reference")):
        if not isinstance(item, dict) or item.get("@id") is None:
            continue
        try:
            ref_id = int(item["@id"])
        except (TypeError, ValueError):
            continue
        doi = item.get("doi/url")
        index[ref_id] = {
            "id": ref_id,
            "authors": clip(item.get("authors"), 300),
            "title": clip(item.get("title"), 300),
            "doi_or_url": clip(doi, 300) if isinstance(doi, str) else None,
            "pubmed": item.get("pubmed"),
        }
    return index


def attach_ref(raw: dict, refs: dict[int, dict]) -> dict | None:
    ref_id = raw.get("@ref")
    if ref_id is None:
        return None
    try:
        ref_id_int = int(ref_id)
    except (TypeError, ValueError):
        return {"id": ref_id}
    found = refs.get(ref_id_int)
    if found:
        return found
    return {"id": ref_id_int, "note": "No matching entry in the Reference section of this record."}


def _fact(record_id: int, raw: dict, refs: dict[int, dict], **fields: Any) -> dict:
    fact = {
        "bacdive_id": record_id,
        "page": page_url(record_id),
        "value_kind": "measured",
        "ref": attach_ref(raw, refs),
    }
    for key, value in fields.items():
        if value is not None:
            fact[key] = clip(value) if isinstance(value, str) else value
    return fact


def _record_id(record: dict) -> int:
    general = record.get("General") or {}
    return int(general.get("BacDive-ID"))


def collection_numbers(record: dict) -> list[str]:
    literature = record.get("Literature") or {}
    raw = literature.get("culture collection no.")
    numbers: list[str] = []
    if isinstance(raw, str):
        numbers.extend(part.strip() for part in raw.split(",") if part.strip())
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                numbers.extend(part.strip() for part in item.split(",") if part.strip())
            elif isinstance(item, dict):
                text = item.get("culture collection no.") or item.get("number")
                if isinstance(text, str):
                    numbers.append(text.strip())
    general = record.get("General") or {}
    dsm = general.get("DSM-Number")
    if dsm is not None:
        label = f"DSM {dsm}"
        if label not in numbers:
            numbers.insert(0, label)
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    unique: list[str] = []
    for number in numbers:
        if number not in seen:
            seen.add(number)
            unique.append(number)
    return unique


def ncbi_tax_ids(record: dict) -> list[dict]:
    general = record.get("General") or {}
    raw = general.get("NCBI tax id")
    rows = []
    for item in as_list(raw):
        if isinstance(item, dict):
            rows.append(
                {
                    "ncbi_tax_id": item.get("NCBI tax id"),
                    "matching_level": item.get("Matching level"),
                }
            )
        else:
            rows.append({"ncbi_tax_id": item})
    return rows


def growth_facts(record: dict) -> dict:
    record_id = _record_id(record)
    refs = reference_index(record)
    culture = record.get("Culture and growth conditions") or {}
    physiology = record.get("Physiology and metabolism") or {}

    media = []
    for item in measured_items(culture.get("culture medium")):
        media.append(
            _fact(
                record_id,
                item,
                refs,
                name=item.get("name"),
                growth=item.get("growth"),
                link=item.get("link"),
                composition=clip(item.get("composition"), 240),
            )
        )

    temperatures = []
    for item in measured_items(culture.get("culture temp")):
        temperatures.append(
            _fact(
                record_id,
                item,
                refs,
                temperature=item.get("temperature"),
                growth=item.get("growth"),
                type=item.get("type"),
            )
        )

    ph_values = []
    for item in measured_items(culture.get("culture pH")):
        ph_values.append(
            _fact(
                record_id,
                item,
                refs,
                ph=item.get("pH"),
                ability=item.get("ability"),
                type=item.get("type"),
            )
        )

    oxygen = []
    for item in measured_items(physiology.get("oxygen tolerance")):
        oxygen.append(
            _fact(
                record_id,
                item,
                refs,
                oxygen_tolerance=item.get("oxygen tolerance"),
            )
        )

    def _cap(rows: list[dict]) -> tuple[list[dict], bool]:
        if len(rows) > MAX_LIST:
            return rows[:MAX_LIST], True
        return rows, False

    media, media_cut = _cap(media)
    temperatures, temp_cut = _cap(temperatures)
    ph_values, ph_cut = _cap(ph_values)
    oxygen, ox_cut = _cap(oxygen)
    return {
        "medium": media,
        "temperature": temperatures,
        "ph": ph_values,
        "oxygen": oxygen,
        "optimum_temperature": [row for row in temperatures if row.get("type") == "optimum"],
        "optimum_ph": [row for row in ph_values if row.get("type") == "optimum"],
        "truncated": media_cut or temp_cut or ph_cut or ox_cut,
    }


def prediction_facts(record: dict, *, traits: set[str] | None = None) -> list[dict]:
    record_id = _record_id(record)
    refs = reference_index(record)
    block = record.get("Genome-based predictions") or {}
    rows = []
    for item in as_list(block.get("predictions")):
        if not isinstance(item, dict):
            continue
        trait = item.get("trait")
        if traits is not None and trait not in traits:
            continue
        fact = {
            "bacdive_id": record_id,
            "page": page_url(record_id),
            "value_kind": "predicted",
            "trait": trait,
            "prediction": item.get("prediction"),
            "confidence": item.get("confidence"),
            "model": item.get("model"),
            "description": clip(item.get("description"), 300),
            "training_data": item.get("training_data"),
            "ref": attach_ref(item, refs),
        }
        rows.append(fact)
    return rows[:MAX_LIST]


def morphology_facts(record: dict) -> dict:
    record_id = _record_id(record)
    refs = reference_index(record)
    morphology = record.get("Morphology") or {}
    cells = measured_items(morphology.get("cell morphology"))
    cell = next((item for item in cells if item.get("gram stain") is not None), cells[0] if cells else {})
    gram = None
    if cell.get("gram stain") is not None:
        gram = _fact(record_id, cell, refs, gram_stain=cell.get("gram stain"))
    return {
        "gram_stain": gram,
        "cell_shape": cell.get("cell shape"),
        "motility": cell.get("motility"),
        "ref": attach_ref(cell, refs) if cell else None,
    }


def identity(record: dict) -> dict:
    record_id = _record_id(record)
    refs = reference_index(record)
    general = record.get("General") or {}
    name = record.get("Name and taxonomic classification") or {}
    return {
        "bacdive_id": record_id,
        "page": page_url(record_id),
        "strain_doi": general.get("doi"),
        "dsm_number": general.get("DSM-Number"),
        "description": clip(general.get("description"), 500),
        "ncbi_tax_ids": ncbi_tax_ids(record),
        "collection_numbers": collection_numbers(record),
        "name": {
            "value_kind": "measured",
            "domain": name.get("domain"),
            "phylum": name.get("phylum"),
            "class": name.get("class"),
            "order": name.get("order"),
            "family": name.get("family"),
            "genus": name.get("genus"),
            "species": name.get("species"),
            "full_scientific_name": clip(name.get("full scientific name"), 300),
            "strain_designation": name.get("strain designation"),
            "type_strain": name.get("type strain"),
            "ref": attach_ref(name, refs),
            "bacdive_id": record_id,
            "page": page_url(record_id),
        },
    }


def search_hit(record: dict) -> dict:
    info = identity(record)
    name = info["name"]
    return {
        "bacdive_id": info["bacdive_id"],
        "page": info["page"],
        "species": name.get("species"),
        "strain_designation": name.get("strain_designation"),
        "type_strain": name.get("type_strain"),
        "dsm_number": info["dsm_number"],
        "collection_numbers": info["collection_numbers"],
        "ref": name.get("ref"),
    }


def strain_summary(record: dict, *, include_predictions: bool) -> dict:
    info = identity(record)
    morph = morphology_facts(record)
    growth = growth_facts(record)
    isolation = record.get("Isolation, sampling and environmental information") or {}
    sources = []
    for item in as_list(isolation.get("isolation"))[:MAX_LIST]:
        if isinstance(item, dict):
            sources.append(
                {
                    "sample_type": clip(item.get("sample type"), 240),
                    "country": item.get("country"),
                    "geographic_location": clip(item.get("geographic location"), 200),
                    "value_kind": "measured",
                    "ref": attach_ref(item, reference_index(record)),
                    "bacdive_id": info["bacdive_id"],
                    "page": info["page"],
                }
            )
    sequences = record.get("Sequence information") or {}
    accessions_16s = []
    for item in as_list(sequences.get("16S sequences"))[:MAX_LIST]:
        if isinstance(item, dict):
            accessions_16s.append(
                {
                    "accession": item.get("accession"),
                    "length": item.get("length"),
                    "database": item.get("database"),
                    "value_kind": "measured",
                    "ref": attach_ref(item, reference_index(record)),
                    "bacdive_id": info["bacdive_id"],
                    "page": info["page"],
                }
            )
    genomes = []
    for item in as_list(sequences.get("Genome sequences"))[:MAX_LIST]:
        if isinstance(item, dict):
            genomes.append(
                {
                    "accession": item.get("INSDC accession"),
                    "assembly_level": item.get("assembly level"),
                    "description": clip(item.get("description"), 200),
                    "value_kind": "measured",
                    "ref": attach_ref(item, reference_index(record)),
                    "bacdive_id": info["bacdive_id"],
                    "page": info["page"],
                }
            )
    summary = {
        **info,
        "morphology": morph,
        "growth": growth,
        "isolation": sources,
        "sequences_16s": accessions_16s,
        "genomes": genomes,
        "predictions": _prediction_block(record, include_predictions, traits=None),
    }
    summary["notes"] = _growth_notes(growth)
    return summary


def growth_summary(record: dict) -> dict:
    info = identity(record)
    growth = growth_facts(record)
    return {
        "bacdive_id": info["bacdive_id"],
        "page": info["page"],
        "species": info["name"].get("species"),
        "strain_designation": info["name"].get("strain_designation"),
        "strain_doi": info["strain_doi"],
        "measured": {
            "medium": growth["medium"],
            "temperature": growth["temperature"],
            "ph": growth["ph"],
            "oxygen": growth["oxygen"],
            "optimum_temperature": growth["optimum_temperature"],
            "optimum_ph": growth["optimum_ph"],
        },
        "predicted": _prediction_block(record, True, traits=GROWTH_PREDICTION_TRAITS),
        "notes": _growth_notes(growth)
        + [
            "Predicted rows come only from the Genome-based predictions section. "
            "They are not copied into measured."
        ],
    }


_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)\s*", re.IGNORECASE)


def doi_key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = _DOI_PREFIX.sub("", value.strip()).strip().rstrip(" .")
    return text.lower() or None


def doi_link(value: Any) -> str | None:
    """Resolvable link for a DOI or an existing URL. Missing values stay None."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.lower().startswith(("http://", "https://")):
        return raw
    key = doi_key(raw)
    if key and key.startswith("10."):
        return "https://doi.org/" + key
    return None


def _reference_ids_by_doi_and_pubmed(refs: dict[int, dict]) -> tuple[dict[str, int], dict[str, int]]:
    by_doi: dict[str, int] = {}
    by_pubmed: dict[str, int] = {}
    for ref_id, ref in refs.items():
        key = doi_key(ref.get("doi_or_url"))
        if key and key not in by_doi:
            by_doi[key] = ref_id
        pubmed = ref.get("pubmed")
        if pubmed is not None:
            digits = str(pubmed).strip()
            if digits and digits not in by_pubmed:
                by_pubmed[digits] = ref_id
    return by_doi, by_pubmed


def citation_summary(record: dict) -> dict:
    info = identity(record)
    refs = reference_index(record)
    by_doi, by_pubmed = _reference_ids_by_doi_and_pubmed(refs)
    literature = record.get("Literature") or {}
    papers = []
    for item in as_list(literature.get("literature"))[:12]:
        if not isinstance(item, dict):
            continue
        doi = item.get("DOI")
        pubmed = item.get("Pubmed-ID")
        ref_id = by_doi.get(doi_key(doi) or "")
        if ref_id is None and pubmed is not None:
            ref_id = by_pubmed.get(str(pubmed).strip())
        papers.append(
            {
                "title": clip(item.get("title"), 300),
                "authors": clip(item.get("authors"), 300),
                "journal": item.get("journal"),
                "year": item.get("year"),
                "doi": doi,
                "link": doi_link(doi),
                "pubmed_id": pubmed,
                "bacdive_reference_id": ref_id,
                "bacdive_id": info["bacdive_id"],
                "page": info["page"],
            }
        )
    return {
        "bacdive_id": info["bacdive_id"],
        "page": info["page"],
        "species": info["name"].get("species"),
        "strain_doi": info["strain_doi"],
        "collection_numbers": info["collection_numbers"],
        "literature": papers,
        "references": list(refs.values())[:20],
        "bacdive_database_citation": BACDIVE_CITATION,
        "data_licence": "CC BY 4.0",
        "data_licence_url": "https://creativecommons.org/licenses/by/4.0/",
    }


def _prediction_block(record: dict, include: bool, traits: set[str] | None) -> dict:
    if not include:
        return {
            "included": False,
            "values": [],
            "note": (
                "Genome-based predictions were not requested. "
                "Ask again with predictions included. Predictions are never mixed into measured fields."
            ),
        }
    values = prediction_facts(record, traits=traits)
    return {
        "included": True,
        "values": values,
        "note": (
            "Each value is a genome-based prediction with its own confidence. "
            "training_data=yes means the strain was in that model's training set. "
            "A missing training_data field means the API did not provide one."
        ),
    }


def _growth_notes(growth: dict) -> list[str]:
    notes = []
    if not growth["optimum_temperature"]:
        notes.append("No measured optimum temperature is present in this record.")
    if not growth["optimum_ph"]:
        notes.append("No measured optimum pH is present in this record.")
    if not growth["oxygen"]:
        notes.append("No measured oxygen-tolerance entry is present in this record.")
    return notes
