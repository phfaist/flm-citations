import re

from urllib.parse import quote as urlquote

import backoff
import requests

from .base import CitationSourceBase


class CitationSourceDoi(CitationSourceBase):

    def __init__(self, **kwargs):

        req_session = requests.Session()
        headers={
            'Accept': 'application/vnd.citationstyles.csl+json'
        }
        req_session.headers.update(headers)

        override_options = {
            'chains_to_sources': [],
            'source_name': 'DOI citation info source',
            'chunk_size': 1, # one DOI at a time
        }
        default_options = {
            'chunk_query_delay_ms': 1100,
            'cite_prefix': 'doi',
            'use_requests': True,
            'requests_session': req_session,
        }

        super().__init__(
            override_options,
            kwargs,
            default_options,
        )


    def retrieve_chunk(self, chunk_keys):

        if ( len(chunk_keys) != 1 ):
            raise RuntimeError(
                f"doi retrieve_chunk(): can only query one DOI at a time, "
                f"requested {chunk_keys!r}"
            )

        doi = chunk_keys[0].strip()

        self.citation_manager.store_citation(
            self.cite_prefix, doi, _get_doi_citeproc_json_object(doi, self)
        )





# utility to get the citeproc-json for a given DOI, with backoff
# (cf. https://www.crossref.org/documentation/retrieve-metadata/rest-api/tips-for-using-the-crossref-rest-api/)

def _backoff_handler(details):
    logger.warning("Backing off {wait:0.1f} seconds after {tries} tries "
                   "calling function {target} with args {args} and kwargs "
                   "{kwargs}".format(**details), exc_info=True)

def _backoff_fatal_code(e):
    if not e.response:
        return True
    return e.response.status_code == 404

@backoff.on_exception(backoff.expo,
                      requests.exceptions.RequestException,
                      max_tries=12,
                      on_backoff=_backoff_handler,
                      giveup=_backoff_fatal_code)
def _get_doi_citeproc_json_object(doi, source):

    doi_escaped = urlquote(doi)

    url = f"https://doi.org/{doi_escaped}"

    return source.fetch_url(
        url,
        json=True,
        # Accept: header already set in source's request_session
    )


# for when using shorthane naming
CitationSourceClass = CitationSourceDoi


