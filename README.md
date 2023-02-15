# Extra citations support for LLM

See the [LLM README file](https://github.com/phfaist/llm/blob/main/README.md).

Install with:
```bash
$ pip install git+https://github.com/phfaist/llm-citations.git@main
```

Use the additional config front matter in your LLM files to enable citations
with automatic citation retrieval from arXiv, DOI, etc.
```yaml
---
$import: pkg:llm_citations
bibliography:
  - my-csl-bibliography.yaml
---
```

Then process your file as usual with `llm`.
