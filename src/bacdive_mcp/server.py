"""stdio MCP server. Tools return JSON text and keep predictions out of measured fields."""

from __future__ import annotations

import json
import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolRequestParams, CallToolResult, TextContent
from pydantic import ValidationError

from bacdive_mcp.errors import BacdiveError
from bacdive_mcp.service import BacdiveService, default_service

logger = logging.getLogger(__name__)


def argument_type_message(exc: ValidationError) -> str:
    """Short invalid-argument text. The rejected value is not copied into the message."""
    sentences: list[str] = []
    for err in exc.errors():
        loc = [str(part) for part in err.get("loc") or () if part != "body"]
        field = ".".join(loc) if loc else "argument"
        kind = str(err.get("type") or "")
        if kind.startswith(("float_", "decimal_", "number_")):
            sentence = f"{field} must be a number."
        elif kind.startswith("int_"):
            sentence = f"{field} must be an integer."
        elif kind.startswith("bool_"):
            sentence = f"{field} must be true or false."
        elif kind.startswith("string_") or kind in {"string_type"}:
            sentence = f"{field} must be text."
        elif kind == "missing":
            sentence = f"{field} is required."
        elif kind == "extra_forbidden":
            sentence = f"{field} is not a parameter of this tool."
        else:
            sentence = f"{field} is not a valid value."
        if sentence not in sentences:
            sentences.append(sentence)
    if not sentences:
        return "One or more arguments have the wrong type."
    return " ".join(sentences)


class BacdiveMCP(MCPServer):
    async def _handle_call_tool(self, ctx, params: CallToolRequestParams) -> CallToolResult:
        context = Context(
            request_context=ctx,
            mcp_server=self,
            input_params=params,
            subscriptions=self._subscriptions,
        )
        try:
            return await self.call_tool(params.name, params.arguments or {}, context)
        except MCPError:
            raise
        except Exception as exc:
            cause = exc.__cause__
            if isinstance(exc, ToolError) and not isinstance(exc, UnexpectedToolError) and isinstance(cause, ValidationError):
                # Field names only: the rejected values are the caller's data.
                fields = sorted({".".join(str(part) for part in err["loc"]) for err in cause.errors()})
                logger.info("Tool %r rejected arguments: %r", params.name, fields)
                payload = {
                    "error": "invalid_argument",
                    "message": argument_type_message(cause),
                    "status": None,
                }
                text = json.dumps(payload, ensure_ascii=False, indent=2)
                return CallToolResult(content=[TextContent(type="text", text=text)], is_error=False)
            if isinstance(exc, ToolError) and not isinstance(exc, UnexpectedToolError):
                logger.info("Tool %r failed: %r", params.name, str(exc))
            else:
                logger.exception("Tool %r raised an unexpected exception", params.name)
            return CallToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)


# mcp 2.3 removed mcp.server.fastmcp.FastMCP and renamed that decorator API to MCPServer.
mcp = BacdiveMCP(
    "bacdive-mcp",
    instructions=(
        "Query BacDive strain records. Keep measured values and genome-based predictions "
        "separate. Every strain result includes its BacDive id, page URL, and a source "
        "reference when the record has one. Trait search uses the public BacDive SPARQL "
        "endpoint and labels those rows as recorded knowledge-graph values, not predictions."
    ),
)


def get_service() -> BacdiveService:
    return default_service()


def _emit(fn) -> str:
    try:
        payload = fn()
    except BacdiveError as exc:
        payload = {"error": exc.code, "message": str(exc), "status": exc.status}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text) > 20000:
        payload["output_truncated"] = True
        payload.pop("references", None)
        if isinstance(payload.get("literature"), list):
            payload["literature"] = payload["literature"][:5]
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(text) > 20000:
            text = text[:19900] + "\n…\n"
    return text


@mcp.tool()
def search_strains(name: str, match: str = "exact", limit: int = 10) -> str:
    """Find BacDive strains by genus, species, and optional strain tokens.

    match is exact, contains, startswith, or endswith.
    exact genus/species/subspecies uses the BacDive REST taxon view.
    Partial matches use the BacDive SPARQL endpoint, because the REST taxon view
    does not apply those modes to name fragments.
    Extra tokens such as K-12 or MG1655 are matched against the strain label and
    designation. Hyphens are ignored, so K-12 matches K12. Prefer a collection
    number when many derivatives share a name.
    """
    return _emit(lambda: get_service().search_strains(name, match=match, limit=limit))


@mcp.tool()
def get_strain(bacdive_id: int, include_predictions: bool = False) -> str:
    """Summarise one BacDive strain.

    Measured morphology, growth, isolation, and sequence accessions are included.
    Genome-based predictions are omitted unless include_predictions is true, and
    then they appear only under predictions, each with confidence and a reference.
    """
    return _emit(
        lambda: get_service().get_strain(bacdive_id, include_predictions=include_predictions)
    )


@mcp.tool()
def lookup_by_collection_number(number: str, match: str = "exact", limit: int = 10) -> str:
    """Look up a strain by a culture-collection number, for example DSM 26640.

    match is exact, contains, startswith, or endswith and is sent to
    /v2/culturecollectionno as search_type.
    """
    return _emit(
        lambda: get_service().lookup_by_collection_number(number, match=match, limit=limit)
    )


@mcp.tool()
def lookup_by_sequence(
    accession: str,
    sequence_type: str = "auto",
    match: str = "exact",
    limit: int = 10,
) -> str:
    """Look up strains by a 16S or genome INSDC accession.

    sequence_type is auto, 16s, or genome. auto queries both views.
    match is exact, contains, startswith, or endswith.
    """
    return _emit(
        lambda: get_service().lookup_by_sequence(
            accession,
            sequence_type=sequence_type,
            match=match,
            limit=limit,
        )
    )


@mcp.tool()
def get_growth_conditions(bacdive_id: int) -> str:
    """Return medium, temperature, pH, and oxygen tolerance for one strain.

    Measured entries and genome-based predictions are in separate lists.
    If the record has no measured optimum temperature, pH, or oxygen tolerance,
    the notes say so. A prediction is never reported as a measured optimum.
    """
    return _emit(lambda: get_service().get_growth_conditions(bacdive_id))


@mcp.tool()
def get_citation(bacdive_id: int) -> str:
    """Return the strain DOI, literature, references, page URL, and the BacDive citation.

    Each literature entry includes its DOI and, when the DOI can be resolved, a
    doi.org link. bacdive_reference_id is the Reference section @id when that
    section's DOI or PubMed id matches the paper, and null when it does not.
    """
    return _emit(lambda: get_service().get_citation(bacdive_id))


@mcp.tool()
def normalize_taxon_name(name: str) -> str:
    """Normalise a prokaryote name with the public NCBI Taxonomy E-utilities.

    Synonyms are resolved to the current scientific name and taxid. NCBI_API_KEY
    is optional. No other taxonomy database is queried.
    """
    return _emit(lambda: get_service().normalize_taxon_name(name))


@mcp.tool()
def query_by_traits(
    isolation_source: str | None = None,
    ph_below: float | None = None,
    oxygen: str | None = None,
    genus: str | None = None,
    gram: str | None = None,
    temperature_optimum_below: float | None = None,
    temperature_optimum_above: float | None = None,
    limit: int = 20,
) -> str:
    """Filter strains by recorded traits in the BacDive knowledge graph.

    The query runs on https://sparql.dsmz.de/api/bacdive, the backend of
    https://sparql.dsmz.de/bacdive.
    isolation_source is a case-insensitive substring such as soil.
    ph_below matches a growth-positive pH range whose start is below the number.
    oxygen is an exact label such as anaerobe, aerobe, facultative anaerobe, or
    obligate aerobe. gram is negative, positive, or variable.
    temperature_optimum_below and temperature_optimum_above filter the recorded
    optimum temperature range start.
    Results are labelled recorded. This endpoint does not return genome-based
    predictions; use get_growth_conditions for those.
    The response includes total_matched, returned, and truncated. truncated is
    true when the limit cut the list short of total_matched.
    """
    return _emit(
        lambda: get_service().query_by_traits(
            isolation_source=isolation_source,
            ph_below=ph_below,
            oxygen=oxygen,
            genus=genus,
            gram=gram,
            temperature_optimum_below=temperature_optimum_below,
            temperature_optimum_above=temperature_optimum_above,
            limit=limit,
        )
    )


def main() -> None:
    mcp.run(transport="stdio")
