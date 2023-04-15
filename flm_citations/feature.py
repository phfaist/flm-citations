import json
import importlib
import warnings
import copy
import os.path
import datetime
from urllib.parse import quote as urlquote

import logging
logger = logging.getLogger(__name__)

from flm.feature.cite import (
    FeatureExternalPrefixedCitations,
)

import citeproc
import citeproc.source
import citeproc.source.json
from . import _cslformatter

from .flmcitationsscanner import CitationsScanner


def importclass(fullname):
    modname, classname = fullname.rsplit('.', maxsplit=1)
    mod = importlib.import_module(modname)
    return getattr(mod, classname)


_default_citation_sources_spec = [
    {
        'name': 'arxiv',
        'config': {},
    },
    {
        'name': 'doi',
        'config': {},
    },
    {
        'name': 'manual',
        'config': {},
    },
    {
        'name': 'bibliographyfile',
        'config': {},
    },
]

class FeatureCiteAuto(FeatureExternalPrefixedCitations):
    r"""
    .....

    Arguments:

    - `citation_sources` - a dictionary where keys are valid `cite_prefix`'s and
      where each value is an instance of `CitationSource` (see `citesources` module).

    - ... further arguments are passed on to
      `flm.feature.cite.FeatureExternalPrefixedCitations`.
    """

    add_arxiv_link = True
    add_doi_link = True
    add_url_link = 'only-if-no-other-link' # = only added if there's no arxiv or doi link

    # inherited!!
    #feature_name = 'citations'

    feature_default_config = {
        'sources': _default_citation_sources_spec,
    }

    class RenderManager(FeatureExternalPrefixedCitations.RenderManager):
        
        def get_citation_content_flm(self, cite_prefix, cite_key, resource_info):

            fdocmgr = self.feature_document_manager

            csljson = fdocmgr.get_citation_csljson(cite_prefix, cite_key)

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

    class DocumentManager(FeatureExternalPrefixedCitations.DocumentManager):

        def initialize(self):
            super().initialize()

            self.citation_sources = {}

            for citation_source_spec in self.feature.sources:

                cname = citation_source_spec['name']
                cconfig = citation_source_spec.get('config', {})

                if '.' not in cname:
                    cname = f"flm_citations.citesources.{cname}.CitationSourceClass"

                cconfig.update(doc=self.doc)

                TheClass = importclass(cname)
                thesource = TheClass(**cconfig)
                thesource.set_citation_manager(self)
                self.citation_sources[thesource.cite_prefix] = thesource

            bib_csl_style = self.feature.bib_csl_style
            if bib_csl_style is None:
                bib_csl_style = os.path.join(
                    os.path.dirname(__file__),
                    'american-physical-society-et-al--patched.csl'
                )
                #"harvard1"
            self.bib_csl_style = \
                citeproc.CitationStylesStyle(bib_csl_style, validate=False)

            self.citations_db = {
                cite_prefix: {}
                for cite_prefix in self.citation_sources.keys()
            }

            logger.debug(f"citation_sources are {self.citation_sources!r}")

            self.load_cache()

            self.new_chained_citations = None

        def load_cache(self):
            if os.path.exists(self.feature.cache_file):
                #logger.debug(f"Loading cache file ‘{self.feature.cache_file}’")
                try:
                    with open(self.feature.cache_file, 'r', encoding='utf-8') as f:
                        self.citations_db.update(json.load(f))
                    # check for expired entries
                    now = datetime.datetime.now()
                    for cite_prefix, cite_key_dict in self.citations_db.items():
                        cite_keys_list = list(cite_key_dict.keys())
                        for cite_key in cite_keys_list:
                            dexpires = datetime.datetime.fromisoformat(
                                self.citations_db[cite_prefix][cite_key]['expires']
                            )
                            if dexpires < now:
                                del self.citations_db[cite_prefix][cite_key]
                    logger.debug("Loaded cache ‘%s’: %s", self.feature.cache_file,
                                 ", ".join([
                                     f"{len(cprefixdb)} {cprefix}"
                                     for cprefix, cprefixdb in self.citations_db.items()
                                 ]))
                except Exception as e:
                   logger.warning(
                       f"Failure while loading cache file ‘{self.feature.cache_file}’: "
                       f"{e}, ignoring ..."
                   )
                   return

        def save_cache(self):
            with open(self.feature.cache_file, 'w', encoding='utf-8') as fw:
                json.dump(self.citations_db, fw)


        def get_citation_csljson(self, cite_prefix, cite_key):
            orig_cite_prefix, orig_cite_key = cite_prefix, cite_key
            set_properties_chain = {}
            while True:
                csljson = self.citations_db[cite_prefix][cite_key]['entry']
                if 'chained' not in csljson:
                    # make sure we have the correct ID set
                    origid = f"{orig_cite_prefix}:{orig_cite_key}"
                    if set_properties_chain:
                        csljson = dict(csljson, **set_properties_chain)
                    if csljson['id'] != origid:
                        return dict(csljson, id=origid)
                    return csljson

                # chained citation, follow chain
                cite_prefix = csljson['chained']['cite_prefix']
                cite_key = csljson['chained']['cite_key']
                set_properties_chain = dict(
                    csljson['chained']['set_properties'],
                    **set_properties_chain
                )

                # ... and repeat !

        def store_citation(self, cite_prefix, cite_key, csljson):

            if csljson.get('chained', None):
                new_cite_prefix = csljson['chained']['cite_prefix']
                new_cite_key = csljson['chained']['cite_key']

                if new_cite_prefix not in self.citation_sources:
                    raise ValueError(
                        f"No source registered for cite prefix ‘{new_cite_prefix}’ in "
                        f"chained citation retreival for ‘{cite_prefix}:{cite_key}’"
                    )

            cslentry = dict(csljson, id=f"{cite_prefix}:{cite_key}")

            self.citations_db[cite_prefix][cite_key] = {
                'entry': cslentry,
                'expires': (datetime.datetime.now()
                            + self.feature.cache_entry_duration_dt).isoformat()
            }

            # save cache at each store
            self.save_cache()

        def store_citation_chained(self, cite_prefix, cite_key,
                                   new_cite_prefix, new_cite_key, set_properties):

            self.store_citation(
                cite_prefix,
                cite_key,
                # special JSON entry to store in cache
                {
                    'chained': {
                        'cite_prefix': new_cite_prefix,
                        'cite_key': new_cite_key,
                        'set_properties': dict(set_properties),
                    }
                },
            )

            self.new_chained_citations.append( (cite_prefix, cite_key) )


        def flm_main_scan_fragment(self, fragment):

            scanner = CitationsScanner()

            fragment.start_node_visitor(scanner)

            retrieve_citation_keys_by_prefix = {
                cite_prefix: set()
                for cite_prefix in self.citation_sources.keys()
            }

            for c in scanner.get_encountered_citations():
                logger.debug(f"Found citation {c=!r}")

                if c['cite_prefix'] not in retrieve_citation_keys_by_prefix:
                    raise ValueError(
                        f"Invalid citation prefix ‘{c['cite_prefix']}’ in "
                        f"{c['encountered_in']['what']}"
                    )

                retrieve_citation_keys_by_prefix[c['cite_prefix']].add(c['cite_key'])

            while any(retrieve_citation_keys_by_prefix.values()):

                self.new_chained_citations = []

                for cite_prefix, cite_key_set in retrieve_citation_keys_by_prefix.items():

                    retrieve_cite_key_set = [
                        cite_key
                        for cite_key in cite_key_set
                        if cite_key not in self.citations_db[cite_prefix]
                    ]

                    logger.debug(f"Keys to retrieve: {cite_prefix} -> {retrieve_cite_key_set}")
                    self.citation_sources[cite_prefix].retrieve_citations(
                        retrieve_cite_key_set
                    )

                #logger.debug(f"At this point, {self.citations_db = }")

                retrieve_citation_keys_by_prefix = {
                    cite_prefix: set()
                    for cite_prefix in self.citation_sources.keys()
                }

                # check if there are chained citations that we need to retrieve as well
                for cite_prefix, cite_key in self.new_chained_citations:
                    csljson = self.citations_db[cite_prefix][cite_key]['entry']
                    chained = csljson['chained']
                    chained_cite_prefix, chained_cite_key = \
                        chained['cite_prefix'], chained['cite_key']
                    if chained_cite_key not in self.citations_db[chained_cite_prefix]:
                        # add this one for retrieval
                        retrieve_citation_keys_by_prefix[chained_cite_prefix].add(
                            chained_cite_key
                        )
                        #logger.debug(f"Adding ‘{chained_cite_prefix}:{chained_cite_key}’ "
                        #             f"for retrieval")



    def __init__(self,
                 sources=None,
                 bib_csl_style=None,
                 cache_file='.flm-citations.cache.json',
                 cache_entry_duration_dt=datetime.timedelta(days=30),
                 **kwargs):

        super().__init__(external_citations_providers=None, **kwargs)

        if sources is None:
            sources = _default_citation_sources_spec
        self.sources = sources

        self.bib_csl_style = bib_csl_style

        self.cache_file = cache_file
        self.cache_entry_duration_dt = cache_entry_duration_dt



def _generate_citation_flm_from_citeprocjsond(
        citeprocjsond, bib_csl_style, what, flm_environment, *,
        add_arxiv_link, add_doi_link, add_url_link,
):

    if '_formatted_flm_text' in citeprocjsond:
        # work is already done for us -- go!
        return flm_environment.make_fragment(
            citeprocjsond['_formatted_flm_text'],
            what=what,
            standalone_mode=True,
        )

    with warnings.catch_warnings():
        # warnings.simplefilter('ignore', citeproc.source.MissingArgumentWarning)
        # warnings.simplefilter('ignore', citeproc.source.UnsupportedArgumentWarning)
        warnings.simplefilter('ignore', UserWarning) # until citeproc-py merges my PR

        citekey = citeprocjsond['id']

        logger.debug(f"Creating citation for entry ‘{citekey}’")

        # patch JSON for limitations of citeproc-py (?)
        #
        # E.g. for authors with 'name': ... instead of 'given': and 'family':

        if 'author' in citeprocjsond:
            citeprocjsond = copy.copy(citeprocjsond)
            for author in citeprocjsond['author']:
                if 'name' in author and 'family' not in author and 'given' not in author:
                    author['family'] = author['name']
                    del author['name']

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
                        f"Encountered invalid FLM string {d!r} when "
                        f"composing citation: {e}"
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
                result += ' \href{'+doiurl+'}{DOI}'
            if arxivid and add_arxiv_link:
                result += ' \href{https://arxiv.org/abs/'+arxivid+'}{'+arxivid+'}'
            if url and (add_url_link is True or
                        (add_url_link == 'only-if-no-other-link'
                         and not arxivid and not doi)):
                result += ' \href{'+url+'}{URL}'

            return result

        try:
            logger.debug(f"Attempting to generate entry for {citekey}...")
            return flm_environment.make_fragment(
                _gen_entry(citeprocjsond),
                what=what,
                standalone_mode=True,
                silent=True # don't report errors on logger
            )
        except Exception:
            logger.debug(f"Error while forming citation entry for {citekey}, trying "
                         f"again with FLM sanitization on")

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
