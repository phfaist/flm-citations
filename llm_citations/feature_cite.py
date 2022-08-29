import json
import importlib

import logging
logger = logging.getLogger(__name__)

from llm.feature.cite import (
    FeatureExternalPrefixedCitations,
    CitationEndnoteCategory
)

import citeproc
import citeproc.source
import citeproc.source.json
from . import _cslformatter

from .llmcitationsscanner import CitationsScanner


def importclass(fullname):
    modname, classname = fullname.rsplit('.', maxsplit=1)
    mod = importlib.import_module(modname)
    return getattr(mod, classname)


_default_citation_sources_spec = {
    'arxiv': {
        'name': 'arxiv',
        'config': {},
    },
    'doi': {
        'name': 'doi',
        'config': {},
    },
    'manual': {
        'name': 'manual',
        'config': {},
    }
}

class FeatureCiteAuto(FeatureExternalPrefixedCitations):
    r"""
    .....

    Arguments:

    - `citation_sources` - a dictionary where keys are valid `cite_prefix`'s and
      where each value is an instance of `CitationSource` (see `citesources` module).

    - ... further arguments are passed on to
      `llm.feature.cite.FeatureExternalPrefixedCitations`.
    """

    class RenderManager(FeatureExternalPrefixedCitations.RenderManager):
        
        def get_citation_content_llm(self, cite_prefix, cite_key, resource_info):

            csljson = self.feature.get_citation_csljson(cite_prefix, cite_key)

            result = _generate_citation_llm_from_citeprocjsond(
                csljson,
                self.feature.bib_csl_style,
                str(resource_info),
                self.render_context.doc.environment
            )

            return result


    def __init__(self,
                 sources=None,
                 bib_csl_style=None,
                 cache_file='.llm-citations.cache.json',
                 **kwargs):

        super().__init__(external_citations_provider=None, **kwargs)

        if sources is None:
            sources = _default_citation_sources_spec

        self.citation_sources = {}

        for cite_prefix, citation_source_spec in sources.items():

            cname = citation_source_spec['name']
            cconfig = citation_source_spec.get('config', {})

            if '.' not in cname:
                cname = f"llm_citations.citesources.{cname}.CitationSourceClass"

            TheClass = importclass(cname)
            self.citation_sources[cite_prefix] = TheClass(**cconfig)

        if bib_csl_style is None:
            bib_csl_style = "harvard1"

        self.bib_csl_style = citeproc.CitationStylesStyle(bib_csl_style, validate=False)

        self.cache_file = cache_file

        self.citations_db = {
            cite_prefix: {}
            for cite_prefix in self.citation_sources.keys()
        }

    def get_citation_csljson(self, cite_prefix, cite_key):
        orig_cite_prefix, orig_cite_key = cite_prefix, cite_key
        set_properties_chain = {}
        while True:
            csljson = citations_db[cite_prefix][cite_key]
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


    def llm_main_scan_fragment(self, fragment):

        scanner = CitationsScanner()

        fragment.start_node_visitor(scanner)

        retrieve_citation_keys_by_prefix = {
            cite_prefix: []
            for cite_prefix in self.citation_sources.keys()
        }

        for c in scanner.get_encountered_citations():
            logger.debug(f"Found citation {c=!r}")

            if c['cite_prefix'] not in retrieve_citation_keys_by_prefix:
                raise ValueError(
                    f"Invalid citation prefix ‘{c['cite_prefix']}’ in "
                    f"{c['encountered_in']['what']}"
                )

            retrieve_citation_keys_by_prefix[c['cite_prefix']].append(c['cite_key'])

        while any(retrieve_citation_keys_by_prefix.values()):

            for cite_prefix, cite_key_list in retrieve_citation_keys_by_prefix.items():

                logger.debug(f"Keys to retrieve: {cite_prefix} -> {cite_key_list}")

                new_citations = \
                    self.citation_sources[cite_prefix].retrieve_citations(cite_key_list)

                self.citations_db[cite_prefix].update(new_citations)


            # check if there are chained citations that we need to retrieve as well
            for cite_prefix, db in self.citations_db.items():
                for cite_key, csljson in db.items():
                    if 'chained' in csljson:
                        chained = csljson['chained']
                        chained_cite_prefix, chained_cite_key = \
                            chained['cite_prefix'], chained['cite_key']
                        if chained_cite_key not in self.citations_db[chained_cite_prefix]:
                            # add this one for retrieval
                            retrieve_citation_keys_by_prefix[chained_cite_prefix].append(
                                chained_cite_key
                            )



def _generate_citation_llm_from_citeprocjsond(citeprocjsond, bib_csl_style, what, llmenviron):

    if '_formatted_llm_text' in citeprocjsond:
        # work is already done for us -- go!
        return llmenviron.make_fragment(
            citeprocjsond['_formatted_llm_text'],
            what=what,
            standalone_mode=True,
        )

    with warnings.catch_warnings():
        warnings.simplefilter('ignore', citeproc.source.MissingArgumentWarning)
        warnings.simplefilter('ignore', citeproc.source.UnsupportedArgumentWarning)

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
        # valid LLM markup
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
                    llmenviron.make_fragment(
                        str(d),
                        standalone_mode=True,
                        silent=True
                    )
                except Exception as e:
                    logger.debug(
                        f"Encountered invalid LLM string {d!r} when "
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
            return bibliography_items[0]

        try:
            logger.debug(f"Attempting to generate entry for {citekey}...")
            return llmenviron.make_fragment(
                _gen_entry(citeprocjsond),
                what=what,
                standalone_mode=True,
                silent=True # don't report errors on logger
            )
        except Exception:
            logger.debug(f"Error while forming citation entry for {citekey}, trying "
                         f"again with LLM sanitization on")

        _sanitize(citeprocjsond)
        try:
            return llmenviron.make_fragment(
                _gen_entry(citeprocjsond),
                standalone_mode=True,
                what=what
            )
        except Exception as e:
            logger.critical(f"EXCEPTION!! {e!r}")
            raise
