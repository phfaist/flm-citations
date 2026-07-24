//! Moving CSL-JSON across the language boundary, and reading config out of it.
//!
//! [`CslValue`] is `serde_json::Value`, so `pythonize` handles both directions
//! generically: a CSL item becomes an ordinary `dict` of ordinary Python
//! scalars, and nothing on the Python side ever handles a wrapper object. That
//! is the whole point of the boundary — Python receives data, not a view onto
//! Rust state.
//!
//! The rest of this module is the small amount of hand-written extraction the
//! config dicts need. It exists because `pythonize`'s errors name a serde path
//! (`sources[2].files[0]: invalid type`) rather than the thing the user wrote,
//! and a citation config is edited by hand in a YAML front-matter block where a
//! precise message is worth more than the brevity of a derive.

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySequence, PyString};

use autocitefetch::CslValue;

/// Convert a Python object into a [`CslValue`].
pub fn to_csl(obj: &Bound<'_, PyAny>) -> PyResult<CslValue> {
    pythonize::depythonize(obj).map_err(|e| PyTypeError::new_err(format!("not CSL-JSON data: {e}")))
}

/// Convert a [`CslValue`] into a Python object.
pub fn from_csl<'py>(py: Python<'py>, value: &CslValue) -> PyResult<Bound<'py, PyAny>> {
    pythonize::pythonize(py, value)
        .map_err(|e| PyValueError::new_err(format!("cannot represent CSL-JSON in Python: {e}")))
}

/// Look up `key` in `spec`, or `None` if it is absent *or* explicitly `None`.
///
/// Treating an explicit `None` as absence is what lets the Python layer build
/// spec dicts unconditionally (`{"files": cfg.get("files")}`) instead of
/// assembling them key by key.
pub fn opt<'py>(spec: &Bound<'py, PyDict>, key: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    match spec.get_item(key)? {
        Some(v) if !v.is_none() => Ok(Some(v)),
        _ => Ok(None),
    }
}

/// A required string entry.
pub fn get_str(spec: &Bound<'_, PyDict>, key: &str, what: &str) -> PyResult<String> {
    match opt(spec, key)? {
        Some(v) => v
            .extract::<String>()
            .map_err(|_| PyTypeError::new_err(format!("{what}: `{key}` must be a string"))),
        None => Err(PyValueError::new_err(format!("{what}: missing `{key}`"))),
    }
}

/// An optional string entry.
pub fn get_opt_str(spec: &Bound<'_, PyDict>, key: &str, what: &str) -> PyResult<Option<String>> {
    match opt(spec, key)? {
        Some(v) => v
            .extract::<String>()
            .map(Some)
            .map_err(|_| PyTypeError::new_err(format!("{what}: `{key}` must be a string"))),
        None => Ok(None),
    }
}

/// An optional list-of-strings entry.
///
/// A bare string is accepted as a one-element list: `bibliography: refs.yaml`
/// is at least as common in a front-matter block as the list form, and the
/// Python feature has always accepted both.
pub fn get_str_list(spec: &Bound<'_, PyDict>, key: &str, what: &str) -> PyResult<Vec<String>> {
    let Some(v) = opt(spec, key)? else {
        return Ok(Vec::new());
    };
    if let Ok(s) = v.cast::<PyString>() {
        return Ok(vec![s.extract()?]);
    }
    let seq = v
        .cast::<PySequence>()
        .map_err(|_| PyTypeError::new_err(format!("{what}: `{key}` must be a list of strings")))?;
    let mut out = Vec::new();
    for item in seq.try_iter()? {
        out.push(item?.extract::<String>().map_err(|_| {
            PyTypeError::new_err(format!("{what}: every entry of `{key}` must be a string"))
        })?);
    }
    Ok(out)
}

/// An optional unsigned-integer entry.
pub fn get_opt_usize(spec: &Bound<'_, PyDict>, key: &str, what: &str) -> PyResult<Option<usize>> {
    match opt(spec, key)? {
        Some(v) => v.extract::<usize>().map(Some).map_err(|_| {
            PyTypeError::new_err(format!("{what}: `{key}` must be a non-negative integer"))
        }),
        None => Ok(None),
    }
}

/// An optional float entry (accepts an int too, as YAML happily produces one).
pub fn get_opt_f64(spec: &Bound<'_, PyDict>, key: &str, what: &str) -> PyResult<Option<f64>> {
    match opt(spec, key)? {
        Some(v) => v
            .extract::<f64>()
            .map(Some)
            .map_err(|_| PyTypeError::new_err(format!("{what}: `{key}` must be a number"))),
        None => Ok(None),
    }
}

/// An optional sub-dictionary.
pub fn get_opt_dict<'py>(
    spec: &Bound<'py, PyDict>,
    key: &str,
    what: &str,
) -> PyResult<Option<Bound<'py, PyDict>>> {
    match opt(spec, key)? {
        Some(v) => v
            .cast_into::<PyDict>()
            .map(Some)
            .map_err(|_| PyTypeError::new_err(format!("{what}: `{key}` must be a mapping"))),
        None => Ok(None),
    }
}

/// An optional list of dictionaries.
pub fn get_dict_list<'py>(
    spec: &Bound<'py, PyDict>,
    key: &str,
    what: &str,
) -> PyResult<Vec<Bound<'py, PyDict>>> {
    let Some(v) = opt(spec, key)? else {
        return Ok(Vec::new());
    };
    let list = v
        .cast::<PyList>()
        .map_err(|_| PyTypeError::new_err(format!("{what}: `{key}` must be a list")))?;
    let mut out = Vec::with_capacity(list.len());
    for item in list.iter() {
        out.push(
            item.cast_into::<PyDict>()
                .map_err(|_| PyTypeError::new_err(format!("{what}: `{key}` must hold mappings")))?,
        );
    }
    Ok(out)
}

/// Reject spec keys this build does not understand.
///
/// A typo in a hand-edited YAML config would otherwise be silently ignored and
/// present as "the option did nothing" — the hardest kind of config bug to
/// track down. The Python layer translates and validates the *user-facing*
/// option names, so anything unknown arriving here is a bug in that layer or a
/// version mismatch between the two halves of the package; either way, say so.
pub fn reject_unknown_keys(
    spec: &Bound<'_, PyDict>,
    allowed: &[&str],
    what: &str,
) -> PyResult<()> {
    let mut unknown: Vec<String> = Vec::new();
    for key in spec.keys() {
        let key: String = key.extract()?;
        if !allowed.contains(&key.as_str()) {
            unknown.push(key);
        }
    }
    if unknown.is_empty() {
        return Ok(());
    }
    unknown.sort();
    Err(PyValueError::new_err(format!(
        "{what}: unknown option(s) {}; expected one of {}",
        unknown.join(", "),
        allowed.join(", "),
    )))
}
