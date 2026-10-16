"""Public NCBI taxonomy name normalisation via E-utilities.

No other taxonomy database is used. NCBI_API_KEY is optional and is sent only as
the api_key parameter. It is not written to logs or cache keys.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from bacdive_mcp.errors import BacdiveError
from bacdive_mcp.http import PoliteHttp

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"


def ncbi_api_key_from_env() -> str | None:
    value = (os.environ.get("NCBI_API_KEY") or "").strip()
    return value or None


def _eutils_url(script: str, params: dict[str, str], api_key: str | None) -> str:
    query = {"tool": "bacdive-mcp", **params}
    if api_key:
        query["api_key"] = api_key
    return EUTILS + script + "?" + urlencode(query)


def normalize_taxon_name(http: PoliteHttp, name: str, *, api_key: str | None = None) -> dict:
    cleaned = " ".join(name.split())
    if not cleaned or len(cleaned) > 200:
        raise BacdiveError("Provide a taxon name of at most 200 characters.", code="invalid_argument")

    search_url = _eutils_url(
        "esearch.fcgi",
        {"db": "taxonomy", "term": cleaned, "retmode": "json", "retmax": "5"},
        api_key,
    )
    searched = http.get_json(search_url)
    result = searched.get("esearchresult") or {}
    id_list = [str(item) for item in result.get("idlist") or []]
    try:
        count = int(result.get("count") or 0)
    except (TypeError, ValueError):
        count = len(id_list)
    if count == 0 or not id_list:
        return {
            "query": cleaned,
            "found": False,
            "source": "NCBI Taxonomy (public E-utilities)",
            "message": "NCBI Taxonomy returned no match for this name.",
        }

    primary = id_list[0]
    fetch_url = _eutils_url(
        "efetch.fcgi",
        {"db": "taxonomy", "id": primary, "retmode": "xml"},
        api_key,
    )
    xml_text = http.get_text(fetch_url, accept="application/xml")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise BacdiveError("NCBI taxonomy XML could not be parsed.", code="bad_response") from exc

    taxon = root.find("Taxon")
    if taxon is None:
        raise BacdiveError("NCBI taxonomy response did not contain a Taxon.", code="bad_response")

    scientific = _text(taxon, "ScientificName")
    rank = _text(taxon, "Rank")
    lineage = _text(taxon, "Lineage")
    other = taxon.find("OtherNames")
    synonyms = _children(other, "Synonym") + _children(other, "GenbankSynonym") + _children(other, "EquivalentName")
    misspellings = []
    if other is not None:
        for node in other.findall("Name"):
            if _text(node, "ClassCDE") == "misspelling":
                label = _text(node, "DispName")
                if label:
                    misspellings.append(label)

    status = "resolved"
    folded = _fold(cleaned)
    if scientific and folded == _fold(scientific):
        status = "current_name"
    elif any(folded == _fold(item) for item in synonyms):
        status = "normalized_from_synonym"

    return {
        "query": cleaned,
        "found": True,
        "source": "NCBI Taxonomy (public E-utilities)",
        "status": status,
        "ncbi_tax_id": _text(taxon, "TaxId"),
        "scientific_name": scientific,
        "rank": rank,
        "lineage": lineage,
        "synonyms": synonyms[:15],
        "misspellings": misspellings[:5],
        "additional_taxids": id_list[1:5],
        "match_count": count,
        "note": (
            "Only the first NCBI hit was fetched. additional_taxids lists further search hits "
            "when NCBI returned more than one."
            if len(id_list) > 1
            else "Resolved with the public NCBI Taxonomy E-utilities. No other taxonomy database was queried."
        ),
    }


def _text(node: ET.Element, tag: str) -> str | None:
    child = node.find(tag)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


def _children(node: ET.Element | None, tag: str) -> list[str]:
    if node is None:
        return []
    values = []
    for child in node.findall(tag):
        if child.text and child.text.strip():
            values.append(child.text.strip())
    return values


def _fold(value: str) -> str:
    return " ".join(value.casefold().replace("-", " ").split())

