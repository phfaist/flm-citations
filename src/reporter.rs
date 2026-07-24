//! Forwarding autocitefetch progress [`Event`]s to Python.
//!
//! Two sinks, both optional and independent:
//!
//! * a Python `logging.Logger` handed in by the Python layer (which owns the
//!   name, and whose level also decides `verbosity`) — this is how the rest of
//!   FLM reports progress and therefore how a user's `-v` flags reach here;
//! * an arbitrary `on_event` callable, for a host that wants to drive a
//!   progress bar off the structured data rather than off log lines.
//!
//! # Constraints this module has to respect
//!
//! [`Reporter::report`] is **synchronous, `&self`, returns nothing, and must
//! not panic** — it is called from inside the retrieval loop, between network
//! requests, and the library relies on it being unable to influence control
//! flow. So every Python error raised by a callback (or by a `logging` handler)
//! is reported through [`PyErr::write_unraisable`] and swallowed: a broken
//! progress display must not abort a document build.
//!
//! We hold the GIL for the whole `block_on` (the manager is `unsendable`; see
//! [`crate::manager`]), so the `Python::attach` here is a cheap re-entrant
//! acquire rather than a real one.
//!
//! [`Event`] is `#[non_exhaustive]`: a newer library may emit something this
//! build has never heard of, which is not an error — hence the wildcard arm.
//!
//! # Relationship to `autocitefetch-cli`
//!
//! The match arms, the message wording, [`WAIT_NOTICE`] and [`dur`] are lifted
//! from `autocitefetch-cli/src/progress.rs` and **kept in sync by hand** — the
//! CLI is a binary crate, so there is nothing to depend on, and the library
//! deliberately ships no renderer ("formatting is the host's business"). If the
//! two ever need to diverge, this is the copy that may.

use std::time::Duration;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use autocitefetch::report::{Event, Reporter, Resolved, Wait};

/// Rate-limit pauses shorter than this are not worth a log line at verbosity 1:
/// they read as normal pacing rather than as the tool having stopped. Backoff
/// waits are always announced — they mean something went wrong.
const WAIT_NOTICE: Duration = Duration::from_secs(2);

/// The three bound logger methods this reporter calls.
///
/// Resolved once rather than per line: `call_method1("info", …)` would redo the
/// attribute lookup and build a fresh bound-method object for every message,
/// and at verbosity 2 that is several per HTTP request plus one per citation.
struct LogMethods {
    info: Py<PyAny>,
    debug: Py<PyAny>,
    warning: Py<PyAny>,
}

/// Forwards events to a Python logger and/or an `on_event` callable.
pub struct PyReporter {
    /// The logger's `info`/`debug`/`warning`, or `None` to log nothing.
    log: Option<LogMethods>,
    /// A host callable taking one dict per event.
    on_event: Option<Py<PyAny>>,
    /// 1 = milestones, 2 = everything (per-request, per-citation).
    verbosity: u8,
}

impl PyReporter {
    /// Build a reporter around `logger` (a Python `logging.Logger`).
    ///
    /// The logger is passed in rather than looked up by name here: the Python
    /// layer already owns it — it is the same logger whose level decides
    /// `verbosity` — and a name duplicated across the language boundary would
    /// silently desynchronize the day the package or sub-logger is renamed.
    ///
    /// `verbosity` 0 still builds a reporter when `on_event` is set — a
    /// structured consumer is not a log level — but attaches no logger.
    pub fn new(
        py: Python<'_>,
        logger: Option<&Bound<'_, PyAny>>,
        verbosity: u8,
        on_event: Option<Py<PyAny>>,
    ) -> PyResult<Self> {
        let log = match logger {
            Some(logger) if verbosity > 0 => Some(LogMethods {
                info: logger.getattr("info")?.unbind(),
                debug: logger.getattr("debug")?.unbind(),
                warning: logger.getattr("warning")?.unbind(),
            }),
            _ => None,
        };
        let _ = py;
        Ok(PyReporter {
            log,
            on_event,
            verbosity,
        })
    }

    /// Call one of the bound logger methods.
    ///
    /// The message is pre-formatted rather than passed as a `%`-style template:
    /// event fields are borrowed `&str`s and integers with no stable argument
    /// shape across variants, so there is nothing to gain by deferring.
    fn emit_log(&self, py: Python<'_>, pick: fn(&LogMethods) -> &Py<PyAny>, msg: &str) {
        let Some(log) = &self.log else { return };
        let method = pick(log);
        if let Err(e) = method.bind(py).call1((msg,)) {
            e.write_unraisable(py, Some(&method.bind(py).clone()));
        }
    }

    fn info(&self, py: Python<'_>, msg: &str) {
        self.emit_log(py, |l| &l.info, msg);
    }

    fn debug(&self, py: Python<'_>, msg: &str) {
        self.emit_log(py, |l| &l.debug, msg);
    }

    fn warning(&self, py: Python<'_>, msg: &str) {
        self.emit_log(py, |l| &l.warning, msg);
    }

    /// Hand a structured form of the event to the `on_event` callable.
    fn emit(&self, py: Python<'_>, ev: &Event<'_>) {
        let Some(cb) = &self.on_event else { return };
        let dict = match event_dict(py, ev) {
            Ok(d) => d,
            Err(e) => {
                e.write_unraisable(py, None);
                return;
            }
        };
        if let Err(e) = cb.bind(py).call1((dict,)) {
            e.write_unraisable(py, Some(&cb.bind(py).clone()));
        }
    }
}

impl Reporter for PyReporter {
    fn report(&self, ev: &Event<'_>) {
        Python::attach(|py| {
            self.emit(py, ev);

            if self.log.is_none() {
                return;
            }
            let verbose = self.verbosity >= 2;

            match *ev {
                Event::RetrieveStarted { cites } => {
                    self.info(py, &format!("retrieving {cites} citation(s)"));
                }

                Event::PassStarted {
                    pass,
                    cached,
                    to_fetch,
                } => {
                    // Nothing to fetch and nothing cached is the overwhelmingly
                    // common steady state (an unchanged document with a warm
                    // cache); a line per pass there is pure noise.
                    if to_fetch > 0 {
                        self.info(
                            py,
                            &format!("pass {pass}: {cached} cached, {to_fetch} to fetch"),
                        );
                    } else {
                        self.debug(py, &format!("pass {pass}: {cached} cached, nothing to fetch"));
                    }
                }

                // A non-zero count is the interesting thing: it is what makes a
                // progress denominator grow mid-run.
                Event::PassFinished { pass, discovered } if discovered > 0 => {
                    self.info(
                        py,
                        &format!("pass {pass}: queued {discovered} chained citation(s)"),
                    );
                }
                Event::PassFinished { .. } => {}

                Event::SourceStarted {
                    prefix,
                    keys,
                    chunks,
                } => self.info(
                    py,
                    &format!("{prefix}: fetching {keys} key(s) in {chunks} request(s)"),
                ),

                Event::SourceProgress {
                    prefix,
                    done,
                    total,
                } => self.info(py, &format!("{prefix}: {done}/{total}")),

                // Carries the final count, so a run whose intermediate samples
                // were throttled away still ends on a complete reading.
                Event::SourceFinished { prefix, done } => {
                    self.debug(py, &format!("{prefix}: {done} key(s) done"))
                }

                Event::CiteResolved { prefix, key, how } if verbose => match how {
                    Resolved::Chained {
                        prefix: tp,
                        key: tk,
                    } => self.debug(py, &format!("{prefix}:{key} -> {tp}:{tk}")),
                    _ => self.debug(py, &format!("{prefix}:{key}: resolved")),
                },
                Event::CiteResolved { .. } => {}

                // Only the grace-served case is logged here. A reported failure
                // reaches the RetrieveReport, and `feature.py` renders it with
                // the document location and the `origin` attribution, which
                // this event cannot know about — logging it too would double
                // every failure. Grace-serving is the opposite: it is invisible
                // in the report by design, so this is the only way to see
                // stale-while-revalidate doing its job.
                Event::CiteFailed {
                    prefix,
                    key,
                    err,
                    grace_served: true,
                } => self.warning(
                    py,
                    &format!("{prefix}:{key}: {err} — serving the cached copy"),
                ),
                Event::CiteFailed { .. } => {}

                Event::WaitStarted { what, expected } => match what {
                    Wait::RateLimit { prefix } => {
                        let d = expected.unwrap_or_default();
                        if verbose || d >= WAIT_NOTICE {
                            self.info(
                                py,
                                &format!("{prefix}: waiting {} (rate limit)", dur(d)),
                            );
                        }
                    }
                    Wait::Backoff { url, attempt } => self.warning(
                        py,
                        &format!(
                            "retrying {url} in {} (attempt {})",
                            dur(expected.unwrap_or_default()),
                            attempt + 2
                        ),
                    ),
                    Wait::CacheFlush => self.debug(py, "compacting the citation cache"),
                    // A wait this build has no name for is still worth
                    // announcing: the point of these events is that the tool is
                    // not hung.
                    _ => self.info(
                        py,
                        &format!("waiting {}", dur(expected.unwrap_or_default())),
                    ),
                },

                Event::WaitFinished { .. } => {}

                Event::RequestStarted { url, attempt } if verbose => {
                    if attempt == 0 {
                        self.debug(py, &format!("GET {url}"));
                    } else {
                        self.debug(py, &format!("GET {url} (attempt {})", attempt + 1));
                    }
                }
                Event::RequestStarted { .. } => {}

                Event::RequestFinished { url, result } if verbose => match result {
                    Ok(status) => self.debug(py, &format!("{status} <- {url}")),
                    Err(e) => self.debug(py, &format!("failed <- {url}: {e}")),
                },
                Event::RequestFinished { .. } => {}

                Event::RetrieveFinished { considered, failed } => self.info(
                    py,
                    &format!(
                        "retrieval done: {considered} citation(s) considered, {failed} failed"
                    ),
                ),

                _ => {}
            }
        });
    }
}

/// A structured form of one event, for the `on_event` callable.
///
/// Every dict carries a `type` naming the variant in `snake_case`, plus that
/// variant's fields under their own names. New variants a future library adds
/// arrive as `{"type": "unknown"}` rather than being dropped, so a consumer can
/// at least count them.
fn event_dict<'py>(py: Python<'py>, ev: &Event<'_>) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    match *ev {
        Event::RetrieveStarted { cites } => {
            d.set_item("type", "retrieve_started")?;
            d.set_item("cites", cites)?;
        }
        Event::PassStarted {
            pass,
            cached,
            to_fetch,
        } => {
            d.set_item("type", "pass_started")?;
            d.set_item("pass", pass)?;
            d.set_item("cached", cached)?;
            d.set_item("to_fetch", to_fetch)?;
        }
        Event::PassFinished { pass, discovered } => {
            d.set_item("type", "pass_finished")?;
            d.set_item("pass", pass)?;
            d.set_item("discovered", discovered)?;
        }
        Event::RetrieveFinished { considered, failed } => {
            d.set_item("type", "retrieve_finished")?;
            d.set_item("considered", considered)?;
            d.set_item("failed", failed)?;
        }
        Event::SourceStarted {
            prefix,
            keys,
            chunks,
        } => {
            d.set_item("type", "source_started")?;
            d.set_item("prefix", prefix)?;
            d.set_item("keys", keys)?;
            d.set_item("chunks", chunks)?;
        }
        Event::SourceProgress {
            prefix,
            done,
            total,
        } => {
            d.set_item("type", "source_progress")?;
            d.set_item("prefix", prefix)?;
            d.set_item("done", done)?;
            d.set_item("total", total)?;
        }
        Event::SourceFinished { prefix, done } => {
            d.set_item("type", "source_finished")?;
            d.set_item("prefix", prefix)?;
            d.set_item("done", done)?;
        }
        Event::CiteResolved { prefix, key, how } => {
            d.set_item("type", "cite_resolved")?;
            d.set_item("prefix", prefix)?;
            d.set_item("key", key)?;
            match how {
                Resolved::Chained {
                    prefix: tp,
                    key: tk,
                } => {
                    d.set_item("how", "chained")?;
                    d.set_item("target", PyList::new(py, [tp, tk])?)?;
                }
                _ => d.set_item("how", "concrete")?,
            }
        }
        Event::CiteFailed {
            prefix,
            key,
            err,
            grace_served,
        } => {
            d.set_item("type", "cite_failed")?;
            d.set_item("prefix", prefix)?;
            d.set_item("key", key)?;
            d.set_item("message", err.to_string())?;
            d.set_item("grace_served", grace_served)?;
        }
        Event::WaitStarted { what, expected } => {
            d.set_item("type", "wait_started")?;
            set_wait(&d, what)?;
            d.set_item("expected_ms", expected.map(|e| e.as_millis() as u64))?;
        }
        Event::WaitFinished { what } => {
            d.set_item("type", "wait_finished")?;
            set_wait(&d, what)?;
        }
        Event::RequestStarted { url, attempt } => {
            d.set_item("type", "request_started")?;
            d.set_item("url", url)?;
            d.set_item("attempt", attempt)?;
        }
        Event::RequestFinished { url, result } => {
            d.set_item("type", "request_finished")?;
            d.set_item("url", url)?;
            match result {
                Ok(status) => d.set_item("status", status)?,
                Err(e) => d.set_item("error", e.to_string())?,
            }
        }
        _ => d.set_item("type", "unknown")?,
    }
    Ok(d)
}

fn set_wait(d: &Bound<'_, PyDict>, what: Wait<'_>) -> PyResult<()> {
    match what {
        Wait::RateLimit { prefix } => {
            d.set_item("wait", "rate_limit")?;
            d.set_item("prefix", prefix)?;
        }
        Wait::Backoff { url, attempt } => {
            d.set_item("wait", "backoff")?;
            d.set_item("url", url)?;
            d.set_item("attempt", attempt)?;
        }
        Wait::CacheFlush => d.set_item("wait", "cache_flush")?,
        _ => d.set_item("wait", "unknown")?,
    }
    Ok(())
}

/// A wait duration, rendered the way a person reads it.
fn dur(d: Duration) -> String {
    let ms = d.as_millis();
    if ms >= 1000 {
        format!("{:.1}s", ms as f64 / 1000.0)
    } else {
        format!("{ms}ms")
    }
}
