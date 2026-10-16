"""BacDive knowledge-graph queries.

The public page is https://sparql.dsmz.de/bacdive. Queries are executed on its
QLever backend, https://sparql.dsmz.de/api/bacdive. Prefixes and the pH comparison
were checked against that endpoint on 2026-10-16.

QLever accepts FILTER(?decimal < 5) for xsd:decimal values. It does not accept
xsd:float(?value) casts. REGEX() is only allowed on a bare variable, not on STR()
or LCASE().
"""

from __future__ import annotations

import math
import re

from bacdive_mcp.errors import BacdiveError

SPARQL_ENDPOINT = "https://sparql.dsmz.de/api/bacdive"
SPARQL_UI = "https://sparql.dsmz.de/bacdive"
PREFIXES = """\
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX d3o: <https://purl.dsmz.de/schema/>
PREFIX bd: <https://purl.dsmz.de/bacdive/>
"""

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .'_-]{0,80}$")
RECORDED_NOTE = (
    "These rows come from the BacDive knowledge graph. The published schema for this "
    "endpoint has no genome-prediction class, so the rows are labelled recorded rather "
    "than measured or predicted. Genome-based predictions, with confidence, are only "
    "returned by get_strain and get_growth_conditions."
)
PH_NOTE = (
    "ph_below keeps strains whose recorded growth-positive pH range starts below the "
    "threshold (d3o:hasPHRangeStart, test type growth, ability positive). A range of "
    "4.2–7.0 matches ph_below=5. This is not a single reading taken at that pH."
)


def sparql_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def escape_regex(value: str) -> str:
    return re.sub(r"([.^$*+?{}\[\]\\|()])", r"\\\1", value)


def regex_pattern(prefix: str, text: str, suffix: str) -> str:
    """Build a SPARQL string literal. Anchors are added after escaping the user text."""
    return sparql_string(prefix + escape_regex(text) + suffix)


def _require_token(value: str, label: str) -> str:
    text = value.strip()
    if not _TOKEN.match(text):
        raise BacdiveError(
            f"{label} must be letters, numbers, spaces, or .'_- (max 81 characters).",
            code="invalid_argument",
        )
    return text


def _sparql_number(number: float) -> str:
    # QLever compares these literals as xsd:decimal. 5.0 is valid but 5 matches
    # the query shape that was checked live.
    return format(number, "g")


def _finite(value: float, label: str, low: float, high: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number < low or number > high:
        raise BacdiveError(
            f"{label} must be a finite number between {low} and {high}.",
            code="invalid_argument",
        )
    return number


def build_trait_query(
    *,
    isolation_source: str | None = None,
    ph_below: float | None = None,
    oxygen: str | None = None,
    genus: str | None = None,
    gram: str | None = None,
    temperature_optimum_below: float | None = None,
    temperature_optimum_above: float | None = None,
    limit: int = 20,
) -> str:
    if not any(
        value is not None and value != ""
        for value in (
            isolation_source,
            ph_below,
            oxygen,
            genus,
            gram,
            temperature_optimum_below,
            temperature_optimum_above,
        )
    ):
        raise BacdiveError(
            "Provide at least one of isolation_source, ph_below, oxygen, genus, gram, "
            "temperature_optimum_below, or temperature_optimum_above.",
            code="invalid_argument",
        )
    if limit < 1 or limit > 50:
        raise BacdiveError("limit must be between 1 and 50.", code="invalid_argument")

    # Selective phenotype patterns come first. Starting from `a d3o:Strain`
    # made the soil + pH query time out on the live QLever endpoint.
    patterns: list[str] = []
    isolation_patterns: list[str] = []
    select = ["?id", "?name"]
    reference_selected = False

    if isolation_source:
        text = isolation_source.strip()
        if not text or len(text) > 100 or any(ord(ch) < 32 for ch in text):
            raise BacdiveError(
                "isolation_source must be a short phrase without control characters.",
                code="invalid_argument",
            )
        isolation_patterns.append("?src a d3o:IsolationSource ;")
        isolation_patterns.append("    d3o:describesStrain ?strain ;")
        isolation_patterns.append("    rdfs:label ?source .")
        isolation_patterns.append(f"FILTER(CONTAINS(LCASE(?source), {sparql_string(text.lower())}))")
        select.append('(GROUP_CONCAT(DISTINCT ?source; SEPARATOR=" | ") AS ?sources)')

    if ph_below is not None:
        number = _sparql_number(_finite(ph_below, "ph_below", 0, 14))
        patterns.append("?ph a d3o:CulturePH ;")
        patterns.append("    d3o:describesStrain ?strain ;")
        patterns.append('    d3o:hasTestType "growth" ;')
        patterns.append('    d3o:hasTestAbility "positive" ;')
        patterns.append("    d3o:hasPHRangeStart ?phStart .")
        patterns.append("OPTIONAL { ?ph d3o:hasPHRangeEnd ?phEnd }")
        patterns.append(f"FILTER(?phStart < {number})")
        patterns.append(
            "OPTIONAL { ?ph d3o:hasReference ?phRef . OPTIONAL { ?phRef rdfs:seeAlso ?refSee } }"
        )
        select.append("(SAMPLE(?phStart) AS ?ph_start)")
        select.append("(SAMPLE(?phEnd) AS ?ph_end)")
        select.append("(SAMPLE(?refSee) AS ?reference)")
        reference_selected = True

    if oxygen:
        text = _require_token(oxygen, "oxygen")
        patterns.append("?ox a d3o:OxygenTolerance ;")
        patterns.append("    d3o:describesStrain ?strain ;")
        patterns.append("    d3o:hasOxygenTolerance ?oxygenValue .")
        patterns.append(f"FILTER(REGEX(?oxygenValue, {regex_pattern('^', text, '$')}, \"i\"))")
        select.append("(SAMPLE(?oxygenValue) AS ?oxygen)")
        if not reference_selected:
            patterns.append(
                "OPTIONAL { ?ox d3o:hasReference ?oxRef . OPTIONAL { ?oxRef rdfs:seeAlso ?refSee } }"
            )
            select.append("(SAMPLE(?refSee) AS ?reference)")
            reference_selected = True

    if gram:
        text = _require_token(gram, "gram")
        patterns.append("?gstain a d3o:GramStain ;")
        patterns.append("    d3o:describesStrain ?strain ;")
        patterns.append("    d3o:hasGramStain ?gramValue .")
        patterns.append(f"FILTER(REGEX(?gramValue, {regex_pattern('^', text, '$')}, \"i\"))")
        select.append("(SAMPLE(?gramValue) AS ?gram)")

    if temperature_optimum_below is not None or temperature_optimum_above is not None:
        patterns.append("?ctemp a d3o:CultureTemperature ;")
        patterns.append("    d3o:describesStrain ?strain ;")
        patterns.append('    d3o:hasTestType "optimum" ;')
        patterns.append("    d3o:hasTemperatureRangeStart ?tempStart .")
        patterns.append("OPTIONAL { ?ctemp d3o:hasTemperatureRangeEnd ?tempEnd }")
        if temperature_optimum_below is not None:
            number = _sparql_number(_finite(temperature_optimum_below, "temperature_optimum_below", -10, 120))
            patterns.append(f"FILTER(?tempStart < {number})")
        if temperature_optimum_above is not None:
            number = _sparql_number(_finite(temperature_optimum_above, "temperature_optimum_above", -10, 120))
            patterns.append(f"FILTER(?tempStart > {number})")
        select.append("(SAMPLE(?tempStart) AS ?temperature_optimum_start)")
        select.append("(SAMPLE(?tempEnd) AS ?temperature_optimum_end)")

    patterns.extend(isolation_patterns)

    if genus:
        text = _require_token(genus, "genus")
        patterns.append("?strain d3o:hasBacDiveID ?id ;")
        patterns.append("    rdfs:label ?name ;")
        patterns.append("    d3o:hasGenus ?genusFilter .")
        patterns.append(f"FILTER(REGEX(?genusFilter, {regex_pattern('^', text, '$')}, \"i\"))")
    else:
        patterns.append("?strain d3o:hasBacDiveID ?id ;")
        patterns.append("    rdfs:label ?name .")

    body = "\n  ".join(patterns)
    projection = " ".join(select)
    return (
        f"{PREFIXES}\n"
        f"SELECT {projection}\n"
        f"WHERE {{\n  {body}\n}}\n"
        f"GROUP BY ?id ?name\n"
        f"LIMIT {int(limit)}\n"
    )


def build_name_query(
    *,
    genus: str | None,
    species: str | None,
    subspecies: str | None,
    designation_tokens: list[str],
    match: str,
    limit: int,
) -> str:
    if match not in {"exact", "contains", "startswith", "endswith"}:
        raise BacdiveError(
            "match must be exact, contains, startswith, or endswith.",
            code="invalid_argument",
        )
    patterns = [
        "?strain a d3o:Strain ;",
        "    d3o:hasBacDiveID ?id ;",
        "    rdfs:label ?name .",
    ]
    if genus and match == "exact":
        patterns.append(f"?strain d3o:hasGenus {sparql_string(genus)} .")
    elif genus and match == "startswith":
        patterns.append("?strain d3o:hasGenus ?genus .")
        patterns.append(f"FILTER(REGEX(?genus, {regex_pattern('^', genus, '')}, \"i\"))")
    elif genus and match == "endswith":
        patterns.append("?strain d3o:hasGenus ?genus .")
        patterns.append(f"FILTER(REGEX(?genus, {regex_pattern('', genus, '$')}, \"i\"))")
    elif genus and match == "contains":
        patterns.append("?strain d3o:hasGenus ?genus .")
        patterns.append(f"FILTER(CONTAINS(LCASE(STR(?genus)), LCASE({sparql_string(genus)})))")

    if species and match == "exact":
        full = f"{genus} {species}" if genus else species
        patterns.append(f"?strain d3o:hasSpecies {sparql_string(full)} .")
    elif species:
        patterns.append("?strain d3o:hasSpeciesEpithet ?epithet .")
        if match == "startswith":
            patterns.append(f"FILTER(REGEX(?epithet, {regex_pattern('^', species, '')}, \"i\"))")
        elif match == "endswith":
            patterns.append(f"FILTER(REGEX(?epithet, {regex_pattern('', species, '$')}, \"i\"))")
        else:
            patterns.append(
                f"FILTER(CONTAINS(LCASE(STR(?epithet)), LCASE({sparql_string(species)})))"
            )

    if subspecies:
        patterns.append(
            f"FILTER(CONTAINS(LCASE(STR(?name)), LCASE({sparql_string(subspecies)})))"
        )

    if designation_tokens:
        patterns.append("OPTIONAL { ?strain d3o:hasDesignation ?desig }")
        for token in designation_tokens:
            alternatives = _needle_variants(token)
            clauses = []
            for needle in alternatives:
                literal = sparql_string(needle)
                clauses.append(f"CONTAINS(LCASE(STR(?name)), LCASE({literal}))")
                clauses.append(f"CONTAINS(LCASE(STR(?desig)), LCASE({literal}))")
            patterns.append("FILTER(" + " || ".join(clauses) + ")")

    if len(patterns) <= 3 and not designation_tokens:
        raise BacdiveError("A taxon name is required.", code="invalid_argument")

    body = "\n  ".join(patterns)
    return (
        f"{PREFIXES}\n"
        "SELECT DISTINCT ?id ?name\n"
        f"WHERE {{\n  {body}\n}}\n"
        "ORDER BY ?id\n"
        f"LIMIT {int(limit)}\n"
    )


def _needle_variants(token: str) -> list[str]:
    text = token.strip().lower()
    compact = re.sub(r"[\s\-]+", "", text)
    variants = [text]
    if compact and compact not in variants:
        variants.append(compact)
    return variants


def binding_value(row: dict, key: str) -> str | None:
    cell = row.get(key)
    if not isinstance(cell, dict):
        return None
    value = cell.get("value")
    if value is None:
        return None
    return str(value)


def parse_trait_bindings(payload: dict) -> list[dict]:
    rows = payload.get("results", {}).get("bindings", [])
    parsed = []
    for row in rows:
        bacdive_id = binding_value(row, "id")
        if bacdive_id is None:
            continue
        try:
            bacdive_int = int(float(bacdive_id))
        except ValueError:
            continue
        item = {
            "bacdive_id": bacdive_int,
            "page": f"https://bacdive.dsmz.de/strain/{bacdive_int}",
            "name": binding_value(row, "name"),
            "value_kind": "recorded",
        }
        for key in (
            "sources",
            "ph_start",
            "ph_end",
            "oxygen",
            "gram",
            "temperature_optimum_start",
            "temperature_optimum_end",
            "reference",
        ):
            value = binding_value(row, key)
            if value is not None:
                item[key] = _maybe_number(value) if key.startswith(("ph_", "temperature_")) else value
        parsed.append(item)
    return parsed


def parse_id_bindings(payload: dict) -> list[int]:
    ids: list[int] = []
    for row in payload.get("results", {}).get("bindings", []):
        raw = binding_value(row, "id")
        if raw is None:
            continue
        try:
            ids.append(int(float(raw)))
        except ValueError:
            continue
    return ids


def _maybe_number(value: str) -> float | str:
    try:
        return float(value)
    except ValueError:
        return value
