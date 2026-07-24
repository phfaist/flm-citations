r"""Live retrieval from arXiv and doi.org.

Deselected by default (``pytest -m 'not network'``).  These hit rate-limited
public APIs — arXiv asks for one request per ~3 s and answers 429 when it is
unhappy — so they are slow by construction and will fail on a bad day for
reasons that have nothing to do with this package.  Run them by hand before a
release; they are the only check that the arXiv → DOI chain and doi.org content
negotiation still work end to end.
"""

import pytest

pytestmark = pytest.mark.network


#: A paper with a published DOI, so `arxiv:` chains to `doi:`.
ARXIV_ID = '1211.3141'
#: Einstein–Podolsky–Rosen, 1935.
DOI = '10.1103/PhysRev.47.777'

USER_AGENT = 'flm-citations-tests/0.3.0 (+https://github.com/phfaist/flm-citations)'


@pytest.fixture
def online_manager(tmp_path):
    from flm_citations._autocitefetch import CitationManager

    def _make(**kwargs):
        return CitationManager(
            {'sources': [
                {'kind': 'arxiv', 'prefix': 'arxiv', 'chain_dois_to': 'doi'},
                {'kind': 'doi', 'prefix': 'doi'},
            ]},
            cache_dir=str(tmp_path), cache_base='.flm-citations',
            user_agent=USER_AGENT, **kwargs,
        )

    return _make


def test_doi_content_negotiation(online_manager):
    mgr = online_manager()
    failures = mgr.retrieve([('doi', DOI)])
    assert not failures, [repr(f) for f in failures]

    item = mgr.get('doi', DOI)
    assert 'Einstein' in str(item['author'])
    # The cache id is lowercased — DOIs are case-insensitive identifiers, so
    # every spelling of one collapses to a single entry and a single fetch.
    assert item['id'] == 'doi:' + DOI.lower()
    # The CSL `DOI` field, by contrast, is whatever doi.org sent, verbatim.
    # (For this DOI that happens to be lowercase already; the point is only
    # that the field is passed through rather than derived from the cache id.)
    assert item['DOI'].lower() == DOI.lower()


def test_a_mixed_case_doi_finds_the_same_entry(online_manager):
    mgr = online_manager()
    assert not mgr.retrieve([('doi', DOI)])
    # Requested in a different case: same cache id, no second fetch.
    assert mgr.get('doi', DOI.upper())['id'] == 'doi:' + DOI.lower()


def test_arxiv_chains_to_doi(online_manager):
    mgr = online_manager()
    failures = mgr.retrieve([('arxiv', ARXIV_ID)])
    assert not failures, [repr(f) for f in failures]

    item = mgr.get('arxiv', ARXIV_ID)
    # `get` walked the chain to the DOI entry, merged the pointer's
    # `set_properties` over it, then forced the requested id back on.
    assert item['id'] == f'arxiv:{ARXIV_ID}'
    assert item['arxivid'] == ARXIV_ID
    assert 'DOI' in item


def test_arxiv_without_chaining_keeps_its_own_metadata(tmp_path):
    from flm_citations._autocitefetch import CitationManager

    mgr = CitationManager(
        {'sources': [{'kind': 'arxiv', 'prefix': 'ax', 'chain_dois_to': None}]},
        cache_dir=str(tmp_path), cache_base='.nochain',
        user_agent=USER_AGENT,
    )
    assert not mgr.retrieve([('ax', ARXIV_ID)])
    item = mgr.get('ax', ARXIV_ID)
    assert item['type'] == 'article-journal'
    assert item['arxivid'] == ARXIV_ID
    # arXiv's own `issued` is the last-revision date, from `<updated>`.
    assert 'issued' in item


def test_the_second_retrieve_issues_no_requests(online_manager):
    mgr = online_manager()
    mgr.retrieve([('doi', DOI)])

    events = []
    mgr2 = online_manager(verbosity=2, on_event=events.append)
    assert not mgr2.retrieve([('doi', DOI)])
    assert not [e for e in events if e['type'] == 'request_started'], \
        "a warm cache must not hit the network"
