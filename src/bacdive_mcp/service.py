"""BacDive REST calls and the small amount of SPARQL needed for traits and name matching."""

from __future__ import annotations

import re
from urllib.parse import quote, urlencode

from bacdive_mcp.errors import BacdiveError
from bacdive_mcp.http import PoliteHttp
from bacdive_mcp.ncbi import ncbi_api_key_from_env, normalize_taxon_name
from bacdive_mcp.present import (
    citation_summary,
    growth_summary,
    page_url,
    search_hit,
    strain_summary,
)
from bacdive_mcp.sparql import (
    PH_NOTE,
    RECORDED_NOTE,
    SPARQL_ENDPOINT,
    SPARQL_UI,
    build_name_query,
    build_trait_query,
    parse_id_bindings,
    parse_trait_bindings,
)

REST_BASE = "https://api.bacdive.dsmz.de/v2/"
MATCHES = {"exact", "contains", "startswith", "endswith"}
RANK_WORDS = {"subsp.", "subsp", "subspecies", "ssp.", "ssp"}


def parse_taxon_name(name: str) -> tuple[str | None, str | None, str | None, list[str]]:
    """Split a name into genus, species epithet, subspecies epithet, and leftover tokens.

    Leftover tokens (K-12, MG1655, strain codes) are not ranks. The REST taxon view
    only accepts genus / species epithet / subspecies epithet.
    """
    cleaned = name.replace("<i>", "").replace("</i>", "")
    tokens = [token for token in re.split(r"\s+", cleaned.strip()) if token]
    tokens = [token for token in tokens if token.casefold().rstrip(".") not in {"subsp", "ssp", "subspecies"} and token not in RANK_WORDS]
    if not tokens:
        raise BacdiveError("A taxon name is required.", code="invalid_argument")

    def is_epithet(token: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z-]*", token))

    genus = tokens[0]
    species = None
    subspecies = None
    extra: list[str] = []
    if len(tokens) >= 2 and is_epithet(tokens[1]):
        species = tokens[1]
        rest = tokens[2:]
    else:
        rest = tokens[1:]
    if rest and species and is_epithet(rest[0]) and not re.search(r"\d", rest[0]):
        subspecies = rest[0]
        extra = rest[1:]
    else:
        extra = rest
    return genus, species, subspecies, extra


class BacdiveService:
    def __init__(self, http: PoliteHttp, *, ncbi_api_key: str | None = None) -> None:
        self.http = http
        self.ncbi_api_key = ncbi_api_key

    def search_strains(self, name: str, match: str = "exact", limit: int = 10) -> dict:
        match = match.strip().lower()
        if match not in MATCHES:
            raise BacdiveError(
                "match must be exact, contains, startswith, or endswith.",
                code="invalid_argument",
            )
        limit = _limit(limit, 25, "limit")
        genus, species, subspecies, extra = parse_taxon_name(name)
        note = None
        if match == "exact" and not extra:
            payload = self.http.get_json(self._taxon_url(genus, species, subspecies))
            ids = _id_list(payload)
            total = payload.get("count")
            source = "bacdive-rest:/v2/taxon"
        else:
            if match != "exact":
                note = (
                    "Partial taxon matching uses the BacDive SPARQL endpoint. "
                    "On 2026-10-16 the REST taxon view did not apply contains, startswith, "
                    "or endswith to name fragments (Phaeo and galla returned no rows)."
                )
            elif extra:
                note = (
                    "Strain tokens beyond genus, species, and subspecies are matched against "
                    "the SPARQL strain label and designation. Hyphens are ignored, so K-12 matches K12. "
                    "Many derivatives can share a token; narrow the name or use a collection number."
                )
            query = build_name_query(
                genus=genus,
                species=species,
                subspecies=subspecies,
                designation_tokens=extra,
                match=match,
                limit=limit,
            )
            payload = self._sparql(query)
            ids = parse_id_bindings(payload)
            total = None
            source = "bacdive-sparql"
        hits = self._summaries(ids[:limit])
        return {
            "query": name,
            "match": match,
            "source": source,
            "total_count": total,
            "returned": len(hits),
            "limit": limit,
            "results": hits,
            "note": note,
        }

    def get_strain(self, bacdive_id: int, *, include_predictions: bool = False) -> dict:
        record = self._fetch_one(_positive_id(bacdive_id), predictions=include_predictions)
        if record is None:
            return _not_found(bacdive_id)
        return strain_summary(record, include_predictions=include_predictions)

    def lookup_by_collection_number(self, number: str, match: str = "exact", limit: int = 10) -> dict:
        match = match.strip().lower()
        if match not in MATCHES:
            raise BacdiveError(
                "match must be exact, contains, startswith, or endswith.",
                code="invalid_argument",
            )
        text = number.strip()
        if not text or len(text) > 80:
            raise BacdiveError("Provide a collection number such as 'DSM 26640'.", code="invalid_argument")
        limit = _limit(limit, 25, "limit")
        url = (
            REST_BASE
            + "culturecollectionno/"
            + quote(text, safe="")
            + "?"
            + urlencode({"search_type": match})
        )
        payload = self.http.get_json(url)
        ids = _id_list(payload)
        return {
            "query": text,
            "match": match,
            "source": "bacdive-rest:/v2/culturecollectionno",
            "total_count": payload.get("count"),
            "returned": min(len(ids), limit),
            "results": self._summaries(ids[:limit]),
        }

    def lookup_by_sequence(
        self,
        accession: str,
        sequence_type: str = "auto",
        match: str = "exact",
        limit: int = 10,
    ) -> dict:
        sequence_type = sequence_type.strip().lower()
        match = match.strip().lower()
        if sequence_type not in {"auto", "16s", "genome"}:
            raise BacdiveError("sequence_type must be auto, 16s, or genome.", code="invalid_argument")
        if match not in MATCHES:
            raise BacdiveError(
                "match must be exact, contains, startswith, or endswith.",
                code="invalid_argument",
            )
        text = accession.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.]{1,40}", text):
            raise BacdiveError(
                "accession must be an INSDC-style accession (letters, numbers, underscore, dot).",
                code="invalid_argument",
            )
        limit = _limit(limit, 25, "limit")
        kinds = ["16s", "genome"] if sequence_type == "auto" else [sequence_type]
        found = []
        for kind in kinds:
            endpoint = "sequence_16s" if kind == "16s" else "sequence_genome"
            url = (
                REST_BASE
                + endpoint
                + "/"
                + quote(text, safe="")
                + "?"
                + urlencode({"search_type": match})
            )
            payload = self.http.get_json(url)
            ids = _id_list(payload)
            if not ids:
                continue
            found.append(
                {
                    "sequence_type": kind,
                    "total_count": payload.get("count"),
                    "results": self._summaries(ids[:limit]),
                }
            )
        return {
            "query": text,
            "match": match,
            "sequence_type": sequence_type,
            "source": "bacdive-rest",
            "hits": found,
            "message": None if found else "No BacDive strain was linked to this accession.",
        }

    def get_growth_conditions(self, bacdive_id: int) -> dict:
        record = self._fetch_one(_positive_id(bacdive_id), predictions=True)
        if record is None:
            return _not_found(bacdive_id)
        return growth_summary(record)

    def get_citation(self, bacdive_id: int) -> dict:
        record = self._fetch_one(_positive_id(bacdive_id), predictions=False)
        if record is None:
            return _not_found(bacdive_id)
        return citation_summary(record)

    def normalize_taxon_name(self, name: str) -> dict:
        return normalize_taxon_name(self.http, name, api_key=self.ncbi_api_key)

    def query_by_traits(
        self,
        *,
        isolation_source: str | None = None,
        ph_below: float | None = None,
        oxygen: str | None = None,
        genus: str | None = None,
        gram: str | None = None,
        temperature_optimum_below: float | None = None,
        temperature_optimum_above: float | None = None,
        limit: int = 20,
    ) -> dict:
        limit = _limit(limit, 50, "limit")
        query = build_trait_query(
            isolation_source=isolation_source,
            ph_below=ph_below,
            oxygen=oxygen,
            genus=genus,
            gram=gram,
            temperature_optimum_below=temperature_optimum_below,
            temperature_optimum_above=temperature_optimum_above,
            limit=limit,
        )
        payload = self._sparql(query)
        rows = parse_trait_bindings(payload)
        notes = [RECORDED_NOTE]
        if ph_below is not None:
            notes.append(PH_NOTE)
        return {
            "source": "bacdive-sparql",
            "sparql_ui": SPARQL_UI,
            "sparql_endpoint": SPARQL_ENDPOINT,
            "returned": len(rows),
            "limit": limit,
            "results": rows,
            "notes": notes,
        }

    def _sparql(self, query: str) -> dict:
        payload = self.http.post_json(
            SPARQL_ENDPOINT,
            query,
            content_type="application/sparql-query",
            accept="application/sparql-results+json",
        )
        if payload.get("status") == "ERROR":
            message = payload.get("exception") or "SPARQL query failed."
            raise BacdiveError(str(message)[:500], code="sparql_error")
        return payload

    def _fetch_one(self, bacdive_id: int, *, predictions: bool) -> dict | None:
        records = self._fetch_many([bacdive_id], predictions=predictions)
        return records.get(str(bacdive_id))

    def _fetch_many(self, ids: list[int], *, predictions: bool) -> dict[str, dict]:
        if not ids:
            return {}
        joined = ";".join(str(item) for item in ids)
        url = REST_BASE + "fetch/" + joined
        if predictions:
            url += "?predictions=1"
        payload = self.http.get_json(url)
        return _records(payload)

    def _summaries(self, ids: list[int]) -> list[dict]:
        records = self._fetch_many(ids, predictions=False)
        hits = []
        for bacdive_id in ids:
            record = records.get(str(bacdive_id))
            if record is None:
                hits.append(
                    {
                        "bacdive_id": bacdive_id,
                        "page": page_url(bacdive_id),
                        "detail": "The id list included this strain, but fetch did not return a record.",
                    }
                )
            else:
                hits.append(search_hit(record))
        return hits

    def _taxon_url(self, genus: str | None, species: str | None, subspecies: str | None) -> str:
        if not genus:
            raise BacdiveError("A genus is required for an exact taxon search.", code="invalid_argument")
        parts = [quote(genus, safe="")]
        if species:
            parts.append(quote(species, safe=""))
        if subspecies:
            parts.append(quote(subspecies, safe=""))
        return REST_BASE + "taxon/" + "/".join(parts)


def default_service() -> BacdiveService:
    # Anonymous BacDive access was verified on 2026-10-16. BACDIVE_USER and
    # BACDIVE_PASSWORD are not added to any request. If they are set, they are
    # kept only so error text can be scrubbed.
    return BacdiveService(PoliteHttp.from_env(), ncbi_api_key=ncbi_api_key_from_env())


def _positive_id(bacdive_id: int) -> int:
    try:
        value = int(bacdive_id)
    except (TypeError, ValueError) as exc:
        raise BacdiveError("bacdive_id must be a positive integer.", code="invalid_argument") from exc
    if value <= 0:
        raise BacdiveError("bacdive_id must be a positive integer.", code="invalid_argument")
    return value


def _limit(value: int, maximum: int, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise BacdiveError(f"{label} must be an integer.", code="invalid_argument") from exc
    if number < 1 or number > maximum:
        raise BacdiveError(f"{label} must be between 1 and {maximum}.", code="invalid_argument")
    return number


def _id_list(payload: dict) -> list[int]:
    results = payload.get("results")
    if isinstance(results, list):
        ids = []
        for item in results:
            try:
                ids.append(int(item))
            except (TypeError, ValueError):
                continue
        return ids
    if isinstance(results, dict):
        ids = []
        for key in results:
            try:
                ids.append(int(key))
            except (TypeError, ValueError):
                continue
        return ids
    return []


def _records(payload: dict) -> dict[str, dict]:
    results = payload.get("results")
    if isinstance(results, dict):
        return {str(key): value for key, value in results.items() if isinstance(value, dict)}
    if isinstance(results, list):
        mapped: dict[str, dict] = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            general = item.get("General") or {}
            bacdive_id = general.get("BacDive-ID")
            if bacdive_id is not None:
                mapped[str(bacdive_id)] = item
        return mapped
    return {}


def _not_found(bacdive_id: int) -> dict:
    return {
        "bacdive_id": int(bacdive_id),
        "page": page_url(int(bacdive_id)),
        "found": False,
        "message": "BacDive returned no record for this id.",
    }

