import re

import logging
logger = logging.getLogger(__name__)

import arxiv
import yaml

from .base import CitationSourceBase


_rx_arxiv_id_from_entryid = re.compile(
    # note: <arxivid> does NOT include the version number
    r'https?://arxiv.org/abs/(?P<arxivid>.*?)(?P<versionnumstr>v(?P<versionnum>\d+))?$',
    flags=re.IGNORECASE
)

_rx_ver = re.compile(r'v\d+$')

class CitationSourceArxiv(CitationSourceBase):

    def __init__(self, **kwargs):

        self.chain_to_doi = kwargs.get('chain_to_doi', True)

        override_options = {
            'chains_to_sources': ['doi'] if self.chain_to_doi else [],
            'source_name': 'ArXiv API citation info source',
        }
        default_options = {
            'chunk_query_delay_ms': 3100,
            'cite_prefix': 'arxiv',
            'use_requests': True,
        }

        super().__init__(
            override_options,
            kwargs,
            default_options,
        )

        self.override_arxiv_dois_file = self.options.get('override_arxiv_dois_file', None)
        self.override_arxiv_dois = self.options.get('override_arxiv_dois', {})

        # silence some arxiv.arxiv messages
        if not self.options.get('keep_arxiv_arxiv_logging_info_output', False):
            logging.getLogger('arxiv.arxiv').setLevel(level=logging.WARNING)


    def source_initialize_run(self):

        self._arxiv_to_doi_override = {}
        if self.override_arxiv_dois_file:
            content = self.fetch_url(self.override_arxiv_dois_file)
            data = yaml.safe_load(content)
            self._arxiv_to_doi_override.update(data)

        self._arxiv_to_doi_override.update(self.override_arxiv_dois)

        self.data_for_versionless_arxivid = {
            arxivid: []
            for arxivid in self.cite_key_list
            if not _rx_ver.search(arxivid)
        }
    
    def retrieve_chunk(self, arxivid_list):

        if not arxivid_list:
            return {}

        #
        # fetch meta-info from the arxiv for all encountered arXiv IDs, and
        # build the associated citation text endnotes.
        #
        big_slow_client = arxiv.Client(
            page_size=len(arxivid_list) + 100,
            delay_seconds=4,
            num_retries=5
        )
        searchobj = arxiv.Search(
            id_list=arxivid_list,
        )
        for result in big_slow_client.results(searchobj):
            #
            # build citation from the arxiv meta-information
            #

            m = _rx_arxiv_id_from_entryid.match(result.entry_id)
            if m is None:
                logger.warning(f"Unable to parse arXiv ID from {result.entry_id!r}")
                continue

            arxivid = m.group('arxivid').lower()
            versionnum = m.group('versionnum')
            arxivver = int(versionnum) if versionnum else None

            logger.debug(
                f"Processing received information for ‘{arxivid}’ (got v{arxivver})"
            )

            doi = None
            if result.doi is not None and result.doi:
                doi = str(result.doi)

            # override the given DOI in exceptional cases where it might be
            # missing, incomplete, or incorrect:
            if arxivid in self._arxiv_to_doi_override:
                override_doi = self._arxiv_to_doi_override[arxivid]
                logger.debug(f"Overriding reported DOI (‘{doi}’) for arXiv "
                             f"item ‘{arxivid}’ with manually specified DOI "
                             f"‘{override_doi}’ given in citation hints")
                doi = override_doi

            # construct CSL-JSON entry from the retrieved metadata
            citeprocjsond = {
                'type': 'article-journal',
                'title': result.title,
                'author': [
                    {'name': a.name}
                    for a in result.authors
                ],
                'published': {
                    'date-parts': [[
                        result.published.year,
                        result.published.month,
                        result.published.day,
                    ]],
                },
                'doi': doi,
                'arxivid': arxivid,
                'arxiv_version_number': arxivver,
            }
            
            if arxivid in self.cite_key_list:
                self.citation_manager.store_citation(self.cite_prefix, arxivid, citeprocjsond)

            if arxivid in self.data_for_versionless_arxivid:
                self.data_for_versionless_arxivid[arxivid].append( citeprocjsond )

        return

    def source_finalize_run(self):

        # all the queries for ids ran, now figure out which versions to link to
        # when a non-versioned key is provided

        for arxivid, versionslist in self.data_for_versionless_arxivid.items():

            if not versionslist:
                logger.error(
                    f"No arXiv data received for arXiv id ‘{arxivid}’, what happened?!?"
                )
                raise ValueError(f"No arXiv data found for ‘{arxivid}’")

            best = None
            for current in versionslist:
                if best is None and current is not None:
                    best = current
                    continue
                if current is None:
                    continue
                # "arxiv_version_number === null" only happens if arXiv API
                # responded with an article whose ID didn't have a version
                # number -> use that one directly as it is the answer to our
                # query!
                if best['arxiv_version_number'] is None:
                    continue
                if current['arxiv_version_number'] is None:
                    best = current
                    continue
                if current['arxiv_version_number'] >= best['arxiv_version_number']:
                    best = current
                    continue

            self._store_citation(arxivid, best)

    def _store_citation(self, arxivid, csljsondata):
        doi = csljsondata.get('doi', None)
        if doi and self.chain_to_doi:
            # chain to DOI
            self.citation_manager.store_citation_chained(
                self.cite_prefix, arxivid,
                'doi', doi,
                {
                    'arxivid': arxivid,
                },
            )
        else:
            self.citation_manager.store_citation(self.cite_prefix, arxivid, csljsondata)




# for when using shorthane naming
CitationSourceClass = CitationSourceArxiv
