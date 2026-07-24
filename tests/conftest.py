r"""Shared fixtures.

Everything here is offline: the `bib` and `manual` sources reach no network,
which is what lets the whole default suite run in CI.  Tests that do hit arXiv
or doi.org are marked ``network`` and deselected with ``-m 'not network'``.

Document rendering goes through the ``flm`` command line rather than
``flm.main.run``, for two reasons: it is the path users actually take, and it
exercises the two-pass scan/render flow — the part the migration reorganized —
without this suite having to reproduce the run-info scaffolding the library
entry point expects.
"""

import json
import logging
import os
import subprocess
import sys

import pytest

import yaml


#: A CSL-JSON bibliography entry citeproc-py can actually render.  Note
#: ``issued`` must be ``{"date-parts": ...}``: citeproc-py rejects the
#: ``[{year: …}]`` list form some CSL-YAML exporters produce.
KNUTH = {
    'type': 'article-journal',
    'title': 'Literate Programming',
    'author': [{'family': 'Knuth', 'given': 'Donald E.'}],
    'container-title': 'The Computer Journal',
    'issued': {'date-parts': [[1984]]},
    'DOI': '10.1093/comjnl/27.2.97',
}

REFS_YAML = """\
Knuth1984:
  type: article-journal
  title: Literate Programming
  author:
    - family: Knuth
      given: Donald E.
  container-title: The Computer Journal
  issued:
    date-parts:
      - [1984]
  DOI: 10.1093/comjnl/27.2.97

# The flat pre-formatted key this package used before 0.3.  Bibliography files
# in the wild are full of it, so it has to keep working alongside the library's
# `_ready_formatted` format map.
PreskillNotes:
  _formatted_flm_text: >-
    J. Preskill. \\emph{Lecture notes on Quantum Computation.} (1997-2020)
"""


@pytest.fixture
def bibdir(tmp_path):
    r"""A directory holding one YAML and one JSON bibliography."""
    (tmp_path / 'refs.yaml').write_text(REFS_YAML, encoding='utf-8')
    (tmp_path / 'refs.json').write_text(
        json.dumps([dict(KNUTH, id='Knuth1984Json')]), encoding='utf-8'
    )
    return tmp_path


@pytest.fixture
def make_manager(tmp_path):
    r"""Build a `CitationManager` over the offline sources, in `tmp_path`."""
    from flm_citations._autocitefetch import CitationManager

    def _make(sources, **kwargs):
        kwargs.setdefault('cache_dir', str(tmp_path))
        kwargs.setdefault('cache_base', '.flm-citations')
        if kwargs.get('verbosity'):
            # The extension takes the logger object, not its name.
            kwargs.setdefault(
                'logger', logging.getLogger('flm_citations.retrieve'))
        return CitationManager({'sources': sources}, **kwargs)

    return _make


class FlmResult:
    """The outcome of one `flm` run."""

    def __init__(self, proc, workdir):
        self.returncode = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.workdir = workdir

    @property
    def ok(self):
        return self.returncode == 0

    def read(self, name):
        with open(os.path.join(self.workdir, name), encoding='utf-8') as f:
            return f.read()

    def __repr__(self):
        return f"FlmResult(rc={self.returncode}, stderr={self.stderr!r})"


@pytest.fixture
def run_flm(tmp_path):
    r"""Render an FLM document with the `flm` CLI in an isolated directory."""

    def _run(content, config=None, workdir=None, args=()):
        workdir = str(workdir or tmp_path)
        with open(os.path.join(workdir, 'doc.flm'), 'w', encoding='utf-8') as f:
            f.write(content)

        # The config file is always written, even when the test passes none:
        # `flm_citations` is a plug-in feature, and without an entry enabling it
        # `\cite` is simply an unknown macro.
        cfgpath = os.path.join(workdir, 'flmconfig.yaml')
        with open(cfgpath, 'w', encoding='utf-8') as f:
            yaml.safe_dump(
                {'flm': {'features': {'flm_citations': config or {}}}}, f
            )

        cmd = [sys.executable, '-m', 'flm', 'doc.flm', '-o', 'out.html',
               '-C', 'flmconfig.yaml']
        cmd += list(args)

        proc = subprocess.run(
            cmd, cwd=workdir, capture_output=True, text=True, timeout=300,
            # Keep a developer's own FLM config out of the test run.
            env={**os.environ, 'PYTHONWARNINGS': 'ignore'},
        )
        return FlmResult(proc, workdir)

    return _run
