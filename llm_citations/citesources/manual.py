
from .base import CitationSourceBase


class CitationSourceManual(CitationSourceBase):

    def __init__(self, **kwargs):

        override_options = {
            'chains_to_sources': [],
            'source_name': 'Manual citation info source',
            'chunk_size': float('inf'),
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

        citations = {}

        for key in chunk_keys:
            citations[key] = {
                '_formatted_llm_text': key, # that was hard
            }

        return citations



# for when using shorthane naming
CitationSourceClass = CitationSourceManual
