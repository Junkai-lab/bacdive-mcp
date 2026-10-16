# bacdive-mcp

个人兴趣项目：让 MCP 客户端查询 BacDive 菌株表型。实验记录和基因组预测分开，每条结果带 BacDive 编号、菌株页面和出处。这不是 DSMZ 或 BacDive 团队的官方软件。

Unofficial MCP server for [BacDive](https://bacdive.dsmz.de/), the DSMZ prokaryotic strain database. It answers strain questions over stdio. Measured records and genome-based predictions stay in separate fields, and each result carries the BacDive id, the strain page, and the source reference when BacDive provides one.

The server uses the official `mcp` SDK (2.3) in the decorator style that used to be called FastMCP. In this release that class was renamed to `MCPServer` (`mcp.server.mcpserver`); `mcp.server.fastmcp` no longer exists. Transport is stdio.

This is a personal, non-commercial project. It is not affiliated with DSMZ or the BacDive team. The data licence is separate from the MIT licence on this code.

## Install

This version is not published to PyPI or any MCP registry. From a checkout:

```bash
uv sync --all-groups
uv run bacdive-mcp
```

`uvx` can run the git branch directly:

```bash
uvx --from git+https://github.com/Junkai-lab/bacdive-mcp@cursor/bacdive-mcp-v1-b1d1 bacdive-mcp
```

After a future release, the same entry point is `uvx bacdive-mcp`. Point an MCP client at that stdio command. Optional environment variables:

| Variable | Role |
|---|---|
| `NCBI_API_KEY` | Optional key for the public NCBI Taxonomy E-utilities. Anonymous access also works, at NCBI's lower rate. |
| `BACDIVE_CACHE_DIR` | Disk cache directory. Default: `$XDG_CACHE_HOME/bacdive-mcp` or `~/.cache/bacdive-mcp`. |
| `BACDIVE_CACHE_TTL_DAYS` | Cache lifetime. Default 7. |
| `BACDIVE_CACHE` | Set to `0` to disable the cache. |
| `BACDIVE_USER`, `BACDIVE_PASSWORD` | Reserved only. v1 does not send them. The public API did not require an account when this was checked (16 October 2026). They are never written to logs. |

## Tools

| Tool | What it calls |
|---|---|
| `search_strains` | REST `/v2/taxon/{genus}/{species}/{subspecies}` for an exact rank name. SPARQL when the match is partial, or when the name includes a strain token such as K-12. |
| `get_strain` | REST `/v2/fetch/{id}`. Predictions only if you ask for them. |
| `lookup_by_collection_number` | REST `/v2/culturecollectionno/{number}` with `search_type` `exact`, `contains`, `startswith`, or `endswith`. |
| `lookup_by_sequence` | REST `/v2/sequence_16s/{accession}` and/or `/v2/sequence_genome/{accession}`. |
| `get_growth_conditions` | Medium, temperature, pH, and oxygen tolerance, split into `measured` and `predicted`. |
| `get_citation` | Strain DOI, literature, the Reference section, and the BacDive paper citation. |
| `normalize_taxon_name` | Public NCBI Taxonomy E-utilities (`esearch` + `efetch`). No other taxonomy database. |
| `query_by_traits` | SPARQL on `https://sparql.dsmz.de/api/bacdive`, the query service behind https://sparql.dsmz.de/bacdive. |

The HTTP paths are the ones documented by BacDive and used by the MIT-licensed `bacdive` client (Julia Koblitz). This package does not depend on that client: the official client prints to stdout and retries for much longer than an MCP tool should.

## Demo questions

These were checked against the live API on 16 October 2026.

1. **Is Phaeobacter gallaeciensis DSM 26640 a strict anaerobe?**
   `lookup_by_collection_number("DSM 26640")` returns BacDive [24493](https://bacdive.dsmz.de/strain/24493). The record has no measured oxygen-tolerance entry. Genome-based predictions (separate list) say `anaerobic = no` (confidence 95.543, not training data) and `oxygen_tolerance = obligate aerobe` (confidence 96.371). Those are predictions, not laboratory results.

2. **What is the optimum growth temperature of Escherichia coli K-12?**
   K-12 covers many derivatives. DSM 18039 is Escherichia coli K12 MG1655 (BacDive 4902). On this date the measured culture temperature is growth at 37°C. There is no measured optimum temperature in that record, and the tool says so instead of inventing one.

3. **Which collections hold DSM 26640?**
   The 24493 record lists DSM 26640, CIP 105210, IAM 14812, JCM 21319, ATCC 700781, IFO 16654, and NBRC 16654, with catalogue references.

4. **Which strain has 16S accession AF000162?**
   BacDive 6583, *Lacticaseibacillus manihotivorans* (DSM 13343).

5. **Which strains were isolated from soil and can grow below pH 5?**
   `query_by_traits(isolation_source="soil", ph_below=5)` returns recorded knowledge-graph rows whose growth-positive pH range starts below 5 and whose isolation-source label contains "soil" (for example BacDive 135, *Terriglobus saanensis*, range 4.5–7.5). A range of 4.2–7.0 matches. This is not a single assay taken at pH 5, and it is not a genome prediction.

## Measured values and predictions

BacDive's REST API omits genome-based predictions unless the request sets `predictions=1`. They arrive in `Genome-based predictions` with `trait`, `prediction`, `confidence`, `model`, and sometimes `training_data`. On strain 24493 the same request also inserts those predictions into Morphology and Physiology; the inserted objects are the ones that carry `confidence`. This server treats any object with `confidence` as a prediction and does not copy it into measured medium, temperature, pH, oxygen, or Gram stain.

`training_data = yes` means the strain was part of that model's training set. If the field is missing, the API did not send it.

`query_by_traits` reads the BacDive knowledge graph (DSMZ Digital Diversity Ontology, prefix `d3o:` `<https://purl.dsmz.de/schema/>`). The schema published on the SPARQL page has no prediction class. Those rows are labelled `recorded`. Use `get_growth_conditions` when you need the prediction list.

## Polite access

- Responses are cached on disk for 7 days.
- At most 2 requests run at once, with about 0.5 s between starts. BacDive does not publish a rate limit; these numbers are a local choice.
- HTTP 429 and 5xx responses are retried at most 3 times (backoff 0.5 s, 1 s, 2 s). A `Retry-After` longer than 8 seconds stops the tool instead of leaving the client waiting.
- The `User-Agent` is `bacdive-mcp/0.1 (+https://github.com/Junkai-lab/bacdive-mcp)`.
- Tool output is capped at about 20,000 characters. Long medium recipes and literature lists are shortened first.

## Data attribution

Strain data are from BacDive (DSMZ), https://bacdive.dsmz.de/. The BacDive about page states that the work is licensed under a [Creative Commons Attribution 4.0 International License](https://creativecommons.org/licenses/by/4.0/) and asks commercial users of BacDive to contact them: https://bacdive.dsmz.de/about. This software does not grant that data licence. You are responsible for complying with BacDive's terms.

Please cite:

> Schober I, Koblitz J, Sardà Carbasse J, Ebeling C, Schmidt ML, Podstawka A, Gupta R, Ilangovan V, Chamanara J, Overmann J, Reimer LC. 2025. BacDive in 2025: the core database for prokaryotic strain data. Nucleic Acids Research 53(D1): D748–D756. https://doi.org/10.1093/nar/gkae959

Volume, issue, pages, and DOI were checked against the journal article and the DOI record. The BacDive "how to cite" text names the same paper.

NCBI Taxonomy names come from the public NCBI E-utilities. Follow NCBI's own usage policy for that service.

## Code licence

MIT. Copyright (c) 2026 Junkai-lab. See [LICENSE](LICENSE).

## Development

```bash
uv sync --all-groups
uv run pytest -m "not live"   # recorded fixtures, no network
uv run pytest -o addopts= -m live   # about ten live questions
```

GitHub Actions runs the offline suite on every push. A weekly workflow runs the live questions.

## What v1 checked, and what it did not

Checked live on 16 October 2026: anonymous `fetch`, `taxon`, `culturecollectionno`, `sequence_16s`, and `sequence_genome`; `predictions=1`; SPARQL pH, isolation source, oxygen, Gram stain, and optimum-temperature shapes; NCBI E-utilities synonym resolution for *Roseobacter gallaeciensis*; the strain page and the CC BY 4.0 statement on the BacDive about page.

Not true, so not implemented: the old path `/v2/culturecolno/` (it returns HTTP 404). Partial names on the REST taxon view (`Phaeo` with `search_type=startswith` returned zero rows). QLever does not accept `xsd:float()` casts or `REGEX()` around `LCASE()`; the queries use a bare numeric comparison and `REGEX` on a variable instead.

Not claimed: a published BacDive rate limit, a SPARQL class for genome predictions, or that every pH range is a continuous growth interval beyond the predicates BacDive stores (`hasPHRangeStart`, `hasPHRangeEnd`, test type `growth`, ability `positive`).
