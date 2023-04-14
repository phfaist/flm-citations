import sys

from .base import CitationSourceBase


class CitationSourceManual(CitationSourceBase):

    def __init__(self, **kwargs):

        override_options = {
            'chains_to_sources': [],
            'source_name': 'Manual citation info source',
            'chunk_size': sys.maxsize,
            'chunk_query_delay_ms': 0,
        }
        default_options = {
            'cite_prefix': 'manual',
            'use_requests': False,
        }

        super().__init__(
            override_options,
            kwargs,
            default_options,
        )


    def retrieve_chunk(self, chunk_keys):

        for key in chunk_keys:
            self.citation_manager.store_citation(
                self.cite_prefix, key,
                {
                    '_formatted_flm_text': key, # well that was hard
                }
            )

        return



# for when using shorthane naming
CitationSourceClass = CitationSourceManual
