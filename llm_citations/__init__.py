
llm_default_import_config = {
    'llm': {
        'features': [
            {
                '$preset': 'defaults',
            },
            {
                'name': 'llm_citations.feature_cite.FeatureCiteAuto',
                'config': {
                    'sources': [
                        {
                            '$preset': 'defaults',
                        },
                    ],
                },
            },
        ],
    },
}
