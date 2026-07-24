# Extra citations support for FLM

See the [FLM README file](https://github.com/phfaist/flm/blob/main/README.md).

Install with:
```bash
$ pip install flm-citations
```

Use the additional config front matter in your FLM files to enable citations
with automatic citation retrieval from arXiv, DOI, etc.
```yaml
---
$import:
  -  pkg:flm_citations
bibliography:
  - my-csl-bibliography.yaml
---
```

Then process your file as usual with `flm`.

The bibliography file(s) you provide (in the example above,
`my-csl-bibliography.yaml`) should be in CSL JSON or CSL YAML
format.  They can easily be exported from Zotero, for example.

With the default configuration, the following citation keys are
processed:
- `\cite{arXiv:XXXX.YYYYY}` - fetch citation information from
  the [arXiv](https://arxiv.org/), and from its corresponding
  DOI if applicable.
- `\cite{doi:XXX}` - fetch citation information using its DOI
- `\cite{manual:{X et al., Journal of Future Results (2034)}}` -
  manual citation text
- `\cite{bib:BibKey2023}` - use a citation from any of your
  bibliography files specified in your document front matter.

## Configuration

All options go under `flm.features.flm_citations` in your config or front
matter.

| Option | |
|---|---|
| `sources` | which citation sources to enable, and under which prefixes — see below |
| `bib_csl_style` | path to a CSL style file for rendering entries |
| `cache_dir`, `cache_base` | where the citation cache lives (default: next to the document, base `.flm-citations`) |
| `on_citation_error` | `fatal` (default) aborts the build on an unresolvable citation; `warn` logs it and renders a visible placeholder |
| `user_agent` | `User-Agent` sent with every request; arXiv and doi.org throttle harder when they cannot tell who is calling, and adding a contact address is customary |
| `drop_csl_fields` | top-level CSL fields stripped before caching (default `abstract`, `reference` — doi.org returns the cited paper's *entire* bibliography under `reference`) |
| `manual_format` | markup name `manual:` citation text is stored under (default `flm`). Setting it to anything else declares that those keys are *not* FLM, so they render verbatim rather than as markup |
| `ttl_policy`, `retry`, `max_chain_depth` | cache-expiry and retry/backoff tuning |
| `prune_cache` | drop cache entries expired past the grace window after each run (default off) |
| `write_csljson_file`, `write_bibtex_file` | export the document's citations |
| `on_event` | a callable receiving one dict per retrieval event (passes, per-source progress, requests, waits) — for driving a progress display. Not settable from YAML, since it takes a function |

Progress is otherwise reported through the `flm_citations.retrieve` logger: at
`INFO` you get passes and per-source counters, at `DEBUG` every HTTP request and
resolved citation. Nothing is reported, and no reporter is even attached, when
that logger is quieter than `INFO`.

Each entry of `sources` is `{name: …, config: {…}}`.  A source's `cite_prefix`
chooses the citation prefix it answers to, so the same kind of source can be
registered more than once — for instance a second bibliography over different
files:

```yaml
flm:
  features:
    flm_citations:
      sources:
        - $defaults:
        - $merge-config:
            name: 'bibliographyfile'
            config:
              cite_prefix: b
              bibliography_file:
                - my-other-bibliography.yaml
```

### The citation cache

Resolved citations are cached in `.flm-citations.jsonl` next to your document:
one sorted entry per line, so a diff stays readable.  Commit it if you want
builds to be reproducible offline.  A run also creates a few throwaway lock and
log files while it is in flight; they all carry a `._` prefix and delete
themselves, so one ignore rule covers them:

```gitignore
._.flm-citations*
```

Manual citation text is never written to the cache at all, so arbitrary
`\cite{manual:…}` text cannot leak into a committed file.

## Metadata Fetching

Thank you to [arXiv](https://arxiv.org/) and
[doi.org](https://doi.org/) for use of their open access
interoperability.

Since version 0.3, retrieval is handled by
[`autocitefetch`](https://github.com/phfaist/autocitefetch), a Rust library
shipped as a compiled extension module.  It brings automatic retry with
backoff, correct rate limiting, stale-while-revalidate caching, and per-citation
error tolerance.  This package keeps the FLM integration and the `citeproc-py`
rendering.

### In case `citeproc` chokes on certain entries fetched by DOI

Sometimes automatically generated citeproc/JSON entries fetched
through various available online APIs (doi.org, crossref.org,
arXiv.org, etc.) might not be fully conforming or exactly
matching the structure expected by the
[`citeproc-py` citation formatting library](https://github.com/brechtm/citeproc-py)
that this project uses.  If you run against such issues, you
might consider installing a patched version of the library that
smoothed out some issues I had in the past; you can install it
with
```
> pip install git+https://github.com/phfaist/citeproc-py.git@pr-branch
```
until my [upstream PR](https://github.com/brechtm/citeproc-py/pull/132)
is considered.

Note that `citeproc-py` needs `issued` in the
`{"date-parts": [[2022, 8, 12]]}` form; the `[{year: 2022, month: 8}]` shape
some CSL-YAML exporters produce is not accepted.

## Upgrading from 0.2

Existing configurations keep working, with two exceptions and a few
deprecations that are reported when they are used:

- **Custom Python citation source classes are gone.** A `sources` entry naming
  an importable class (`name: 'my.module.MyClass'`) is now an error; retrieval
  happens in the Rust library, which has no Python source interface.
- **`cache_file` is replaced by `cache_dir` + `cache_base`.** The cache is no
  longer a single JSON file. The old `.flm-citations.cache.json` is not read
  and can be deleted.
- **`cache_entry_duration_dt` is ignored.** Cache lifetimes are now per source
  (arXiv 10 days, DOI 360 days, bibliography files 60 s, manual entries not
  cached); only a bibliography source's lifetime is configurable, via
  `ttl_seconds`.
- **`chunk_size` and `chunk_query_delay_ms` are ignored.** Chunk sizes and
  request pacing are properties of each remote API and are fixed by the library.

Two output differences are worth knowing about:

- An arXiv entry's date is now its **last revision** (`<updated>`) rather than
  its original submission, so a paper's citation *year* may change.
- `write_csljson_file` / `write_bibtex_file` now export exactly the citations
  the document uses, rather than everything that happened to be in the cache.

## Development

```sh
pip install maturin
maturin develop            # build the extension into the current virtualenv
cargo test                 # Rust unit tests
pytest -m 'not network'    # Python tests, offline
pytest -m network          # live arXiv/doi.org checks (slow, rate-limited)
```

The Rust half depends on `autocitefetch`, pulled as a git dependency over
anonymous https; no credentials are needed to build.

## License

MIT
