//! JSON/YAML parsing for bibliography files and arXiv DOI-override maps.
//!
//! This is where autocitefetch's *host-parses-config* principle lands. The
//! `no_std` core depends on no format crate but `serde_json`, and both hooks it
//! offers for anything else are used here:
//!
//! * [`BibliographyFileSource::with_parser`] takes a bytes → [`CslValue`]
//!   function, so YAML costs the library nothing — a bib file is still fetched
//!   through the one `Fetcher` choke point (including as an `http(s):` URL),
//!   only the parse step changes. This matters for us: FLM front matter has
//!   always pointed `bibliography:` at CSL-YAML files.
//! * arXiv DOI overrides are handed over as *data*
//!   ([`ArxivSource::with_override_dois`]) rather than as a file path, so the
//!   override file may be YAML even though the library's built-in
//!   `with_override_dois_file` convenience only knows JSON.
//!
//! Lifted almost verbatim from `autocitefetch-cli/src/formats.rs`; kept in sync
//! with it by hand rather than shared, since the CLI crate is a binary.
//!
//! [`BibliographyFileSource::with_parser`]: autocitefetch::source::BibliographyFileSource::with_parser
//! [`ArxivSource::with_override_dois`]: autocitefetch::source::ArxivSource::with_override_dois

use autocitefetch::CslValue;
use autocitefetch::source::BibParser;

/// Which parser a bibliography file should be read with.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub enum Format {
    /// Try JSON, fall back to YAML.
    #[default]
    Auto,
    Json,
    Yaml,
}

impl Format {
    /// Parse a format name from a config file.
    pub fn parse(name: &str) -> Result<Self, String> {
        match name {
            "auto" => Ok(Format::Auto),
            "json" => Ok(Format::Json),
            "yaml" | "yml" => Ok(Format::Yaml),
            other => Err(format!(
                "unknown bibliography format `{other}`; expected auto, json or yaml"
            )),
        }
    }
}

fn parse_json(bytes: &[u8]) -> Result<CslValue, String> {
    serde_json::from_slice(bytes).map_err(|e| format!("invalid JSON: {e}"))
}

fn parse_yaml(bytes: &[u8]) -> Result<CslValue, String> {
    serde_yaml_ng::from_slice(bytes).map_err(|e| format!("invalid YAML: {e}"))
}

/// Parse as JSON, falling back to YAML.
///
/// YAML 1.2 is very nearly a JSON superset, so running only the YAML parser
/// would usually work — but "very nearly" is not "exactly" (an integer literal
/// too large for the YAML reader's number type is valid JSON and rejected as
/// out of range), and its diagnostics for a *broken* JSON file are much worse
/// than serde_json's. So try the exact parser first, and report both failures:
/// a bibliography location may be a URL with no extension to guess from, and
/// hiding one parser's complaint would be worse than showing two.
fn parse_auto(bytes: &[u8]) -> Result<CslValue, String> {
    match parse_json(bytes) {
        Ok(v) => Ok(v),
        Err(json_err) => parse_yaml(bytes)
            .map_err(|yaml_err| format!("not parseable as JSON or YAML — {json_err}; {yaml_err}")),
    }
}

/// The [`BibParser`] implementing `format`.
pub fn parser_for(format: Format) -> BibParser {
    match format {
        Format::Auto => parse_auto,
        Format::Json => parse_json,
        Format::Yaml => parse_yaml,
    }
}

/// Turn a parsed arXiv-id → DOI mapping into the `(arxivid, Option<doi>)` pairs
/// [`ArxivSource::with_override_dois`] takes.
///
/// A string value overrides whatever DOI the arXiv feed reports; a `null` value
/// *suppresses* the DOI entirely (keep the arXiv metadata, do not chain), which
/// is why the value type is `Option<String>` rather than `String`.
///
/// [`ArxivSource::with_override_dois`]: autocitefetch::source::ArxivSource::with_override_dois
pub fn doi_overrides_from(
    value: &CslValue,
    what: &str,
) -> Result<Vec<(String, Option<String>)>, String> {
    let map = value
        .as_object()
        .ok_or_else(|| format!("{what}: expected a mapping of arXiv id to DOI (or to null)"))?;

    let mut out = Vec::with_capacity(map.len());
    for (arxivid, doi) in map {
        let doi = match doi {
            CslValue::String(s) => Some(s.clone()),
            CslValue::Null => None,
            other => {
                return Err(format!(
                    "{what}: the override for `{arxivid}` must be a DOI string or null, not {}",
                    type_name(other)
                ));
            }
        };
        out.push((arxivid.clone(), doi));
    }
    Ok(out)
}

/// Read and parse a local file, tagging any error with its path.
pub fn read_file(path: &str, format: Format) -> Result<CslValue, String> {
    let bytes = std::fs::read(path).map_err(|e| format!("{path}: {e}"))?;
    parser_for(format)(&bytes).map_err(|e| format!("{path}: {e}"))
}

/// A human-readable name for a JSON value's type, for error messages.
fn type_name(v: &CslValue) -> &'static str {
    match v {
        CslValue::Null => "null",
        CslValue::Bool(_) => "a boolean",
        CslValue::Number(_) => "a number",
        CslValue::String(_) => "a string",
        CslValue::Array(_) => "an array",
        CslValue::Object(_) => "an object",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn auto_accepts_json_and_yaml() {
        let json = br#"{"a": {"title": "T"}}"#;
        let yaml = b"a:\n  title: T\n";
        assert_eq!(parse_auto(json).unwrap(), parse_auto(yaml).unwrap());
    }

    #[test]
    fn auto_accepts_json_the_yaml_reader_rejects() {
        // Why `auto` tries JSON first instead of leaning on YAML being a JSON
        // superset: it isn't quite one. An integer literal past the YAML
        // reader's range is valid JSON and an error there.
        let json = br#"{"a": 123456789012345678901234567890}"#;
        assert!(parse_yaml(json).is_err());
        assert!(parse_auto(json).is_ok());
    }

    #[test]
    fn auto_reports_both_parsers_on_garbage() {
        let err = parse_auto(b"not: [valid").unwrap_err();
        assert!(err.contains("JSON"), "{err}");
        assert!(err.contains("YAML"), "{err}");
    }

    #[test]
    fn overrides_map_string_to_some_and_null_to_none() {
        let value = parse_auto(b"1211.1037: 10.1103/PhysRevA.86.052329\n0704.0001: ~\n").unwrap();
        let mut got = doi_overrides_from(&value, "overrides").unwrap();
        got.sort();
        assert_eq!(
            got,
            vec![
                ("0704.0001".to_string(), None),
                (
                    "1211.1037".to_string(),
                    Some("10.1103/PhysRevA.86.052329".to_string())
                ),
            ]
        );
    }

    #[test]
    fn overrides_reject_a_non_string_value() {
        let value = parse_json(br#"{"1211.1037": 42}"#).unwrap();
        let err = doi_overrides_from(&value, "overrides").unwrap_err();
        assert!(err.contains("must be a DOI string or null"), "{err}");
    }

    #[test]
    fn format_names_are_parsed_and_bad_ones_named() {
        assert_eq!(Format::parse("yml").unwrap(), Format::Yaml);
        assert!(Format::parse("bibtex").unwrap_err().contains("bibtex"));
    }
}
