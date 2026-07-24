r"""The extension module's behaviour, exercised directly (no FLM involved).

These cover the Rust↔Python surface: source-spec validation, the two-phase
``retrieve``/``get`` flow, the cache's file layout, and progress reporting.
Everything here is offline — only the `bib` and `manual` sources are used.
"""

import logging
import os

import pytest

from flm_citations._autocitefetch import CitationError, CitationManager


def bib(prefix, files, **kw):
    return dict(kind='bib', prefix=prefix, files=[str(f) for f in files], **kw)


def manual(prefix='manual', fmt='flm'):
    return {'kind': 'manual', 'prefix': prefix, 'format': fmt}


class TestSourceSpecs:

    def test_prefix_is_independent_of_kind(self, make_manager, bibdir):
        # A source declares no prefix, so the same kind can back several — this
        # is what the `cite_prefix:` config option has always meant.
        mgr = make_manager([
            bib('bib', [bibdir / 'refs.yaml']),
            bib('b', [bibdir / 'refs.json']),
        ])
        assert mgr.prefixes == ['bib', 'b']

        assert not mgr.retrieve([('bib', 'Knuth1984'), ('b', 'Knuth1984Json')])
        assert mgr.get('bib', 'Knuth1984')['title'] == 'Literate Programming'
        assert mgr.get('b', 'Knuth1984Json')['title'] == 'Literate Programming'

    def test_two_sources_cannot_share_a_prefix(self, make_manager, bibdir):
        # `register` would silently let the second replace the first, which for
        # a config file means one of the blocks the user wrote does nothing.
        with pytest.raises(ValueError, match='more than one citation source'):
            make_manager([
                bib('bib', [bibdir / 'refs.yaml']),
                bib('bib', [bibdir / 'refs.json']),
            ])

    def test_arxiv_chain_target_must_be_registered(self, make_manager):
        # The realistic mistake: renaming the DOI source without updating
        # arXiv's chain target. The library would only surface this as a
        # per-citation failure much later.
        with pytest.raises(ValueError, match='chains to `doi:`'):
            make_manager([
                {'kind': 'arxiv', 'prefix': 'arxiv', 'chain_dois_to': 'doi'},
                {'kind': 'doi', 'prefix': 'dx'},
            ])

    def test_arxiv_chaining_can_be_switched_off(self, make_manager):
        mgr = make_manager([
            {'kind': 'arxiv', 'prefix': 'arxiv', 'chain_dois_to': None},
        ])
        assert mgr.prefixes == ['arxiv']

    def test_unknown_kind_and_option_are_rejected(self, make_manager, bibdir):
        with pytest.raises(ValueError, match='unknown citation source kind'):
            make_manager([{'kind': 'bibtex', 'prefix': 'bib'}])
        with pytest.raises(ValueError, match='unknown option'):
            make_manager([bib('bib', [bibdir / 'refs.yaml'], chunk_size=10)])

    def test_colon_in_prefix_is_rejected(self, make_manager, bibdir):
        # Citation ids are `prefix:key` split at the first colon, so a colon in
        # the prefix makes them ambiguous.
        with pytest.raises(ValueError, match='must not contain'):
            make_manager([bib('b:x', [bibdir / 'refs.yaml'])])

    def test_manual_format_is_required_not_defaulted(self, make_manager):
        # No default here on purpose: the extension cannot know what markup its
        # host renders, and a default would be a second place for the Python
        # layer's `manual_format` to disagree with.
        with pytest.raises(ValueError, match='missing `format`'):
            make_manager([{'kind': 'manual', 'prefix': 'manual'}])

    def test_bib_entries_can_be_passed_as_data(self, make_manager):
        mgr = make_manager([{
            'kind': 'bib', 'prefix': 'bib',
            'entries': {'X': {'type': 'book', 'title': 'Preloaded'}},
        }])
        assert not mgr.retrieve([('bib', 'X')])
        assert mgr.get('bib', 'X')['title'] == 'Preloaded'


class TestRetrieveAndGet:

    def test_yaml_and_json_bibliographies_both_load(self, make_manager, bibdir):
        mgr = make_manager([bib('y', [bibdir / 'refs.yaml']),
                            bib('j', [bibdir / 'refs.json'])])
        assert not mgr.retrieve([('y', 'Knuth1984'), ('j', 'Knuth1984Json')])

    def test_missing_key_is_reported_not_raised(self, make_manager, bibdir):
        mgr = make_manager([bib('bib', [bibdir / 'refs.yaml'])])
        failures = mgr.retrieve([('bib', 'Knuth1984'), ('bib', 'Nope')])

        # One bad citation must not abort the batch.
        assert [f.key for f in failures] == ['Nope']
        assert failures[0].prefix == 'bib'
        assert failures[0].origin is None
        assert 'not found' in failures[0].message
        assert mgr.get('bib', 'Knuth1984')['title'] == 'Literate Programming'

    def test_get_without_retrieve_raises(self, make_manager, bibdir):
        mgr = make_manager([bib('bib', [bibdir / 'refs.yaml'])])
        with pytest.raises(CitationError):
            mgr.get('bib', 'Knuth1984')

    def test_id_is_rewritten_to_prefix_key(self, make_manager, bibdir):
        mgr = make_manager([bib('b', [bibdir / 'refs.yaml'])])
        mgr.retrieve([('b', 'Knuth1984')])
        assert mgr.get('b', 'Knuth1984')['id'] == 'b:Knuth1984'

    def test_bib_keys_are_case_sensitive(self, make_manager, bibdir):
        # A bib key is an opaque user-chosen label matched byte-for-byte
        # against the file, unlike a DOI. Folding it would silently miss.
        mgr = make_manager([bib('bib', [bibdir / 'refs.yaml'])])
        failures = mgr.retrieve([('bib', 'knuth1984')])
        assert [f.key for f in failures] == ['knuth1984']

    def test_legacy_formatted_flm_text_passes_through(self, make_manager, bibdir):
        mgr = make_manager([bib('bib', [bibdir / 'refs.yaml'])])
        mgr.retrieve([('bib', 'PreskillNotes')])
        item = mgr.get('bib', 'PreskillNotes')
        assert r'\emph{Lecture notes' in item['_formatted_flm_text']


class TestManualSource:

    def test_key_is_the_text_under_the_configured_format(self, make_manager):
        mgr = make_manager([manual()])
        # A colon and FLM markup in the key: both are payload, not syntax.
        key = r'Me \emph{et al.}, Journal of Results (2022): part 2'
        assert not mgr.retrieve([('manual', key)])
        assert mgr.get('manual', key)['_ready_formatted'] == {'flm': key}

    def test_format_name_is_configuration(self, make_manager):
        mgr = make_manager([manual(fmt='latex')])
        mgr.retrieve([('manual', 'X')])
        assert mgr.get('manual', 'X')['_ready_formatted'] == {'latex': 'X'}

    def test_case_and_whitespace_are_significant(self, make_manager):
        # `normalize_key` is the identity here: the key *is* the rendered text.
        mgr = make_manager([manual()])
        mgr.retrieve([('manual', '  Padded Text  ')])
        assert mgr.get('manual', '  Padded Text  ')['_ready_formatted']['flm'] \
            == '  Padded Text  '


class TestCacheFiles:

    def test_only_the_jsonl_survives_a_run(self, make_manager, bibdir, tmp_path):
        mgr = make_manager([bib('bib', [bibdir / 'refs.yaml'])])
        mgr.retrieve([('bib', 'Knuth1984')])

        left = sorted(p for p in os.listdir(tmp_path) if 'flm-citations' in p)
        # Every throwaway file carries a `._` prefix and deletes itself, so a
        # finished run leaves only the committable entry file.
        assert left == ['.flm-citations.jsonl']

    def test_a_second_manager_reuses_the_cache(self, make_manager, bibdir, tmp_path):
        make_manager([bib('bib', [bibdir / 'refs.yaml'])]).retrieve([('bib', 'Knuth1984')])

        # Point the second manager at a bibliography that no longer exists: if
        # it answers, the answer came from the cache.
        os.remove(bibdir / 'refs.yaml')
        mgr = make_manager([bib('bib', [bibdir / 'refs.yaml'])])
        assert mgr.get('bib', 'Knuth1984')['title'] == 'Literate Programming'

    def test_manual_entries_are_never_persisted(self, make_manager, tmp_path):
        # TTL 0 makes them ephemeral: readable for the run, never written, so
        # arbitrary citation text cannot leak into a committed file.
        mgr = make_manager([manual()])
        mgr.retrieve([('manual', 'Secret Unpublished Note')])
        assert mgr.get('manual', 'Secret Unpublished Note')  # readable now

        cache = tmp_path / '.flm-citations.jsonl'
        if cache.exists():
            assert 'Secret' not in cache.read_text()

    def test_drop_csl_fields_never_reaches_the_cache(self, make_manager, tmp_path):
        mgr = make_manager(
            [{'kind': 'bib', 'prefix': 'bib', 'entries': {
                'X': {'type': 'book', 'title': 'T', 'abstract': 'LONG ABSTRACT'},
            }}],
            drop_csl_fields=['abstract'],
        )
        mgr.retrieve([('bib', 'X')])
        assert 'abstract' not in mgr.get('bib', 'X')
        assert 'LONG ABSTRACT' not in (tmp_path / '.flm-citations.jsonl').read_text()


class TestReporting:

    def test_events_reach_an_on_event_callback(self, make_manager, bibdir):
        events = []
        mgr = make_manager([bib('bib', [bibdir / 'refs.yaml'])],
                           verbosity=2, on_event=events.append)
        mgr.retrieve([('bib', 'Knuth1984')])

        kinds = {e['type'] for e in events}
        assert {'retrieve_started', 'pass_started', 'source_started',
                'cite_resolved', 'retrieve_finished'} <= kinds
        assert any(e['type'] == 'cite_resolved' and e['key'] == 'Knuth1984'
                   for e in events)

    def test_logging_records_are_emitted(self, make_manager, bibdir, caplog):
        caplog.set_level(logging.DEBUG, logger='flm_citations.retrieve')
        mgr = make_manager([bib('bib', [bibdir / 'refs.yaml'])], verbosity=2)
        mgr.retrieve([('bib', 'Knuth1984')])

        messages = [r.message for r in caplog.records]
        assert any('retrieving 1 citation' in m for m in messages)
        assert any('retrieval done' in m for m in messages)

    # The exception surfaces as an *unraisable* — which is the point: the
    # binding reports it via `PyErr::write_unraisable` and swallows it, exactly
    # as `Reporter::report`'s "must not panic, returns nothing" contract needs.
    @pytest.mark.filterwarnings('ignore::pytest.PytestUnraisableExceptionWarning')
    def test_a_raising_callback_does_not_break_the_run(self, make_manager, bibdir):
        # `Reporter::report` must never influence control flow: a broken
        # progress display cannot be allowed to abort a document build.
        def boom(event):
            raise RuntimeError('progress bar exploded')

        mgr = make_manager([bib('bib', [bibdir / 'refs.yaml'])],
                           verbosity=1, on_event=boom)
        assert not mgr.retrieve([('bib', 'Knuth1984')])
        assert mgr.get('bib', 'Knuth1984')['title'] == 'Literate Programming'

    def test_verbosity_zero_logs_nothing(self, make_manager, bibdir, caplog):
        caplog.set_level(logging.DEBUG, logger='flm_citations.retrieve')
        mgr = make_manager([bib('bib', [bibdir / 'refs.yaml'])], verbosity=0)
        mgr.retrieve([('bib', 'Knuth1984')])
        assert not caplog.records


class TestPolicies:

    def test_ttl_and_retry_policies_are_accepted(self, make_manager, bibdir):
        mgr = make_manager(
            [bib('bib', [bibdir / 'refs.yaml'])],
            ttl_policy={'stale_percent': 50, 'jitter_percent': 5, 'grace_days': 3},
            retry={'max_retries': 1, 'base_ms': 10, 'cap_ms': 100},
            max_chain_depth=4,
        )
        assert not mgr.retrieve([('bib', 'Knuth1984')])

    def test_unknown_policy_keys_are_rejected(self, make_manager, bibdir):
        with pytest.raises(ValueError, match='unknown option'):
            make_manager([bib('bib', [bibdir / 'refs.yaml'])],
                         ttl_policy={'stale_pct': 50})
