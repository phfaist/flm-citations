//! The `CitationManager` Python class: the whole Rust↔Python surface.
//!
//! Three shapes are worth knowing before reading this file.
//!
//! **No async runtime.** Every `autocitefetch-std` backend is
//! *blocking-in-a-future*: it does its work on the first poll and returns
//! `Poll::Ready`. So the futures are driven by a `Waker::noop()` poll loop
//! ([`block_on`]) and nothing pulls in an executor — the same convention the
//! library's tests, example and CLI follow.
//!
//! **The class is `unsendable`.** [`CitationManager`](autocitefetch::CitationManager)
//! holds `Box<dyn Source>`, an `Rc<dyn Reporter>` and `!Send` `BoxFuture`s.
//! That is deliberate upstream (WASM futures are `!Send`), and it means the GIL
//! cannot be released around a fetch with `Python::detach`: a slow arXiv
//! request blocks other Python threads for its duration. Acceptable here — an
//! FLM document build is single-threaded — and it is what lets the reporter
//! call back into Python without acquiring anything.
//!
//! **Two-phase retrieval.** [`retrieve`](PyCitationManager::retrieve) populates
//! the cache and returns only a failure report;
//! [`get`](PyCitationManager::get) reads items back, following arXiv → DOI
//! chain pointers. This maps one-to-one onto FLM's two passes: the document
//! scan retrieves, the render reads.

use std::future::Future;
use std::path::PathBuf;
use std::rc::Rc;
use std::task::{Context, Poll, Waker};
use std::time::Duration;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use autocitefetch::report::{Reporter, ThrottledReporter};
use autocitefetch::{CitationManager, CslValue, RetryPolicy, TtlPolicy};
use autocitefetch_std::{BlockingTimer, SingleFileCacheStore, SystemClock, UreqFetcher};

use crate::convert::{from_csl, get_dict_list, get_opt_f64, get_opt_usize};
use crate::reporter::PyReporter;
use crate::sources;
use crate::CitationError;

/// Only *sample* events are throttled, and every `*Finished` event carries
/// final counts, so a display can never be left stuck short of its total. One
/// second matches the CLI: `doi:` is paced at 1100 ms, so an unthrottled
/// counter would put a line between every pair of requests.
const PROGRESS_THROTTLE: Duration = Duration::from_secs(1);

type Manager = CitationManager<UreqFetcher, SingleFileCacheStore, SystemClock, BlockingTimer>;

/// One citation that could not be retrieved.
///
/// Mirrors `autocitefetch::CiteFailure`. [`origin`](Self::origin) is the point:
/// when an `arxiv:` citation chains to a DOI and *that* fetch fails, `prefix`
/// and `key` name the DOI — which the user never wrote — while `origin` names
/// the arXiv citation they did. The library guarantees every requested citation
/// that ultimately fails is discoverable either directly or via some failure's
/// `origin`, so joining this list against the request list never wrongly
/// concludes a citation succeeded.
#[pyclass(module = "flm_citations._autocitefetch", frozen, name = "CiteFailure")]
pub struct PyCiteFailure {
    /// The prefix of the citation that actually failed.
    #[pyo3(get)]
    prefix: String,
    /// The key of the citation that actually failed.
    #[pyo3(get)]
    key: String,
    /// A human-readable explanation.
    #[pyo3(get)]
    message: String,
    /// The originally requested `(prefix, key)` this failure descends from, or
    /// `None` when the failing citation *is* the requested one.
    #[pyo3(get)]
    origin: Option<(String, String)>,
}

#[pymethods]
impl PyCiteFailure {
    fn __repr__(&self) -> String {
        match &self.origin {
            Some((p, k)) => format!(
                "CiteFailure({}:{}, needed by {p}:{k}: {})",
                self.prefix, self.key, self.message
            ),
            None => format!("CiteFailure({}:{}: {})", self.prefix, self.key, self.message),
        }
    }
}

/// Resolves citations to CSL-JSON, backed by the Rust `autocitefetch` library.
#[pyclass(module = "flm_citations._autocitefetch", unsendable, name = "CitationManager")]
pub struct PyCitationManager {
    inner: Manager,
    /// Every registered prefix, in registration order — so the Python layer can
    /// validate a citation's prefix without keeping its own copy of the config.
    prefixes: Vec<String>,
}

#[pymethods]
impl PyCitationManager {
    #[new]
    #[pyo3(signature = (
        sources,
        *,
        cache_dir = None,
        cache_base = None,
        user_agent = None,
        drop_csl_fields = None,
        ttl_policy = None,
        retry = None,
        max_chain_depth = None,
        logger = None,
        verbosity = 0,
        on_event = None,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        sources: &Bound<'_, PyDict>,
        cache_dir: Option<PathBuf>,
        cache_base: Option<String>,
        user_agent: Option<String>,
        drop_csl_fields: Option<Vec<String>>,
        ttl_policy: Option<&Bound<'_, PyDict>>,
        retry: Option<&Bound<'_, PyDict>>,
        max_chain_depth: Option<usize>,
        logger: Option<&Bound<'_, PyAny>>,
        verbosity: u8,
        on_event: Option<Py<PyAny>>,
    ) -> PyResult<Self> {
        // `sources` arrives as a dict so it can be passed straight through from
        // Python keyword-style; the list lives under "sources".
        let specs = get_dict_list(sources, "sources", "citation sources")?;
        let mut registrations = Vec::with_capacity(specs.len());
        for spec in &specs {
            registrations.push(sources::build(spec)?);
        }
        sources::check_unique_prefixes(&registrations)?;
        sources::check_chain_targets(&registrations)?;

        let dir = cache_dir.unwrap_or_else(|| PathBuf::from("."));
        let base = cache_base.unwrap_or_else(|| ".flm-citations".to_string());
        let store = block_on(SingleFileCacheStore::with_base(&dir, &base)).map_err(|e| {
            CitationError::new_err(format!("citation cache in {}: {e}", dir.display()))
        })?;

        let fetcher = match user_agent {
            Some(ua) => UreqFetcher::with_user_agent(ua),
            None => UreqFetcher::new(),
        };

        let mut inner = CitationManager::new(fetcher, store, SystemClock, BlockingTimer);

        if let Some(fields) = drop_csl_fields {
            inner = inner.with_dropped_csl_fields(fields);
        }
        if let Some(policy) = ttl_policy {
            inner = inner.with_policy(build_ttl_policy(policy)?);
        }
        if let Some(policy) = retry {
            inner = inner.with_retry_policy(build_retry_policy(policy)?);
        }
        if let Some(depth) = max_chain_depth {
            inner = inner.with_max_chain_depth(depth);
        }

        // Verbosity 0 with no callback attaches nothing at all, leaving the
        // library's `NopReporter` — one vtable call to an empty body per event.
        if (verbosity > 0 && logger.is_some()) || on_event.is_some() {
            let reporter = PyReporter::new(py, logger, verbosity, on_event)?;
            let reporter: Rc<dyn Reporter> = if verbosity >= 2 {
                // `-vv` asked for detail: deliver every sample.
                Rc::new(reporter)
            } else {
                Rc::new(ThrottledReporter::new(
                    reporter,
                    SystemClock,
                    PROGRESS_THROTTLE,
                ))
            };
            inner = inner.with_reporter(reporter);
        }

        let mut prefixes = Vec::with_capacity(registrations.len());
        for reg in registrations {
            prefixes.push(reg.prefix.clone());
            inner = inner
                .register(reg.prefix.clone(), reg.source)
                .map_err(|e| PyValueError::new_err(format!("citation source `{}`: {e}", reg.prefix)))?;
        }

        Ok(PyCitationManager { inner, prefixes })
    }

    /// The citation prefixes this manager answers to, in registration order.
    #[getter]
    fn prefixes(&self) -> Vec<String> {
        self.prefixes.clone()
    }

    /// Fetch every missing or stale citation, following chain pointers.
    ///
    /// `cites` is a sequence of `(prefix, key)` pairs. Returns the citations
    /// that could not be resolved, as [`CiteFailure`](PyCiteFailure) objects —
    /// one bad citation never aborts the batch. Only a *cache* failure raises.
    fn retrieve(&self, py: Python<'_>, cites: &Bound<'_, PyAny>) -> PyResult<Py<PyList>> {
        let mut pairs: Vec<(String, String)> = Vec::new();
        for item in cites.try_iter()? {
            // `extract` to a 2-tuple already enforces the shape and both
            // element types.
            pairs.push(item?.extract().map_err(|_| {
                PyValueError::new_err("each citation must be a (prefix, key) tuple")
            })?);
        }

        let report = block_on(self.inner.retrieve(&pairs))
            .map_err(|e| CitationError::new_err(e.to_string()))?;

        let failures: Vec<Py<PyCiteFailure>> = report
            .failures
            .into_iter()
            .map(|f| {
                Py::new(
                    py,
                    PyCiteFailure {
                        prefix: f.prefix,
                        key: f.key,
                        message: f.message,
                        origin: f.origin,
                    },
                )
            })
            .collect::<PyResult<_>>()?;

        Ok(PyList::new(py, failures)?.unbind())
    }

    /// Read one resolved citation back as a CSL-JSON dict.
    ///
    /// Follows chain pointers, merges their `set_properties`, and rewrites `id`
    /// to the *normalized* `"prefix:key"` — so a citation written
    /// `doi:10.1103/PhysRevA.86.052329` reads back with a lowercased id, while
    /// the CSL `DOI` field keeps its registered case.
    ///
    /// Raises [`CitationError`] if the citation is not in the cache (it was
    /// never retrieved, or its chain target turned out to be gone).
    fn get(&self, py: Python<'_>, prefix: &str, key: &str) -> PyResult<Py<PyAny>> {
        let item: CslValue = block_on(self.inner.get(prefix, key))
            .map_err(|e| CitationError::new_err(format!("{prefix}:{key}: {e}")))?;
        Ok(from_csl(py, &item)?.unbind())
    }

    /// Drop cache entries that are expired past the policy's grace window.
    /// Returns how many were removed.
    fn prune(&self) -> PyResult<usize> {
        block_on(self.inner.prune()).map_err(|e| CitationError::new_err(e.to_string()))
    }
}

fn build_ttl_policy(spec: &Bound<'_, PyDict>) -> PyResult<TtlPolicy> {
    let what = "ttl_policy";
    crate::convert::reject_unknown_keys(
        spec,
        &["stale_percent", "jitter_percent", "grace_days"],
        what,
    )?;
    let mut policy = TtlPolicy::default();
    if let Some(v) = get_opt_usize(spec, "stale_percent", what)? {
        policy.stale_percent = v as u32;
    }
    if let Some(v) = get_opt_usize(spec, "jitter_percent", what)? {
        policy.jitter_percent = v as u32;
    }
    if let Some(days) = get_opt_f64(spec, "grace_days", what)? {
        if days < 0.0 {
            return Err(PyValueError::new_err(
                "ttl_policy: `grace_days` must not be negative",
            ));
        }
        policy.grace = Duration::from_secs_f64(days * 24.0 * 60.0 * 60.0);
    }
    Ok(policy)
}

fn build_retry_policy(spec: &Bound<'_, PyDict>) -> PyResult<RetryPolicy> {
    let what = "retry";
    crate::convert::reject_unknown_keys(
        spec,
        &["max_retries", "base_ms", "cap_ms", "honor_retry_after"],
        what,
    )?;
    let mut policy = RetryPolicy::default();
    if let Some(v) = get_opt_usize(spec, "max_retries", what)? {
        policy.max_retries = v as u32;
    }
    if let Some(v) = get_opt_usize(spec, "base_ms", what)? {
        policy.base = Duration::from_millis(v as u64);
    }
    if let Some(v) = get_opt_usize(spec, "cap_ms", what)? {
        policy.cap = Duration::from_millis(v as u64);
    }
    if let Some(v) = crate::convert::opt(spec, "honor_retry_after")? {
        policy.honor_retry_after = v.extract()?;
    }
    Ok(policy)
}

/// Drive a future to completion on this thread, with no async runtime.
///
/// Every backend used here resolves on the first poll (`UreqFetcher` blocks the
/// thread, `BlockingTimer` sleeps it, the cache store does blocking I/O), so a
/// no-op waker suffices. The poll budget is the safety net: with `Waker::noop()`
/// nothing can wake this loop, so a future that genuinely pended would spin
/// forever — fail loudly instead of hanging.
fn block_on<F: Future>(fut: F) -> F::Output {
    let mut fut = std::pin::pin!(fut);
    let waker = Waker::noop();
    let mut cx = Context::from_waker(waker);
    for _ in 0..1_000_000 {
        if let Poll::Ready(v) = fut.as_mut().poll(&mut cx) {
            return v;
        }
        std::thread::yield_now();
    }
    panic!(
        "citation retrieval did not complete (a backend unexpectedly pended — this driver only \
         works with the blocking autocitefetch-std backends)"
    );
}
