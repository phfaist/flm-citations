import time
from urllib.parse import urlparse

import logging
logger = logging.getLogger(__name__)

import requests


class CitationSourceBase:

    def __init__(self, override_options, kwargs, default_options):
        super().__init__()

        self.options = dict(default_options)
        self.options.update(kwargs)
        self.options.update(override_options)

        self.chunk_size = self.options.get('chunk_size', 512)
        self.chunk_query_delay_ms = self.options.get('chunk_query_delay_ms', 1000)

        self.cite_prefix = self.options['cite_prefix']

        self.chains_to_sources = self.options.get('chains_to_sources', [])
        self.source_name = self.options.get('source_name', '<unknown source>')

        self.requests_session = None
        if self.options.get('use_requests', False):
            if 'requests_session' in self.options:
                self.requests_session = self.options['requests_session']
            else:
                self.requests_session = requests.Session()

        self.cite_key_list = None

        self.citation_manager = None

    def set_citation_manager(self, citation_manager):
        self.citation_manager = citation_manager


    def retrieve_citations(self, cite_key_list):

        self.cite_key_list = cite_key_list

        self.source_initialize_run()

        total_retrieved = 0
        remaining_keys = list(cite_key_list)

        last_chunk_query_monotonic_s = None

        if len(remaining_keys):
            logger.info(f"{self.source_name}: there are "
                        f"{len(remaining_keys)} citation(s) to query")
        else:
            logger.debug(f"{self.source_name}: there are no citations to query")

        while len(remaining_keys) > 0:

            # if applicable, wait before another chunk query call
            if (self.chunk_query_delay_ms and last_chunk_query_monotonic_s is not None):
                dt_s = (time.monotonic() - last_chunk_query_monotonic_s)
                dt_s_to_wait = (self.chunk_query_delay_ms/1000) - dt_s
                if dt_s_to_wait > 0:
                    time.sleep(dt_s_to_wait)

            # create a chunk of IDs to query
            chunk_ids, remaining_keys = \
                remaining_keys[:self.chunk_size], remaining_keys[self.chunk_size:]

            last_chunk_query_monotonic_s = time.monotonic()
            self.retrieve_chunk(chunk_ids)
            
            # bookkeeping & logging
            total_retrieved += len(chunk_ids)
            logger.info(f"{self.source_name}: {total_retrieved}/{len(cite_key_list)}")

        self.source_finalize_run()


    # helper for fetching URLs
    def fetch_url(self, url, binary=False, json=False, **kwargs):

        if url is None:
            raise ValueError(f"fetch_url(): url is None!")

        logger.debug(f"Fetching URL ‘{url}’ ...")

        p = urlparse(url)

        if not p.scheme or p.scheme == 'file':
            # Read a local file.
            if binary:
                open_args, open_kwargs = ('rb',), {}
            else:
                open_args, open_kwargs = ('r',), { 'encoding': 'utf-8' }
            with open(p.path, *open_args, **open_kwargs) as f:
                return f.read()

        req_kwargs = {}

        method = kwargs.pop('method', 'get')
        reqfn = getattr(self.requests_session, method)

        if method == 'post':
            req_kwargs['data'] = kwargs.pop('body', None)

        # e.g., headers etc.
        req_kwargs.update(kwargs)

        # fire!
        r = reqfn(url)

        if not r.ok:
            # raise errors
            r.raise_for_status()

        if binary:
            return r.content

        r.encoding = 'utf-8'

        if json:
            return r.json()

        return r.text



    # -------------

    # can be reimplemented

    def source_initialize_run(self):
        pass

    def source_finalize_run(self):
        pass


    # must be reimplemented

    def retrieve_chunk(self, chunk_keys):
        raise RuntimeError("The method retrieve_chunk() must be reimplemented by subclasses!")
