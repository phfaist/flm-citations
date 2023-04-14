# Extra citations support for FLM

See the [FLM README file](https://github.com/phfaist/flm/blob/main/README.md).

Install with:
```bash
$ pip install git+https://github.com/phfaist/flm-citations.git@main
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
  
