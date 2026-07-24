r"""The ``flm_citations`` FLM feature: ``\cite{arxiv:…}`` and friends.

This module is the *presentation* half of the package.  Everything to do with
obtaining a citation — HTTP, retry and backoff, rate limiting, the on-disk
cache and its expiry policy, arXiv Atom parsing, doi.org content negotiation,
bibliography files, and following ``arxiv:`` → ``doi:`` chains — lives in the
``autocitefetch`` Rust library, reached through
:mod:`flm_citations._autocitefetch`.  What is left here is FLM- and
CSL-shaped: scanning the document for citations, rendering CSL-JSON to FLM
markup with ``citeproc-py``, and exporting BibTeX/CSL-JSON.

The library's two-phase model lines up exactly with FLM's two passes:

* :meth:`DocumentManager.flm_main_scan_fragment` walks the document and calls
  ``retrieve()`` once, which fetches everything (including chain targets it
  discovers on the way) and returns only a per-citation failure report;
* :meth:`RenderManager.get_citation_content_flm` calls ``get()`` per citation
  while rendering, which reads an item back and follows any chain pointer.
"""

import json
import warnings
import copy
import os.path
from urllib.parse import quote as urlquote

import logging
logger = logging.getLogger(__name__)

# Progress from the retrieval library lands on this logger; its level is what
# decides whether a reporter is attached at all.
retrieve_logger = logging.getLogger(__name__.rsplit('.', 1)[0] + '.retrieve')

from pylatexenc.latexnodes import LatexWalkerError

from flm.feature.cite import (
    FeatureExternalPrefixedCitations,
)

import citeproc
import citeproc.source
import citeproc.source.json

from . import _cslformatter
from . import _config
from ._autocitefetch import CitationError, CitationManager, __version__
from .flmcitationsscanner import CitationsScanner


#: The name of the markup *this* renderer speaks.  Pre-formatted citation text
#: is carried as ``{"_ready_formatted": {<format>: <text>}}``, and this is the
#: key whose value can go to the FLM parser as-is; text under any other name is
#: some other markup and is shown verbatim.
#:
#: Distinct from the ``manual_format`` option, which says what the ``manual``
#: source should *tag* its text as.  The two are the same by default — a
#: ``\cite{manual:…}`` key is FLM — and a host that sets ``manual_format`` to
#: something else is declaring that those keys are *not* FLM, which is exactly
#: what makes them render verbatim.
FLM_FORMAT_NAME = 'flm'

#: Default for the ``manual_format`` option: a manual citation's key is FLM
#: markup unless the document says otherwise.
MANUAL_FORMAT = FLM_FORMAT_NAME

#: Sent with every HTTP request.  arXiv and doi.org both ask callers to
#: identify themselves, and both throttle harder when they cannot tell who is
#: calling; the library's own default names *it* rather than this package.
#: Override with the ``user_agent`` option — adding a contact e-mail address is
#: customary and is what crossref's documentation asks for.
DEFAULT_USER_AGENT = (
    f'flm-citations/{__version__} (+https://github.com/phfaist/flm-citations)'
)

#: Top-level CSL fields dropped on the way into the cache.  doi.org routinely
#: returns a ``reference`` array holding the cited paper's *entire*
#: bibliography, and an ``abstract`` several kilobytes long; neither is used by
#: any citation style here, and both otherwise bloat the cache file.
DEFAULT_DROPPED_CSL_FIELDS = ['abstract', 'reference']


class FeatureCiteAuto(FeatureExternalPrefixedCitations):
    r"""
    Citations with automatic retrieval from arXiv, doi.org, bibliography files
    and manual entries.

    Arguments:

    - `sources` — a list of ``{'name': …, 'config': {…}}`` dicts naming the
      citation sources to enable.  Recognized names are ``arxiv``, ``doi``,
      ``manual`` and ``bibliographyfile``; each source's ``cite_prefix`` config
      option chooses the citation prefix it answers to, so the same kind of
      source can be registered more than once (two bibliography sources over
      different files, say).

    - `cache_dir`, `cache_base` — where the citation cache lives.  The cache is
      a family of files sharing `cache_base`: ``<base>.jsonl`` holds the
      entries, and the throwaway lock/log files a run creates all carry a
      ``._<base>`` prefix and delete themselves.

    - `on_citation_error` — ``'fatal'`` (the default, and the historical
      behaviour) aborts the build on a citation that will not resolve;
      ``'warn'`` logs it and carries on, so a network outage does not block a
      document.

    - ... further arguments are passed on to
      `flm.feature.cite.FeatureExternalPrefixedCitations`.
    """

    add_arxiv_link = True
    add_doi_link = True
    add_url_link = 'only-if-no-other-link' # = only added if there's no arxiv or doi link

    # inherited!!
    #feature_name = 'citations'

    feature_default_config = {
        'sources': _config.DEFAULT_SOURCES,
    }

    class RenderManager(FeatureExternalPrefixedCitations.RenderManager):

        def get_citation_content_flm(self, cite_prefix, cite_key, resource_info):

            fdocmgr = self.feature_document_manager

            csljson = fdocmgr.get_citation_csljson_for_render(cite_prefix, cite_key)

            result = _generate_citation_flm_from_citeprocjsond(
                csljson,
                bib_csl_style=fdocmgr.bib_csl_style,
                what=str(resource_info),
                flm_environment=self.render_context.doc.environment,
                add_arxiv_link=self.feature.add_arxiv_link,
                add_doi_link=self.feature.add_doi_link,
                add_url_link=self.feature.add_url_link,
            )

            return result

        def postprocess(self, value):
            if (self.feature.write_csljson_file is None
                    and self.feature.write_bibtex_file is None):
                return

            # Built once and shared: every entry costs a call across the
            # extension boundary plus a full CSL-JSON rebuild.
            fdocmgr = self.feature_document_manager
            csljson_data = fdocmgr.get_export_csljson_data()

            if self.feature.write_csljson_file is not None:
                fname = self.feature.write_csljson_file

                with open(fname, 'w', encoding='utf-8') as fw:
                    json.dump(csljson_data, fw, indent=4)

                logger.info("Wrote CSL-JSON file ‘%s’", fname)

            if self.feature.write_bibtex_file is not None:
                fname = self.feature.write_bibtex_file

                # Safe to hand over the same list: the BibTeX pass patches the
                # entries in place, and the JSON dump above has already run.
                bibtex_content = fdocmgr.get_export_bibtex_content(csljson_data)

                with open(fname, 'w', encoding='utf-8') as fw:
                    fw.write( bibtex_content )

                logger.info("Wrote bibtex file ‘%s’", fname)


    class DocumentManager(FeatureExternalPrefixedCitations.DocumentManager):

        def initialize(self):
            super().initialize()

            doc = self.doc
            self.cwd = _config.document_cwd(doc)

            source_specs = _config.build_source_specs(
                self.feature.sources, doc, self.cwd,
                manual_format=self.feature.manual_format,
            )

            cache_dir, cache_base = _config.normalize_cache_location(
                self.feature.cache_file, self.feature.cache_dir,
                self.feature.cache_base, self.cwd,
            )
            _config.warn_obsolete_feature_options(self.feature.cache_entry_duration_dt)

            self.citation_manager = CitationManager(
                {'sources': source_specs},
                cache_dir=cache_dir,
                cache_base=cache_base,
                user_agent=self.feature.user_agent,
                drop_csl_fields=self.feature.drop_csl_fields,
                ttl_policy=self.feature.ttl_policy,
                retry=self.feature.retry,
                max_chain_depth=self.feature.max_chain_depth,
                # The logger is handed over rather than named across the
                # boundary, so its name lives in exactly one place — here, where
                # its level also decides how much the library reports.
                logger=retrieve_logger,
                verbosity=_config.verbosity_from_logger(retrieve_logger),
                on_event=self.feature.on_event,
            )
            self.cite_prefixes = list(self.citation_manager.prefixes)

            # Citations encountered in the document, in first-seen order.  This
            # is what the CSL-JSON/BibTeX export walks: the old implementation
            # exported the whole cache file, which pulled in entries left by
            # other documents that happened to share a directory.
            self.encountered_cites = []

            # Citations the retrieval pass already reported on, so the render
            # pass does not warn about the same one a second time.
            self.failed_cites = set()

            bib_csl_style = self.feature.bib_csl_style
            if bib_csl_style is None:
                bib_csl_style = os.path.join(
                    os.path.dirname(__file__),
                    'american-physical-society-et-al--patched.csl'
                )
                #"harvard1"
            self.bib_csl_style = \
                citeproc.CitationStylesStyle(bib_csl_style, validate=False)

            logger.debug("citation prefixes are %r", self.cite_prefixes)


        def get_citation_csljson(self, cite_prefix, cite_key):
            r"""Read one resolved citation back, following any chain pointer.

            Strict: an unresolved citation raises.  The export path wants that
            (it must not invent entries); the render path goes through
            :meth:`get_citation_csljson_for_render` instead.
            """
            try:
                return self.citation_manager.get(cite_prefix, cite_key)
            except CitationError as e:
                raise LatexWalkerError(f"No citation found: {e}") from e


        def get_citation_csljson_for_render(self, cite_prefix, cite_key):
            r"""As :meth:`get_citation_csljson`, honouring ``on_citation_error``.

            Under ``on_citation_error: 'warn'`` an unresolved citation must not
            abort the build *here* either — it would make the option useless,
            since a citation that failed to retrieve is exactly one that cannot
            be read back.  Render a visible placeholder instead, so the reader
            can see which reference is missing rather than silently getting a
            document with a wrong bibliography.
            """
            try:
                return self.get_citation_csljson(cite_prefix, cite_key)
            except LatexWalkerError:
                if self.feature.on_citation_error == 'fatal':
                    raise

                cite_id = f"{cite_prefix}:{cite_key}"
                # The retrieval pass already warned about anything it tried to
                # fetch; warning again per citation would double every line.
                if (cite_prefix, cite_key) not in self.failed_cites:
                    logger.warning("No citation information for ‘%s’", cite_id)
                    self.failed_cites.add( (cite_prefix, cite_key) )

                # Tagged with the renderer's own markup name, not the
                # `manual_format` option: this text is ours and it is FLM.
                return {
                    'id': cite_id,
                    '_ready_formatted': {
                        FLM_FORMAT_NAME: (
                            r'\begin{verbatimtext}[missing citation: ' + cite_id
                            + r']\end{verbatimtext}'
                        ),
                    },
                }


        def get_export_csljson_data(self):

            fullciteprocjsond = []
            for cite_prefix, cite_key in self.encountered_cites:
                the_id = f"{cite_prefix}:{cite_key}"
                try:
                    data = dict( self.get_citation_csljson(cite_prefix, cite_key) )
                except LatexWalkerError:
                    # A citation that failed to retrieve under
                    # `on_citation_error: warn` has already been reported; it
                    # simply has nothing to export.
                    logger.debug("Not exporting unresolved citation ‘%s’", the_id)
                    continue
                data = _patch_json_entry(data)
                data['id'] = the_id
                data['key'] = the_id
                if 'type' not in data:
                    data['type'] = None
                fullciteprocjsond.append(data)

            return fullciteprocjsond


        def get_export_bibtex_content(self, csljson_data=None):
            r"""Render the document's citations as BibTeX.

            `csljson_data` lets a caller that already built the export set
            hand it over; each entry costs a call across the extension
            boundary plus a full CSL-JSON rebuild, so building it twice when
            both export files are configured is worth avoiding.  Note this
            mutates the entries it is given (`_patch_in_place_for_bibtex_export`
            below), so pass a set that has already been serialized.
            """

            fullciteprocjsond = (
                self.get_export_csljson_data() if csljson_data is None
                else csljson_data
            )

            with warnings.catch_warnings():
                if hasattr(citeproc.source, 'MissingArgumentWarning'):
                    # my patched version
                    warnings.simplefilter('ignore', citeproc.source.MissingArgumentWarning)
                    warnings.simplefilter('ignore', citeproc.source.UnsupportedArgumentWarning)
                else:
                    # until citeproc-py merges my PR
                    warnings.simplefilter('ignore', UserWarning)

                csl_style = citeproc.CitationStylesStyle(
                    os.path.join( os.path.dirname(__file__), 'bibtex--patched.csl' ),
                    validate=False
                )

                # patch entries for bibtex export!
                for entrydata in fullciteprocjsond:
                    _patch_in_place_for_bibtex_export(entrydata)

                bib_source = citeproc.source.json.CiteProcJSON(fullciteprocjsond)
                bibliography = citeproc.CitationStylesBibliography(
                    csl_style,
                    bib_source,
                    _cslformatter
                )

                for entrydata in fullciteprocjsond:
                    the_id = entrydata['id']
                    citation = citeproc.Citation([citeproc.CitationItem(the_id)])
                    bibliography.register(citation)

                # generate bibliography:
                bibliography_items = [str(item) for item in bibliography.bibliography()]

            return "\n\n".join(bibliography_items)


        def flm_main_scan_fragment(self, fragment, document_parts_fragments=None, **kwargs):
            r"""Collect every citation in the document and retrieve them all.

            One ``retrieve()`` call covers the lot: the library runs its own
            worklist, so chain targets (an ``arxiv:`` entry's DOI) are
            discovered and fetched in later passes without this method knowing
            anything about chaining.
            """

            scanner = CitationsScanner()

            fragment.start_node_visitor(scanner)
            if document_parts_fragments:
                for frag in document_parts_fragments:
                    frag.start_node_visitor(scanner)

            cites = []
            # Where each citation was written, for error messages.  Keyed by the
            # citation as *requested*, which is what a failure's `origin` names,
            # and it doubles as the dedup set for `cites` — first mention wins
            # for both the order and the reported location.
            where_required = {}

            for c in scanner.get_encountered_citations():
                logger.debug("Found citation %r", c)

                cite_prefix, cite_key = c['cite_prefix'], c['cite_key']

                if cite_prefix not in self.cite_prefixes:
                    raise LatexWalkerError(
                        f"Invalid citation prefix ‘{cite_prefix}’ in "
                        f"{c['encountered_in']['what']}"
                    )

                if (cite_prefix, cite_key) not in where_required:
                    where_required[(cite_prefix, cite_key)] = c['encountered_in']['what']
                    cites.append( (cite_prefix, cite_key) )

            self.encountered_cites = cites

            if not cites:
                return

            failures = self.citation_manager.retrieve(cites)

            if self.feature.prune_cache:
                # After retrieving, so this run's entries are fresh and only
                # genuinely dead ones go: `prune` drops what is expired *past*
                # the grace window, i.e. what stale-while-revalidate would no
                # longer serve anyway.
                dropped = self.citation_manager.prune()
                if dropped:
                    logger.info("Pruned %d expired citation(s) from the cache", dropped)

            for failure in failures:
                # `origin` names the citation the *user* wrote: when an
                # `arxiv:` entry chains to a DOI and that fetch fails, the
                # failure is about a DOI nobody typed.
                requested = failure.origin or (failure.prefix, failure.key)
                where = where_required.get(requested, '<unknown location>')

                msg = f"Failed to retrieve citation ‘{failure.prefix}:{failure.key}’"
                if failure.origin is not None:
                    msg += f" (needed by ‘{requested[0]}:{requested[1]}’)"
                msg += f", requested from {where}: {failure.message}"

                if self.feature.on_citation_error == 'fatal':
                    raise LatexWalkerError(msg)
                logger.warning(msg)
                self.failed_cites.add(requested)
                self.failed_cites.add( (failure.prefix, failure.key) )


    def __init__(self,
                 sources=None,
                 bib_csl_style=None,
                 cache_dir=None,
                 cache_base=None,
                 cache_file=None,
                 cache_entry_duration_dt=None,
                 on_citation_error='fatal',
                 user_agent=None,
                 manual_format=MANUAL_FORMAT,
                 drop_csl_fields=None,
                 ttl_policy=None,
                 retry=None,
                 max_chain_depth=None,
                 prune_cache=False,
                 on_event=None,
                 write_csljson_file=None,
                 write_bibtex_file=None,
                 **kwargs):

        super().__init__(external_citations_providers=None, **kwargs)

        # `None` is passed straight through: `_config.build_source_specs`
        # applies the default, so it lives in exactly one place.
        self.sources = sources

        self.bib_csl_style = bib_csl_style

        self.cache_dir = cache_dir
        self.cache_base = cache_base
        # Deprecated; translated in `_config.normalize_cache_location`.
        self.cache_file = cache_file
        # Deprecated; the library uses per-source lifetimes.
        self.cache_entry_duration_dt = cache_entry_duration_dt

        if on_citation_error not in ('fatal', 'warn'):
            raise ValueError(
                f"flm_citations: `on_citation_error` must be 'fatal' or 'warn', "
                f"not {on_citation_error!r}"
            )
        self.on_citation_error = on_citation_error

        self.user_agent = user_agent if user_agent is not None else DEFAULT_USER_AGENT
        self.manual_format = manual_format
        self.drop_csl_fields = (
            list(DEFAULT_DROPPED_CSL_FIELDS) if drop_csl_fields is None
            else list(drop_csl_fields)
        )
        self.ttl_policy = ttl_policy
        self.retry = retry
        self.max_chain_depth = max_chain_depth
        self.prune_cache = prune_cache
        self.on_event = on_event

        self.write_csljson_file = write_csljson_file
        self.write_bibtex_file = write_bibtex_file




def get_ready_formatted_text(citeprocjsond, format_name=FLM_FORMAT_NAME):
    r"""Pre-formatted citation text carried by an entry, if it has any.

    Returns ``(text, is_flm)``, or ``(None, False)``.  ``is_flm`` says whether
    the text is markup this renderer speaks and can go to the FLM parser as-is.
    `format_name` is the renderer's own markup name, not the ``manual_format``
    option — that option chooses the *tag* a manual citation is stored under,
    so setting it to anything else is a statement that those keys are not FLM.

    Two spellings are accepted, and both are current:

    * ``{"_ready_formatted": {"flm": "…"}}`` — what the library's ``manual``
      source emits.  It is a *map* keyed by markup format, because the library
      cannot know what its host renders;
    * ``{"_formatted_flm_text": "…"}`` — the flat key this package used before
      the migration.  Bibliography files in the wild are full of it, and they
      are user data we do not get to rewrite.

    Text under some *other* format name is deliberately not treated as FLM:
    passing HTML or LaTeX to the FLM parser would either fail or, worse,
    silently render as something else.
    """
    ready = citeprocjsond.get('_ready_formatted', None)
    if isinstance(ready, dict) and ready:
        if format_name in ready:
            return ready[format_name], True
        # Some other markup: show it, but not as FLM.
        other_format = sorted(ready.keys())[0]
        return ready[other_format], False

    legacy = citeprocjsond.get('_formatted_flm_text', None)
    if legacy is not None:
        return legacy, True

    return None, False


def _patch_in_place_for_bibtex_export(entrydata):
    if 'issued' not in entrydata and 'published' in entrydata:
        entrydata['issued'] = entrydata['published']
    if 'ISSN' in entrydata and isinstance(entrydata['ISSN'], list):
        entrydata['ISSN'] = " ".join(entrydata['ISSN'])


def _patch_json_entry(citeprocjsond):

    if 'author' in citeprocjsond:
        citeprocjsond = copy.copy(citeprocjsond)
        for author in citeprocjsond['author']:
            if 'name' in author and 'family' not in author and 'given' not in author:
                author['family'] = author['name']
                del author['name']

    return citeprocjsond


def _generate_citation_flm_from_citeprocjsond(
        citeprocjsond, bib_csl_style, what, flm_environment, *,
        add_arxiv_link, add_doi_link, add_url_link,
):

    ready_text, is_flm = get_ready_formatted_text(citeprocjsond)
    if ready_text is not None:
        # work is already done for us -- go!
        if not is_flm:
            ready_text = (r'\begin{verbatimtext}' + str(ready_text)
                          + r'\end{verbatimtext}')
        return flm_environment.make_fragment(
            ready_text,
            what=what,
            standalone_mode=True,
        )

    with warnings.catch_warnings():
        if hasattr(citeproc.source, 'MissingArgumentWarning'):
            # my patched version
            warnings.simplefilter('ignore', citeproc.source.MissingArgumentWarning)
            warnings.simplefilter('ignore', citeproc.source.UnsupportedArgumentWarning)
        else:
            # until citeproc-py merges my PR
            warnings.simplefilter('ignore', UserWarning)

        citekey = citeprocjsond['id']

        logger.debug("Creating citation for entry ‘%s’", citekey)

        # patch JSON for limitations of citeproc-py (?)
        #
        # E.g. for authors with 'name': ... instead of 'given': and 'family':

        citeprocjsond = _patch_json_entry(citeprocjsond)

        # explore the citeprocjsond tree and make sure that all strings are
        # valid FLM markup
        def _sanitize(d):
            if isinstance(d, dict):
                for k in d.keys():
                    d[k] = _sanitize(d[k])
                return d
            elif isinstance(d, list):
                for j, val in enumerate(d):
                    d[j] = _sanitize(val)
                return d
            else:
                try:
                    # try compiling the given value, suppressing warnings
                    flm_environment.make_fragment(
                        str(d),
                        standalone_mode=True,
                        silent=True
                    )
                except Exception as e:
                    logger.debug(
                        "Encountered invalid FLM string %r when "
                        "composing citation: %s", d, e
                    )
                    return r'\begin{verbatimtext}' + str(d) + r'\end{verbatimtext}'
                return d

        #
        # Sanitizing the entire JSON object (which often includes the abstract,
        # etc.) is completely overkill.  So we first try to generate the entry
        # without sanitizing, and if it fails, we sanitize.
        #
        #_sanitize(citeprocjsond)

        def _gen_entry(citeprocjsond):
            bib_source = citeproc.source.json.CiteProcJSON([citeprocjsond])
            bibliography = citeproc.CitationStylesBibliography(bib_csl_style, bib_source,
                                                               _cslformatter)

            citation1 = citeproc.Citation([citeproc.CitationItem(citeprocjsond['id'])])
            bibliography.register(citation1)
            bibliography_items = [str(item) for item in bibliography.bibliography()]
            assert len(bibliography_items) == 1
            result = bibliography_items[0]

            arxivid = citeprocjsond.get('arxivid', None)
            doi = citeprocjsond.get('doi', None) or citeprocjsond.get('DOI', None)
            url = citeprocjsond.get('URL', None)
            if doi and add_doi_link:
                doiurl = 'https://doi.org/'+urlquote(doi)
                result += r' \href{'+doiurl+r'}{DOI}'
            if arxivid and add_arxiv_link:
                result += r' \href{https://arxiv.org/abs/'+arxivid+'}{'+arxivid+'}'
            if url and (add_url_link is True or
                        (add_url_link == 'only-if-no-other-link'
                         and not arxivid and not doi)):
                result += r' \href{'+url+r'}{URL}'

            return result

        try:
            logger.debug("Attempting to generate entry for %s...", citekey)
            return flm_environment.make_fragment(
                _gen_entry(citeprocjsond),
                what=what,
                standalone_mode=True,
                silent=True # don't report errors on logger
            )
        except Exception:
            logger.debug("Error while forming citation entry for %s, trying "
                         "again with FLM sanitization on", citekey)

        _sanitize(citeprocjsond)
        try:
            return flm_environment.make_fragment(
                _gen_entry(citeprocjsond),
                standalone_mode=True,
                what=what
            )
        except Exception as e:
            logger.critical(f"EXCEPTION!! {e!r}")
            raise


# ------------------------------------------------

FeatureClass = FeatureCiteAuto
