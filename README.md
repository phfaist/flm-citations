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


### Metadata Fetching

Thank you to [arXiv](https://arxiv.org/) and
[doi.org](https://doi.org/) for use of their open access
interoperability.
