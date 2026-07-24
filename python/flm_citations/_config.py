r"""Translate the ``flm_citations`` feature configuration into source specs.

The YAML the user writes has not changed shape::

    flm:
      features:
        flm_citations:
          sources:
            - $defaults:
            - $merge-config:
                name: 'bibliographyfile'
                config:
                  cite_prefix: b
                  bibliography_file:
                    - refs.yaml

What it *means* has, because retrieval now happens in the ``autocitefetch``
Rust library rather than in this package.  Two kinds of option therefore need
handling here rather than being passed through:

* options whose meaning survives but whose name or shape changed
  (``cite_prefix`` is now the registration prefix, ``chain_to_doi: false`` is
  now ``chain_dois_to: None``) — translated, with a :class:`DeprecationWarning`
  when the old spelling was used;
* options that no longer have anything to configure (``chunk_size``,
  ``chunk_query_delay_ms``, and the ``requests``-session plumbing) — dropped
  with a warning, because silently ignoring them would present as "the option
  did nothing", which is the hardest kind of config bug to chase.

Everything this module emits is a plain dict; the extension module validates it
and rejects keys it does not know.
"""

import os.path
import re
import warnings

import logging
logger = logging.getLogger(__name__)


# Old `name:` values, and the source kind each maps to.  `bibliographyfile` is
# the historical spelling; `bib` matches the library and the citation prefix.
_SOURCE_KINDS = {
    'arxiv': 'arxiv',
    'doi': 'doi',
    'manual': 'manual',
    'bib': 'bib',
    'bibliographyfile': 'bib',
}

# A kind's own name is the prefix it is registered under when the config does
# not say otherwise (`bib` for the `bib` kind, whatever `name:` spelled it).

# The source list used when the feature is enabled without one.
DEFAULT_SOURCES = [
    {'name': 'arxiv', 'config': {}},
    {'name': 'doi', 'config': {}},
    {'name': 'manual', 'config': {}},
    {'name': 'bibliographyfile', 'config': {}},
]

# Per-source options that used to configure the Python fetchers and have no
# equivalent: chunk sizes and pacing are properties of each remote API and are
# fixed by the library (arXiv 100 keys / 3.1 s, doi.org 1 / 1.1 s), and there is
# no `requests` session to hand around any more.
_OBSOLETE_SOURCE_OPTIONS = {
    'chunk_size': 'chunk sizes are fixed per source by the retrieval library',
    'chunk_query_delay_ms': 'request pacing is fixed per source by the retrieval library',
    'use_requests': 'HTTP is handled by the retrieval library',
    'requests_session': 'HTTP is handled by the retrieval library',
    'chains_to_sources': 'use `chain_dois_to` on the arxiv source instead',
    'source_name': 'sources are identified by their citation prefix',
    'keep_arxiv_arxiv_logging_info_output': 'the `arxiv` Python package is no longer used',
}


_vars = {
    'jobname': lambda doc: doc.metadata['jobname'],
}

_rx_vars = re.compile(r'\$\{(' + "|".join(re.escape(v) for v in _vars) + r')\}')


def _replace_vars(x, doc):
    r"""Expand ``${jobname}`` in a bibliography file location."""
    return _rx_vars.sub(lambda m: _vars[m.group(1)](doc), x)


def _deprecated(what, message):
    warnings.warn(f"flm_citations: {what}: {message}", DeprecationWarning, stacklevel=3)
    logger.warning("%s: %s", what, message)


def _pop_alias(config, what, *names):
    r"""Pop the first of `names` that is present, removing all of them.

    Every alias is popped, not just the winner, so a config that spells one
    option two ways does not trip the unknown-option check with the loser's
    name — it gets the message below instead.
    """
    found = [(name, config.pop(name)) for name in names if name in config]
    if len(found) > 1:
        raise ValueError(
            f"flm_citations: {what}: give only one of "
            f"{', '.join('`' + n + '`' for n, _ in found)} — they are the same option"
        )
    return found[0][1] if found else None


def _is_url(location):
    r"""Whether a bibliography location is a URL rather than a local path.

    Only these two schemes: a Windows drive letter (``C:\refs.json``) would
    otherwise look like a scheme, and the library fetches everything else off
    the filesystem.
    """
    return location.startswith(('http://', 'https://', 'file:'))


def resolve_bibliography_location(location, cwd):
    r"""Make a bibliography location absolute, leaving URLs alone.

    The library fetches bibliography files through its one ``Fetcher`` choke
    point, which resolves a bare path against the *process* working directory —
    not the document's.  A relative ``bibliography: refs.yaml`` in front matter
    has always meant "next to the document", so resolve it here, where the
    document directory is known.
    """
    if _is_url(location):
        return location
    return os.path.abspath(os.path.join(cwd, os.path.expanduser(location)))


def document_cwd(doc):
    r"""The directory a document's relative paths are resolved against."""
    if doc is not None and doc.metadata and 'filepath' in doc.metadata:
        return doc.metadata['filepath']['dirname']
    return ''


def default_bibliography_files(doc, cwd):
    r"""The bibliography files implied by the document itself.

    Two sources, both pre-existing behaviour: the front matter's
    ``bibliography:`` key, and a ``<jobname>.bib.json`` sitting next to the
    document.
    """
    if doc is None or not doc.metadata:
        return []

    files = []

    meta = doc.metadata.get('bibliography', None)
    if isinstance(meta, str):
        meta = [meta]
    # A `bibliography:` key with nothing under it parses as None, which is a
    # perfectly ordinary thing to leave behind while editing front matter.
    if meta:
        files = files + list(meta)

    if 'jobname' in doc.metadata:
        jobnamebibfile = doc.metadata['jobname'] + '.bib.json'
        if os.path.exists(os.path.join(cwd, jobnamebibfile)):
            files = files + [jobnamebibfile]

    return files


def build_source_specs(sources, doc, cwd, manual_format='flm'):
    r"""Turn the feature's ``sources`` config into extension source specs.

    Returns a list of dicts, one per citation prefix, ready to hand to
    :class:`flm_citations._autocitefetch.CitationManager`.
    """
    if sources is None:
        sources = DEFAULT_SOURCES

    specs = []
    for entry in sources:
        specs.append(_build_one(entry, doc, cwd, manual_format))

    _fix_up_chain_targets(specs)
    return specs


def _build_one(entry, doc, cwd, manual_format):
    name = entry.get('name', None)
    if name is None:
        raise ValueError(f"flm_citations: citation source has no `name`: {entry!r}")

    config = dict(entry.get('config', None) or {})

    if name not in _SOURCE_KINDS:
        if '.' in name:
            # The old config could name any importable Python class.  There is
            # no Python source class to import any more, so this cannot be
            # honoured in any partial way — say so plainly rather than failing
            # later with an unknown-prefix citation error.
            raise ValueError(
                f"flm_citations: citation source ‘{name}’: custom Python citation source "
                f"classes are no longer supported (citation retrieval now happens in the "
                f"autocitefetch library).  Use one of: "
                f"{', '.join(sorted(set(_SOURCE_KINDS)))}"
            )
        raise ValueError(
            f"flm_citations: unknown citation source ‘{name}’; expected one of "
            f"{', '.join(sorted(set(_SOURCE_KINDS)))}"
        )

    kind = _SOURCE_KINDS[name]
    what = f"citation source ‘{name}’"

    # The `doc` the old source constructors were handed is not config.
    config.pop('doc', None)

    # A kind's own name is its default prefix.  Note `pop(...) or kind` would be
    # wrong: an explicit `cite_prefix: ''` is a mistake worth naming, not a
    # request for the default.
    prefix = config.pop('cite_prefix', None)
    if prefix is None:
        prefix = kind
    elif not prefix or ':' in prefix:
        raise ValueError(
            f"flm_citations: {what}: `cite_prefix` must be a non-empty string "
            f"without a `:` (citation keys are `prefix:key`, split at the first "
            f"colon), not {prefix!r}"
        )

    for option, why in _OBSOLETE_SOURCE_OPTIONS.items():
        if option in config:
            config.pop(option)
            _deprecated(what, f"the option ‘{option}’ is ignored ({why})")

    spec = {'kind': kind, 'prefix': prefix}

    if kind == 'arxiv':
        _build_arxiv(spec, config, what, doc, cwd)
    elif kind == 'manual':
        spec['format'] = config.pop('format', None) or manual_format
    elif kind == 'bib':
        _build_bib(spec, config, what, doc, cwd)

    if config:
        raise ValueError(
            f"flm_citations: {what}: unknown option(s) "
            f"{', '.join(sorted(config.keys()))}"
        )

    return spec


def _build_arxiv(spec, config, what, doc, cwd):
    # `chain_to_doi: false` is now "chain to nothing"; the target prefix is
    # configuration rather than a hard-coded `doi`, so that a renamed DOI source
    # still works.  A missing key means "chain to whatever the DOI source ended
    # up registered as", filled in by `_fix_up_chain_targets`.
    if 'chain_dois_to' in config:
        spec['chain_dois_to'] = config.pop('chain_dois_to')
    elif 'chain_to_doi' in config:
        chain = config.pop('chain_to_doi')
        spec['chain_dois_to'] = None if not chain else 'doi'

    overrides = _pop_alias(config, what, 'override_arxiv_dois', 'override_dois')
    if overrides:
        spec['override_dois'] = dict(overrides)

    overrides_file = _pop_alias(
        config, what, 'override_arxiv_dois_file', 'override_dois_file')
    if overrides_file:
        # Resolved relative to the document like a bibliography file, but
        # unlike one it must be a local path: it is read directly rather than
        # fetched, so a URL here would fail with a bare "no such file".
        if _is_url(overrides_file):
            raise ValueError(
                f"flm_citations: {what}: the arXiv DOI override file must be a "
                f"local path, not a URL ({overrides_file!r})"
            )
        spec['override_dois_file'] = resolve_bibliography_location(
            _replace_vars(overrides_file, doc), cwd)


def _build_bib(spec, config, what, doc, cwd):
    files = _pop_alias(config, what, 'bibliography_file', 'files')

    if files is None:
        files = default_bibliography_files(doc, cwd)
    elif isinstance(files, str):
        files = [files]

    files = [_replace_vars(f, doc) for f in files]
    spec['files'] = [resolve_bibliography_location(f, cwd) for f in files]

    fmt = config.pop('format', None)
    if fmt is not None:
        spec['format'] = fmt

    ttl = config.pop('ttl_seconds', None)
    if ttl is not None:
        spec['ttl_seconds'] = ttl


def _fix_up_chain_targets(specs):
    r"""Point each arXiv source at whatever prefix the DOI source really got.

    Prefixes are host-chosen now, so ``cite_prefix: dx`` on the DOI source is
    legitimate — and it would silently break every chained arXiv citation if
    arXiv kept pointing at a literal ``doi``.  Only a target the config did not
    state is filled in.

    Every arXiv spec leaves here with an explicit ``chain_dois_to``, so the
    extension never falls back to its own default.  That matters for the error
    message: this layer knows what the user wrote and can name it, whereas the
    extension would only see a prefix that appears nowhere in their config.
    """
    doi_prefixes = [s['prefix'] for s in specs if s['kind'] == 'doi']

    for spec in specs:
        if spec['kind'] != 'arxiv' or 'chain_dois_to' in spec:
            continue

        if len(doi_prefixes) == 1:
            spec['chain_dois_to'] = doi_prefixes[0]
        elif not doi_prefixes:
            # No DOI source at all: chaining would point nowhere, so keep the
            # arXiv metadata instead of manufacturing failures for citations
            # the user never wrote.
            spec['chain_dois_to'] = None
        else:
            raise ValueError(
                f"flm_citations: citation source ‘arxiv’: there are several DOI "
                f"citation sources ({', '.join(doi_prefixes)}), so it is ambiguous "
                f"which one arXiv entries should chain to.  Set `chain_dois_to` on "
                f"the arxiv source to one of them (or to null to disable chaining)."
            )


def normalize_cache_location(cache_file, cache_dir, cache_base, cwd):
    r"""Work out the cache directory and base name.

    The cache used to be a single JSON file named by ``cache_file``.  It is now
    a family of files sharing a base name — ``<base>.jsonl`` holds the entries
    and every throwaway file the writer needs carries a ``._<base>`` prefix and
    deletes itself — so the option becomes a directory plus a stem.
    """
    if cache_file is not None:
        _deprecated(
            "the `cache_file` option",
            "the cache is now a set of files sharing a base name; use `cache_dir` "
            "and `cache_base` instead (the old cache file is not read and can be deleted)",
        )
        if cache_dir is None and cache_base is None:
            head, tail = os.path.split(cache_file)
            if head:
                cache_dir = head
            # `.flm-citations.cache.json` -> `.flm-citations`, not
            # `.flm-citations.cache`.
            base, ext = os.path.splitext(tail)
            if ext == '.json' and base.endswith('.cache'):
                base = base[: -len('.cache')]
            cache_base = base

    if cache_base is None:
        cache_base = '.flm-citations'
    if cache_dir is None:
        cache_dir = cwd or '.'
    elif not os.path.isabs(cache_dir):
        cache_dir = os.path.join(cwd, cache_dir)

    return cache_dir, cache_base


def warn_obsolete_feature_options(cache_entry_duration_dt):
    r"""Report feature-level options that no longer have an effect."""
    if cache_entry_duration_dt is not None:
        _deprecated(
            "the `cache_entry_duration_dt` option",
            "cache lifetimes are now per source (arXiv 10 days, DOI 360 days, "
            "bibliography files 60 s, manual entries not cached at all); only a "
            "bibliography source's lifetime is configurable, via its `ttl_seconds` option",
        )


def verbosity_from_logger(retrieve_logger):
    r"""Map the retrieval logger's level onto the extension's 0/1/2 verbosity.

    Progress reporting costs nothing when nobody is listening — verbosity 0
    attaches no reporter at all — so deriving it from the logger means FLM's own
    ``-v`` flags drive it without a separate option.
    """
    if retrieve_logger.isEnabledFor(logging.DEBUG):
        return 2
    if retrieve_logger.isEnabledFor(logging.INFO):
        return 1
    return 0
