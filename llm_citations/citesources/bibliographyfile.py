import sys
import json
import yaml

import logging
logger = logging.getLogger(__name__)


from .base import CitationSourceBase


class CitationSourceBibliographyFile(CitationSourceBase):

    def __init__(self, bibliography_file, **kwargs):

        override_options = {
            'chains_to_sources': [],
            'source_name': 'Bibliography file(s) citation info source',
            'chunk_size': sys.maxsize,
            'chunk_query_delay_ms': 0,
        }
        default_options = {
            'cite_prefix': 'bibfile',
            'use-requests': True, # to retrieve possibly remote bib files
        }

        super().__init__(
            override_options,
            kwargs,
            default_options,
        )

        if isinstance(bibliography_file, str):
            self.bibliography_files = [ bibliography_file ]
        else:
            self.bibliography_files = bibliography_file

        logger.debug(f"bib file citation source ({self.cite_prefix=}), "
                     f"{self.bibliography_files=}")
            
        self.bibliography_data = {}

    def source_initialize_run(self):
        
        for bibfile in self.bibliography_files:
            logger.debug(f"Loading bibliography ‘{bibfile}’ ...")
            bibdata = self.fetch_url(bibfile)
            if bibfile.endswith('.json'):
                bibdatajson = json.loads(bibdata)
            elif bibfile.endswith( ('.yml', '.yaml') ):
                bibdatajson = yaml.safe_load(bibdata)
            else:
                raise ValueError(f"Unknown bibliography format: ‘{bibfile}’ (expected "
                                 f"CSL-JSON or CSL-YAML)")
                
            if isinstance(bibdatajson, list):
                bibdatajson = {
                    obj['id']: obj
                    for obj in bibdatajson
                }
            self.bibliography_data.update(bibdatajson)

    def retrieve_chunk(self, chunk_keys):

        for key in chunk_keys:
            if key not in self.bibliography_data:
                raise ValueError(
                    f"Bibliography key {key} was not found in bibliography files "
                    + ', '.join([f'‘{b}’' for b in self.bibliography_files])
                )

            self.citation_manager.store_citation(
                self.cite_prefix, key, self.bibliography_data[key]
            )

        return



# for when using shorthane naming
CitationSourceClass = CitationSourceBibliographyFile
