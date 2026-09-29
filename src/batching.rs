//! Per-source overrides of the library's refresh batching.
//!
//! [`RefreshBatching`] decides, per source, whether a few stale cache entries
//! are refetched now or deferred until there are enough to be worth a
//! (rate-limited) request, and whether a request going out anyway is topped up
//! with entries that are nearly due. Every built-in source declares its own
//! default (arXiv waits for 20, doi.org for 10, a bibliography file refreshes
//! everything at once); a source spec's `refresh_batching` entry adjusts it:
//!
//! ```python
//! "eager"                                   # fetch what is due, when due
//! {"min_batch": 50}                         # change one setting, keep the rest
//! {"eager": True, "min_batch": 3}           # start from "eager" instead
//! {"max_defer_days": 0.5}                   # an expired entry waits at most 12 h
//! {"top_up": False}                         # never pull entries forward
//! {"top_up": {"min_age_percent": 80, "fill": 5}}   # fill: "chunk" or a count
//! ```
//!
//! This mirrors the CLI's `--refresh-batching PREFIX:SETTINGS` (kept in sync
//! by hand with `autocitefetch-cli/src/batching.rs`): settings are applied on
//! top of the source's default, which is only known once the source is
//! registered — hence the parse/[`apply`](BatchingOverride::apply) split.

use std::time::Duration;

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use autocitefetch::{Fill, RefreshBatching, TopUp};

use crate::convert::{get_opt_f64, get_opt_usize, opt, reject_unknown_keys};

/// A parsed `refresh_batching` entry, not yet applied to a source default.
#[derive(Clone, Debug, Default)]
pub struct BatchingOverride {
    eager: bool,
    min_batch: Option<usize>,
    max_defer: Option<Duration>,
    top_up: Option<TopUpOverride>,
}

#[derive(Clone, Debug)]
enum TopUpOverride {
    Off,
    Set {
        min_age_percent: Option<u32>,
        fill: Option<Fill>,
    },
}

impl BatchingOverride {
    /// Parse a spec's `refresh_batching` value: `"eager"`, `"default"`, or a
    /// mapping of settings.
    pub fn parse(value: &Bound<'_, PyAny>, what: &str) -> PyResult<Self> {
        let what = format!("{what}: `refresh_batching`");
        if let Ok(s) = value.extract::<String>() {
            return match s.as_str() {
                "eager" => Ok(BatchingOverride {
                    eager: true,
                    ..Default::default()
                }),
                "default" => Ok(BatchingOverride::default()),
                _ => Err(PyValueError::new_err(format!(
                    "{what}: expected `eager`, `default` or a mapping of settings, got `{s}`"
                ))),
            };
        }
        let spec = value.cast::<PyDict>().map_err(|_| {
            PyTypeError::new_err(format!(
                "{what}: expected `eager`, `default` or a mapping of settings"
            ))
        })?;
        reject_unknown_keys(spec, &["eager", "min_batch", "max_defer_days", "top_up"], &what)?;

        let eager = match opt(spec, "eager")? {
            Some(v) => v
                .extract::<bool>()
                .map_err(|_| PyTypeError::new_err(format!("{what}: `eager` must be a boolean")))?,
            None => false,
        };
        let min_batch = get_opt_usize(spec, "min_batch", &what)?;
        let max_defer = match get_opt_f64(spec, "max_defer_days", &what)? {
            Some(days) if !(days >= 0.0 && days.is_finite()) => {
                return Err(PyValueError::new_err(format!(
                    "{what}: `max_defer_days` must be a non-negative number"
                )));
            }
            Some(days) => Some(Duration::from_secs_f64(days * 24.0 * 60.0 * 60.0)),
            None => None,
        };
        // Not `opt()`: an explicit `None` here means "off", not "absent".
        let top_up = match spec.get_item("top_up")? {
            None => None,
            Some(v) => Some(parse_top_up(&v, &what)?),
        };

        Ok(BatchingOverride {
            eager,
            min_batch,
            max_defer,
            top_up,
        })
    }

    /// These settings applied on top of `base` (the source's default): first
    /// the `eager` reset, then each setting given.
    pub fn apply(&self, mut base: RefreshBatching, what: &str) -> PyResult<RefreshBatching> {
        if self.eager {
            base = RefreshBatching::EAGER;
        }
        if let Some(n) = self.min_batch {
            base.min_batch = n;
        }
        if let Some(d) = self.max_defer {
            base.max_defer = d;
        }
        match self.top_up {
            None => {}
            Some(TopUpOverride::Off) => base.top_up = None,
            Some(TopUpOverride::Set {
                min_age_percent,
                fill,
            }) => {
                let min_age_percent = min_age_percent
                    .or(base.top_up.map(|t| t.min_age_percent))
                    .ok_or_else(|| {
                        PyValueError::new_err(format!(
                            "{what}: `refresh_batching`: top-up is off for this source, so \
                             `top_up` needs a `min_age_percent` to turn it on"
                        ))
                    })?;
                let fill = fill
                    .or(base.top_up.map(|t| t.fill))
                    .unwrap_or(Fill::ChunkBoundary);
                base.top_up = Some(TopUp {
                    min_age_percent,
                    fill,
                });
            }
        }
        Ok(base)
    }
}

fn parse_top_up(v: &Bound<'_, PyAny>, what: &str) -> PyResult<TopUpOverride> {
    if v.is_none() {
        return Ok(TopUpOverride::Off);
    }
    // `True` would have to invent a percentage; only `False` is meaningful.
    if let Ok(b) = v.extract::<bool>() {
        return if b {
            Err(PyValueError::new_err(format!(
                "{what}: `top_up: true` is not enough; give a mapping with `min_age_percent` \
                 (and optionally `fill`)"
            )))
        } else {
            Ok(TopUpOverride::Off)
        };
    }
    if let Ok(s) = v.extract::<String>() {
        if s == "off" {
            return Ok(TopUpOverride::Off);
        }
    }
    let what = format!("{what}: `top_up`");
    let spec = v.cast::<PyDict>().map_err(|_| {
        PyTypeError::new_err(format!(
            "{what} must be false/null to disable it, or a mapping with `min_age_percent` \
             and/or `fill`"
        ))
    })?;
    reject_unknown_keys(spec, &["min_age_percent", "fill"], &what)?;

    let min_age_percent = match get_opt_usize(spec, "min_age_percent", &what)? {
        Some(p) if p > 100 => {
            return Err(PyValueError::new_err(format!(
                "{what}: `min_age_percent` is a percentage (0-100), got {p}"
            )));
        }
        Some(p) => Some(p as u32),
        None => None,
    };
    let fill = match opt(spec, "fill")? {
        None => None,
        Some(f) => Some(match f.extract::<String>() {
            Ok(s) if s == "chunk" => Fill::ChunkBoundary,
            _ => Fill::Extra(f.extract::<usize>().map_err(|_| {
                PyTypeError::new_err(format!(
                    "{what}: `fill` must be `chunk` or a non-negative integer"
                ))
            })?),
        }),
    };

    Ok(TopUpOverride::Set {
        min_age_percent,
        fill,
    })
}
