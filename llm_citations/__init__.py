
llm_default_import_config = {
    'llm': {
        'features': [
            {
                '$defaults': True
            },
            {
                'name': 'llm_citations.feature_cite.FeatureCiteAuto',
                'config': {
                    'sources': [
                        {
                            '$defaults': True,
                        },
                    ],
                },
            },
        ],
    },
}
