

### FIXME: Merge feature config of imported configs don't work yet ... :/
### Need to fix this in llm.main.run().  Probably all $import's should be
### processed first, before merging anything.

llm_default_import_config = {
    'llm': {
        'features': {
            'llm_citations': {},
            # {
            #     'sources': [
            #         {
            #             '$defaults': True,
            #         },
            #     ],
            # },
        },
    },
}


def FeatureClass(*args, **kwargs):
    from .feature import FeatureClass as _FeatureClass
    return _FeatureClass(*args, **kwargs)
