r"""End-to-end: a real FLM document rendered by the real `flm` command.

These are the tests that would catch a break in the two-pass flow the migration
reorganized — the document scan calls ``retrieve()`` once, the render calls
``get()`` per citation.  Offline throughout: only `bib` and `manual` citations.
"""

import json
import os

import pytest


DOC = r"""---
bibliography:
  - refs.yaml
---
\section{Test}

Literate programming~\cite{bib:Knuth1984}, notes~\cite{bib:PreskillNotes},
and a manual one~\cite{manual:{Me \emph{et al.}, Results (2022): part 2}}.
"""


@pytest.fixture
def workdir(bibdir):
    return bibdir


class TestRendering:

    def test_a_document_with_bib_and_manual_citations_renders(self, run_flm, workdir):
        res = run_flm(DOC, workdir=workdir)
        assert res.ok, res
        html = res.read('out.html')

        # citeproc-rendered entry, with the DOI link the feature appends.
        assert 'D. E. Knuth' in html
        assert 'The Computer Journal' in html
        assert 'https://doi.org/10.1093/comjnl/27.2.97' in html

        # Three endnotes, one per citation.
        assert html.count('class="href-endnote endnote citation"') == 3

    def test_legacy_formatted_flm_text_is_rendered_as_flm(self, run_flm, workdir):
        res = run_flm(DOC, workdir=workdir)
        assert res.ok, res
        html = res.read('out.html')
        # `\emph{...}` from the bibliography file became real FLM markup, not
        # escaped text.
        assert 'J. Preskill.' in html
        assert '<span class="textit">Lecture notes on Quantum Computation.</span>' in html

    def test_manual_citation_keeps_markup_and_colons(self, run_flm, workdir):
        res = run_flm(DOC, workdir=workdir)
        assert res.ok, res
        html = res.read('out.html')
        # The key contains a colon (which must not be re-split as a prefix) and
        # FLM markup (which must survive as markup).
        assert 'Results (2022): part 2' in html
        assert 'Me <span class="textit">et al.</span>' in html

    def test_manual_format_reaches_the_renderer(self, run_flm, workdir):
        # `manual_format` names the markup the key is written in. With a
        # non-default value the renderer must *not* treat the text as FLM —
        # otherwise the setting is silently ignored and foreign markup goes to
        # the FLM parser.
        doc = "Cited~\\cite{manual:{\\emph{not FLM}}}.\n"
        config = {
            'manual_format': 'latex',
            'sources': [{'name': 'manual', 'config': {}}],
        }
        res = run_flm(doc, config=config, workdir=workdir)
        assert res.ok, res
        html = res.read('out.html')
        assert 'verbatimtext' in html
        # Shown literally, not rendered as italics.
        assert '<span class="textit">' not in html

    def test_a_custom_prefix_source_works(self, run_flm, workdir):
        doc = r"""\section{T}
From my own bibliography~\cite{b:Knuth1984}.
"""
        config = {
            'sources': [
                {'name': 'bibliographyfile',
                 'config': {'cite_prefix': 'b', 'bibliography_file': ['refs.yaml']}},
            ],
        }
        res = run_flm(doc, config=config, workdir=workdir)
        assert res.ok, res
        assert 'D. E. Knuth' in res.read('out.html')


class TestCitationErrors:

    MISSING = r"""\section{T}
Missing~\cite{bib:NoSuchKey}.
"""

    def _config(self, on_error):
        return {
            'on_citation_error': on_error,
            'sources': [{'name': 'bibliographyfile',
                         'config': {'bibliography_file': ['refs.yaml']}}],
        }

    def test_fatal_is_the_default_and_names_the_location(self, run_flm, workdir):
        res = run_flm(self.MISSING, config=self._config('fatal'), workdir=workdir)
        assert not res.ok
        assert 'Failed to retrieve citation' in res.stderr
        assert 'bib:NoSuchKey' in res.stderr
        # The message points at the document position the citation was written.
        assert 'doc.flm' in res.stderr

    def test_warn_renders_a_placeholder_and_finishes(self, run_flm, workdir):
        # The whole point of `warn`: a citation that failed to *retrieve* is
        # exactly one that cannot be *read back*, so the render pass must not
        # re-raise or the option would be useless.
        res = run_flm(self.MISSING, config=self._config('warn'), workdir=workdir)
        assert res.ok, res
        assert 'missing citation: bib:NoSuchKey' in res.read('out.html')

    def test_an_unregistered_prefix_is_rejected(self, run_flm, workdir):
        doc = "Nope~\\cite{nosuch:key}.\n"
        res = run_flm(doc, config=self._config('warn'), workdir=workdir)
        assert not res.ok
        assert 'Invalid citation prefix' in res.stderr


class TestExports:

    def test_csljson_and_bibtex_exports(self, run_flm, workdir):
        config = {
            'write_csljson_file': 'out.csl.json',
            'write_bibtex_file': 'out.bib',
            'sources': [
                {'name': 'manual', 'config': {}},
                {'name': 'bibliographyfile',
                 'config': {'bibliography_file': ['refs.yaml']}},
            ],
        }
        res = run_flm(DOC, config=config, workdir=workdir)
        assert res.ok, res

        data = json.loads(res.read('out.csl.json'))
        ids = [entry['id'] for entry in data]
        assert 'bib:Knuth1984' in ids
        # Exactly the document's citations, in citation order — not whatever
        # else happened to be sitting in the cache directory.
        assert len(data) == 3
        assert ids[0] == 'bib:Knuth1984'

        bib = res.read('out.bib')
        assert 'Literate Programming' in bib
        assert '10.1093/comjnl/27.2.97' in bib

    def test_export_skips_citations_that_did_not_resolve(self, run_flm, workdir):
        config = {
            'on_citation_error': 'warn',
            'write_csljson_file': 'out.csl.json',
            'sources': [{'name': 'bibliographyfile',
                         'config': {'bibliography_file': ['refs.yaml']}}],
        }
        doc = "Good~\\cite{bib:Knuth1984} and bad~\\cite{bib:NoSuchKey}.\n"
        res = run_flm(doc, config=config, workdir=workdir)
        assert res.ok, res

        data = json.loads(res.read('out.csl.json'))
        # The placeholder is a rendering affordance, not data: an export must
        # not invent an entry for something that never resolved.
        assert [e['id'] for e in data] == ['bib:Knuth1984']


class TestCacheBehaviour:

    def test_a_run_leaves_only_the_entry_file(self, run_flm, workdir):
        res = run_flm(DOC, workdir=workdir)
        assert res.ok, res
        left = sorted(p for p in os.listdir(workdir) if 'flm-citations' in p)
        assert left == ['.flm-citations.jsonl']

    def test_a_second_run_reuses_the_cache(self, run_flm, workdir):
        assert run_flm(DOC, workdir=workdir).ok

        # Remove the bibliography: a second run that still resolves the entry
        # can only have read it from the cache.
        os.remove(os.path.join(workdir, 'refs.yaml'))
        res = run_flm(DOC.replace('  - refs.yaml\n', ''), workdir=workdir)
        assert res.ok, res
        assert 'D. E. Knuth' in res.read('out.html')

    def test_prune_cache_keeps_live_entries(self, run_flm, workdir):
        # `prune` drops only what is expired past the grace window, so a run
        # that just fetched everything must keep all of it.
        config = {
            'prune_cache': True,
            'sources': [{'name': 'bibliographyfile',
                         'config': {'bibliography_file': ['refs.yaml']}}],
        }
        doc = "Cited~\\cite{bib:Knuth1984}.\n"
        res = run_flm(doc, config=config, workdir=workdir)
        assert res.ok, res
        assert 'D. E. Knuth' in res.read('out.html')
        cache = os.path.join(workdir, '.flm-citations.jsonl')
        with open(cache, encoding='utf-8') as f:
            assert 'bib:Knuth1984' in f.read()

    def test_manual_text_never_lands_in_the_cache_file(self, run_flm, workdir):
        res = run_flm(DOC, workdir=workdir)
        assert res.ok, res
        cache = os.path.join(workdir, '.flm-citations.jsonl')
        with open(cache, encoding='utf-8') as f:
            assert 'Results (2022)' not in f.read()
