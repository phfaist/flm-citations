r"""Translating the feature's YAML configuration into source specs.

The config shape did not change with the migration, but what several of its
options *mean* did.  These tests pin the translation, and pin that every option
that no longer does anything says so rather than being silently dropped.
"""

import os.path
import warnings

import pytest

from flm_citations import _config


# The document directory these tests pretend to run in, spelled the way the
# platform spells an absolute path: resolution goes through `os.path`, so on
# Windows a bare `/docs` comes back as `D:\docs` (`abspath` stamps on the
# current drive) with backslashes throughout.  Expectations are built with
# `docpath` from the same functions, so they compare equal on every platform.
DOCDIR = os.path.abspath('/docs')


def docpath(*parts):
    return os.path.join(DOCDIR, *parts)


class FakeDoc:
    def __init__(self, **metadata):
        self.metadata = {'jobname': 'mydoc',
                         'filepath': {'dirname': DOCDIR},
                         **metadata}


@pytest.fixture
def doc():
    return FakeDoc()


def build(sources, doc, cwd=DOCDIR):
    return _config.build_source_specs(sources, doc, cwd)


def build_quietly(sources, doc, cwd=DOCDIR):
    """Build, returning `(specs, [deprecation messages])`."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        specs = build(sources, doc, cwd)
    return specs, [str(w.message) for w in caught
                   if issubclass(w.category, DeprecationWarning)]


class TestSourceNames:

    def test_defaults_cover_the_four_sources(self, doc):
        specs = build(None, doc)
        assert [s['kind'] for s in specs] == ['arxiv', 'doi', 'manual', 'bib']
        assert [s['prefix'] for s in specs] == ['arxiv', 'doi', 'manual', 'bib']

    def test_bibliographyfile_is_the_bib_kind(self, doc):
        specs = build([{'name': 'bibliographyfile'}], doc)
        assert specs[0]['kind'] == 'bib'
        assert specs[0]['prefix'] == 'bib'

    def test_cite_prefix_chooses_the_prefix(self, doc):
        specs = build(
            [{'name': 'bibliographyfile', 'config': {'cite_prefix': 'b'}}], doc)
        assert specs[0]['prefix'] == 'b'

    def test_an_empty_cite_prefix_is_rejected(self, doc):
        # `pop(...) or kind` would silently substitute the default here; an
        # explicit empty prefix is a mistake worth naming.
        with pytest.raises(ValueError, match='cite_prefix'):
            build([{'name': 'doi', 'config': {'cite_prefix': ''}}], doc)

    def test_a_colon_in_cite_prefix_is_rejected(self, doc):
        with pytest.raises(ValueError, match='cite_prefix'):
            build([{'name': 'doi', 'config': {'cite_prefix': 'a:b'}}], doc)

    def test_custom_python_class_is_rejected_clearly(self, doc):
        with pytest.raises(ValueError, match='no longer supported'):
            build([{'name': 'my.module.MyClass'}], doc)

    def test_unknown_name_is_rejected(self, doc):
        with pytest.raises(ValueError, match='unknown citation source'):
            build([{'name': 'bibtex'}], doc)


class TestArxivOptions:

    def test_chain_to_doi_false_disables_chaining(self, doc):
        specs = build([{'name': 'arxiv', 'config': {'chain_to_doi': False}}], doc)
        assert specs[0]['chain_dois_to'] is None

    def test_chain_target_follows_a_renamed_doi_source(self, doc):
        # Prefixes are host-chosen now, so a `cite_prefix: dx` DOI source is
        # legitimate — and would silently break every chained arXiv citation if
        # arXiv kept pointing at a literal `doi`.
        specs = build([{'name': 'arxiv'},
                       {'name': 'doi', 'config': {'cite_prefix': 'dx'}}], doc)
        assert specs[0]['chain_dois_to'] == 'dx'

    def test_no_doi_source_means_no_chaining(self, doc):
        # Chaining would point at a prefix nobody registered, manufacturing
        # failures for citations the user never wrote.
        specs = build([{'name': 'arxiv'}], doc)
        assert specs[0]['chain_dois_to'] is None

    def test_several_doi_sources_is_a_clear_error(self, doc):
        # This layer knows what the user wrote and can name both candidates;
        # left to the extension it would only see a `doi` prefix that appears
        # nowhere in their config.
        with pytest.raises(ValueError, match='several DOI'):
            build([{'name': 'arxiv'},
                   {'name': 'doi'},
                   {'name': 'doi', 'config': {'cite_prefix': 'dx'}}], doc)

    def test_explicit_chain_target_wins(self, doc):
        specs = build([{'name': 'arxiv', 'config': {'chain_dois_to': 'zzz'}},
                       {'name': 'doi'}], doc)
        assert specs[0]['chain_dois_to'] == 'zzz'

    def test_the_two_spellings_of_an_option_cannot_both_be_given(self, doc):
        with pytest.raises(ValueError, match='same option'):
            build([{'name': 'arxiv', 'config': {
                'override_arxiv_dois': {'a': 'b'},
                'override_dois': {'c': 'd'},
            }}, {'name': 'doi'}], doc)

    def test_an_override_file_url_is_rejected(self, doc):
        # Unlike a bibliography file, this one is read directly rather than
        # fetched, so a URL would fail with a bare "no such file".
        with pytest.raises(ValueError, match='local path, not a URL'):
            build([{'name': 'arxiv', 'config': {
                'override_arxiv_dois_file': 'https://example.org/dois.yaml',
            }}, {'name': 'doi'}], doc)

    def test_doi_overrides_are_passed_as_data(self, doc):
        specs = build([{'name': 'arxiv', 'config': {
            'override_arxiv_dois': {'1211.1037': '10.1/x', '0704.0001': None},
        }}, {'name': 'doi'}], doc)
        assert specs[0]['override_dois'] == {'1211.1037': '10.1/x',
                                             '0704.0001': None}


class TestBibliographyFiles:

    def test_paths_are_resolved_against_the_document(self, doc):
        specs = build([{'name': 'bibliographyfile',
                        'config': {'bibliography_file': 'refs.yaml'}}], doc)
        assert specs[0]['files'] == [docpath('refs.yaml')]

    def test_urls_are_left_alone(self, doc):
        specs = build([{'name': 'bibliographyfile', 'config': {
            'bibliography_file': ['https://example.org/refs.json'],
        }}], doc)
        assert specs[0]['files'] == ['https://example.org/refs.json']

    def test_jobname_is_substituted(self, doc):
        specs = build([{'name': 'bibliographyfile', 'config': {
            'bibliography_file': ['${jobname}.bib.json'],
        }}], doc)
        assert specs[0]['files'] == [docpath('mydoc.bib.json')]

    def test_front_matter_bibliography_is_the_default(self, doc):
        doc.metadata['bibliography'] = ['a.yaml', 'b.json']
        specs = build([{'name': 'bibliographyfile'}], doc)
        assert specs[0]['files'] == [docpath('a.yaml'), docpath('b.json')]

    def test_a_bare_string_bibliography_is_accepted(self, doc):
        doc.metadata['bibliography'] = 'only.yaml'
        specs = build([{'name': 'bibliographyfile'}], doc)
        assert specs[0]['files'] == [docpath('only.yaml')]

    def test_an_empty_bibliography_key_is_not_an_error(self, doc):
        # `bibliography:` with nothing under it parses as None — an ordinary
        # thing to leave behind while editing front matter.
        doc.metadata['bibliography'] = None
        specs = build([{'name': 'bibliographyfile'}], doc)
        assert specs[0]['files'] == []


class TestDeprecatedOptions:

    @pytest.mark.parametrize('option', [
        'chunk_size', 'chunk_query_delay_ms', 'use_requests',
        'chains_to_sources', 'source_name',
    ])
    def test_obsolete_source_options_warn_and_are_dropped(self, doc, option):
        specs, msgs = build_quietly(
            [{'name': 'arxiv', 'config': {option: 1}}, {'name': 'doi'}], doc)
        assert any(option in m for m in msgs), msgs
        assert option not in specs[0]

    def test_unknown_source_option_is_an_error_not_a_warning(self, doc):
        # A typo must not present as "the option did nothing".
        with pytest.raises(ValueError, match='unknown option'):
            build([{'name': 'arxiv', 'config': {'chian_to_doi': True}},
                   {'name': 'doi'}], doc)

    def test_cache_file_becomes_a_directory_and_base(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            d, base = _config.normalize_cache_location(
                '.flm-citations.cache.json', None, None, DOCDIR)
        assert (d, base) == (DOCDIR, '.flm-citations')
        assert any('cache_file' in str(w.message) for w in caught)

    def test_cache_file_directory_component_is_honoured(self):
        # A relative directory stays relative to the *document*, as the old
        # `os.path.join(cwd, cache_file)` did.
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            d, base = _config.normalize_cache_location(
                'sub/.cites.json', None, None, DOCDIR)
        assert (d, base) == (docpath('sub'), '.cites')

    def test_cache_defaults(self):
        assert _config.normalize_cache_location(None, None, None, DOCDIR) \
            == (DOCDIR, '.flm-citations')

    def test_cache_entry_duration_warns(self):
        import datetime
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            _config.warn_obsolete_feature_options(datetime.timedelta(days=30))
        assert any('cache_entry_duration_dt' in str(w.message) for w in caught)


class TestVerbosity:

    @pytest.mark.parametrize('level,expected', [
        ('DEBUG', 2), ('INFO', 1), ('WARNING', 0), ('CRITICAL', 0),
    ])
    def test_verbosity_follows_the_logger(self, level, expected):
        import logging
        log = logging.getLogger('flm_citations.test.verbosity')
        log.setLevel(getattr(logging, level))
        assert _config.verbosity_from_logger(log) == expected
