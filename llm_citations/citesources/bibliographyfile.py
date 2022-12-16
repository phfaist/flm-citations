import re
import os.path
import sys
import json
import yaml

import logging
logger = logging.getLogger(__name__)


from .base import CitationSourceBase


_vars = {
    'jobname': lambda doc: doc.metadata['jobname'],
}

_rx_vars = re.compile(r'\$\{(' + "|".join(re.escape(v) for v in _vars) + ')\}')

def _replace_vars(x, doc):
    return _rx_vars.sub(
        lambda m: _vars[m.group(1)] (doc),
        x
    )


class CitationSourceBibliographyFile(CitationSourceBase):

    def __init__(self, bibliography_file=None, **kwargs):

        override_options = {
            'chains_to_sources': [],
            'source_name': 'Bibliography file(s) citation info source',
            'chunk_size': sys.maxsize,
            'chunk_query_delay_ms': 0,
        }
        default_options = {
            'cite_prefix': 'bib',
            'use-requests': True, # to retrieve possibly remote bib files
        }

        super().__init__(
            override_options,
            kwargs,
            default_options,
        )

        doc = kwargs['doc']

        if bibliography_file is None:
            bibliography_files = []

            # specified manually in metadata
            if 'bibliography' in doc.metadata:
                meta_bibliography_files = doc.metadata['bibliography']
                if isinstance(meta_bibliography_files, str):
                    meta_bibliography_files = [ meta_bibliography_files ]
                bibliography_files = bibliography_files + meta_bibliography_files

            # derived from jobname
            jobnamebibfile = doc.metadata['jobname'] + '.bib.json'
            if os.path.exists(jobnamebibfile):
                bibliography_files = bibliography_files + [ jobnamebibfile ]

        elif isinstance(bibliography_file, str):

            bibliography_files = [ bibliography_file ]

        else:

            bibliography_files = bibliography_file


        bibliography_files = [ _replace_vars(b, doc)
                               for b in bibliography_files ]

        self.bibliography_files = bibliography_files

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
