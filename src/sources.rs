//! Turning source-spec dictionaries into registered sources.
//!
//! A spec is one dict per citation prefix, e.g.
//!
//! ```python
//! {"kind": "arxiv", "prefix": "arxiv", "chain_dois_to": "doi"}
//! {"kind": "bib",   "prefix": "b", "files": ["/abs/path/refs.yaml"]}
//! ```
//!
//! Two properties of the library shape this module.
//!
//! **A source owns no prefix.** `(prefix, source)` is a binding the manager
//! holds, made by `register(prefix, source)`, so `kind` and `prefix` are
//! genuinely independent: registering `BibliographyFileSource` twice under two
//! prefixes over two file sets is an ordinary thing to do, and it is exactly
//! what this package's `cite_prefix:` config option has always meant.
//!
//! **A chain target is configuration.** `ArxivSource::chain_dois_to` names the
//! prefix an entry's DOI is delegated to; nothing guarantees a source was
//! registered under that name. The library treats a dangling target as a
//! per-citation failure at retrieval time — correct for a library, but for a
//! config file read once at startup it is much better to say so immediately,
//! which is what [`check_chain_targets`] does with `Source::chains_to`.

use std::collections::HashSet;
use std::time::Duration;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use autocitefetch::source::{
    ArxivSource, BibliographyFileSource, DoiSource, ManualSource, Resolution, RetrieveCtx, Source,
};
use autocitefetch::{BoxFuture, CslValue};

use crate::convert::{
    get_opt_dict, get_opt_str, get_opt_usize, get_str, get_str_list, opt, reject_unknown_keys,
    to_csl,
};
use crate::formats::{self, Format};

/// One `(prefix, source)` binding.
pub struct Registration {
    pub prefix: String,
    pub source: BoxedSource,
}

/// A `Box<dyn Source>` that is itself a [`Source`].
///
/// The four built-ins are different types, so building them in a `match` means
/// boxing — but `CitationManager::register` takes `impl Source + 'static`, and
/// `Box<dyn Source>` does not implement the trait it points at. Delegating
/// through a newtype is the whole fix. (An upstream
/// `impl<S: Source + ?Sized> Source for Box<S>` — the shape `report.rs` already
/// uses for `Reporter`/`Rc` — would make this file unnecessary; until it
/// exists, forward here.) Every method is forwarded, including the ones with
/// defaults: forgetting one would silently substitute the trait default and,
/// for `normalize_key`, quietly lowercase a manual citation's text or an
/// old-style arXiv id's subject class.
pub struct BoxedSource(Box<dyn Source>);

impl Source for BoxedSource {
    fn chunk_size(&self) -> usize {
        self.0.chunk_size()
    }

    fn min_interval(&self) -> Duration {
        self.0.min_interval()
    }

    fn default_ttl(&self) -> Duration {
        self.0.default_ttl()
    }

    fn normalize_key(&self, key: &str) -> String {
        self.0.normalize_key(key)
    }

    fn chains_to(&self) -> Vec<&str> {
        self.0.chains_to()
    }

    fn retrieve_chunk<'a>(
        &'a self,
        keys: Vec<String>,
        ctx: &'a RetrieveCtx<'a>,
    ) -> BoxFuture<'a, Vec<Resolution>> {
        self.0.retrieve_chunk(keys, ctx)
    }
}

/// Build one registration from a spec dict.
pub fn build(spec: &Bound<'_, PyDict>) -> PyResult<Registration> {
    let kind = get_str(spec, "kind", "citation source")?;
    let prefix = get_str(spec, "prefix", &format!("`{kind}` citation source"))?;
    let what = format!("`{prefix}:` citation source");

    // `register` rejects these too, but only once every source is built; saying
    // it here names the offending spec instead of a bare prefix string.
    if prefix.is_empty() || prefix.contains(':') {
        return Err(PyValueError::new_err(format!(
            "{what}: a citation prefix must be non-empty and must not contain `:` \
             (citation ids are `prefix:key`, split at the first colon)"
        )));
    }

    // Boxed uniformly, so the chain targets can be read back off the trait
    // (`Source::chains_to`) rather than each arm reporting its own. A new
    // chaining source kind is then covered by `check_chain_targets` for free.
    let source: Box<dyn Source> = match kind.as_str() {
        "arxiv" => {
            reject_unknown_keys(
                spec,
                &[
                    "kind",
                    "prefix",
                    "chain_dois_to",
                    "override_dois",
                    "override_dois_file",
                ],
                &what,
            )?;
            Box::new(build_arxiv(spec, &what)?)
        }
        "doi" => {
            reject_unknown_keys(spec, &["kind", "prefix"], &what)?;
            Box::new(DoiSource::new())
        }
        "manual" => {
            reject_unknown_keys(spec, &["kind", "prefix", "format"], &what)?;
            // The format name is the inner key of the emitted
            // `{"_ready_formatted": {<format>: <text>}}`. It is required rather
            // than defaulted: the library cannot know what markup its host
            // renders, and a default here would be a second place for the
            // Python layer's `manual_format` to disagree with.
            let format = get_str(spec, "format", &what)?;
            Box::new(ManualSource::new(format))
        }
        "bib" => {
            reject_unknown_keys(
                spec,
                &["kind", "prefix", "files", "entries", "format", "ttl_seconds"],
                &what,
            )?;
            Box::new(build_bib(spec, &what)?)
        }
        other => {
            return Err(PyValueError::new_err(format!(
                "unknown citation source kind `{other}`; expected arxiv, doi, manual or bib"
            )));
        }
    };

    Ok(Registration {
        prefix,
        source: BoxedSource(source),
    })
}

fn build_arxiv(spec: &Bound<'_, PyDict>, what: &str) -> PyResult<ArxivSource> {
    let mut source = ArxivSource::new();

    // Absent means "keep the default (`doi`)"; an explicit `None` means "do not
    // chain at all" — the old `chain_to_doi: false`. `opt()` collapses the two,
    // so consult the dict directly here.
    if let Some(value) = spec.get_item("chain_dois_to")? {
        if value.is_none() {
            source = source.chain_dois_to(None);
        } else {
            let prefix: String = value.extract().map_err(|_| {
                PyValueError::new_err(format!(
                    "{what}: `chain_dois_to` must be a citation prefix string, or None to \
                     disable DOI chaining"
                ))
            })?;
            source = source.chain_dois_to(Some(&prefix));
        }
    }

    // A file is read and parsed *here*, then passed as data, so it may be YAML
    // — the library's own `with_override_dois_file` is JSON-only.
    if let Some(path) = get_opt_str(spec, "override_dois_file", what)? {
        let value = formats::read_file(&path, Format::Auto).map_err(PyValueError::new_err)?;
        let overrides = formats::doi_overrides_from(&value, &path).map_err(PyValueError::new_err)?;
        source = source.with_override_dois(overrides);
    }

    // Inline overrides are applied second so they win over the file, matching
    // the library's own documented precedence.
    if let Some(map) = opt(spec, "override_dois")? {
        let value = to_csl(&map)?;
        let overrides = formats::doi_overrides_from(&value, &format!("{what}: `override_dois`"))
            .map_err(PyValueError::new_err)?;
        source = source.with_override_dois(overrides);
    }

    Ok(source)
}

fn build_bib(spec: &Bound<'_, PyDict>, what: &str) -> PyResult<BibliographyFileSource> {
    let files = get_str_list(spec, "files", what)?;
    let entries = get_opt_dict(spec, "entries", what)?;

    // The two constructors are mutually exclusive in the library; saying so is
    // better than silently letting one win.
    if entries.is_some() && !files.is_empty() {
        return Err(PyValueError::new_err(format!(
            "{what}: give either `files` or `entries`, not both"
        )));
    }

    let mut source = match entries {
        Some(entries) => {
            let mut pairs: Vec<(String, CslValue)> = Vec::with_capacity(entries.len());
            for (key, value) in entries.iter() {
                pairs.push((key.extract()?, to_csl(&value)?));
            }
            BibliographyFileSource::from_entries(pairs)
        }
        None => {
            let format = match get_opt_str(spec, "format", what)? {
                Some(name) => Format::parse(&name)
                    .map_err(|e| PyValueError::new_err(format!("{what}: {e}")))?,
                None => Format::Auto,
            };
            BibliographyFileSource::new(files).with_parser(formats::parser_for(format))
        }
    };

    if let Some(secs) = get_opt_usize(spec, "ttl_seconds", what)? {
        source = source.with_ttl(Duration::from_secs(secs as u64));
    }

    Ok(source)
}

/// Reject a chain target with no source behind it.
///
/// The realistic mistake this catches: renaming the DOI source's prefix
/// (`cite_prefix: dx`) without updating arXiv's `chain_dois_to`, which would
/// otherwise turn every chained arXiv citation into a retrieval failure long
/// after the config was read.
pub fn check_chain_targets(registrations: &[Registration]) -> PyResult<()> {
    let known: Vec<&str> = registrations.iter().map(|r| r.prefix.as_str()).collect();
    for reg in registrations {
        for target in reg.source.chains_to() {
            if !known.contains(&target) {
                return Err(PyValueError::new_err(format!(
                    "the `{}:` citation source chains to `{target}:`, but no citation source is \
                     registered under that prefix (registered: {})",
                    reg.prefix,
                    known.join(", "),
                )));
            }
        }
    }
    Ok(())
}

/// Reject two sources claiming one prefix.
///
/// `register` would silently let the later one replace the earlier — reasonable
/// for a library whose caller is code, wrong for a config file where it means
/// one of the two blocks the user wrote is doing nothing.
pub fn check_unique_prefixes(registrations: &[Registration]) -> PyResult<()> {
    let mut seen: HashSet<&str> = HashSet::new();
    let mut dups: Vec<&str> = registrations
        .iter()
        .map(|r| r.prefix.as_str())
        .filter(|p| !seen.insert(p))
        .collect();
    if dups.is_empty() {
        return Ok(());
    }
    dups.sort_unstable();
    dups.dedup();
    Err(PyValueError::new_err(format!(
        "more than one citation source registered under the prefix(es) {}; \
         each prefix needs its own",
        dups.join(", ")
    )))
}
