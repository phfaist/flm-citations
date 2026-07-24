//! `flm_citations._autocitefetch` — the Rust half of the `flm-citations`
//! package.
//!
//! This extension is a thin binding over the [`autocitefetch`] library, which
//! owns everything to do with *obtaining* a citation: HTTP, retry and backoff,
//! rate limiting, chunking, the on-disk cache and its TTL policy, arXiv Atom
//! parsing and version resolution, doi.org content negotiation, bibliography
//! files, chain discovery and chain-following, key normalization, and progress
//! reporting.
//!
//! The Python half owns everything to do with *presenting* one: the FLM node
//! scanner, `citeproc-py` rendering, the CSL styles, BibTeX/CSL-JSON export,
//! and the feature configuration. The interface between them is plain
//! CSL-JSON — Python receives `dict`s and never holds a handle on Rust state
//! other than the manager itself.
//!
//! Everything importable lives in [`manager`]; the other modules are its
//! helpers.

mod convert;
mod formats;
mod manager;
mod reporter;
mod sources;

use pyo3::prelude::*;

pyo3::create_exception!(
    flm_citations._autocitefetch,
    CitationError,
    pyo3::exceptions::PyException,
    "A citation could not be retrieved or read back."
);

#[pymodule]
fn _autocitefetch(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("CitationError", m.py().get_type::<CitationError>())?;
    m.add_class::<manager::PyCitationManager>()?;
    m.add_class::<manager::PyCiteFailure>()?;
    Ok(())
}
