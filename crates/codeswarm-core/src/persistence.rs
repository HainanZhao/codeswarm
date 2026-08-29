//! Versioned persistence for the Rust client.
//!
//! The first Rust implementation wrote bare [`AgentEvent`] values to JSONL.
//! The types in this module deliberately accept that format as schema zero and
//! provide migration helpers for versioned envelopes.  Session
//! metadata is likewise accepted in the shape written by the Python client.
//! Metadata is exported in a flattened form so the existing Python decoder can
//! continue to find keys such as `roster` and `agent_data`.

use std::collections::BTreeSet;
use std::fmt::{Display, Formatter};
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::ops::Deref;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

use crate::AgentEvent;

/// The current on-disk schema for Rust persistence.
pub const CURRENT_SCHEMA_VERSION: u32 = 1;
const LEGACY_SCHEMA_VERSION: u32 = 0;

/// Persistence operations report malformed input, unsupported versions, and
/// I/O failures through this type.
#[derive(Debug)]
pub enum PersistenceError {
    Io(std::io::Error),
    Malformed { kind: &'static str, detail: String },
    UnsupportedVersion { kind: &'static str, version: u32 },
}

impl Display for PersistenceError {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "persistence I/O error: {error}"),
            Self::Malformed { kind, detail } => write!(formatter, "malformed {kind}: {detail}"),
            Self::UnsupportedVersion { kind, version } => {
                write!(formatter, "unsupported {kind} schema version {version}")
            }
        }
    }
}

impl std::error::Error for PersistenceError {}

impl From<std::io::Error> for PersistenceError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

/// Result of converting a file to the current schema.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MigrationReport {
    /// `None` means that no source record/version was observed, including when
    /// the source file is missing or empty.
    pub source_version: Option<u32>,
    pub target_version: u32,
    pub records: usize,
    pub changed: bool,
}

/// Events read from a versioned log, including the source versions observed.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LoadedEvents {
    pub events: Vec<AgentEvent>,
    pub source_versions: BTreeSet<u32>,
}

/// A JSONL event log that accepts legacy bare events and version-zero envelopes.
#[derive(Clone, Debug)]
pub struct VersionedEventLog {
    path: PathBuf,
}

impl VersionedEventLog {
    pub fn open(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Append one current-schema envelope.
    pub fn append(&self, event: &AgentEvent) -> Result<(), PersistenceError> {
        let record = serde_json::json!({
            "schema_version": CURRENT_SCHEMA_VERSION,
            "event": event,
        });
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        serde_json::to_writer(&mut file, &record)
            .map_err(|error| malformed("event log", error.to_string()))?;
        file.write_all(b"\n")?;
        file.sync_data()?;
        Ok(())
    }

    /// Read all events. Missing logs are an empty event stream.
    pub fn read(&self) -> Result<Vec<AgentEvent>, PersistenceError> {
        Ok(self.read_with_versions()?.events)
    }

    pub fn read_with_versions(&self) -> Result<LoadedEvents, PersistenceError> {
        let file = match File::open(&self.path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Ok(LoadedEvents {
                    events: Vec::new(),
                    source_versions: BTreeSet::new(),
                });
            }
            Err(error) => return Err(error.into()),
        };
        let mut events = Vec::new();
        let mut source_versions = BTreeSet::new();
        for (line_number, result) in BufReader::new(file).lines().enumerate() {
            let line = result?;
            if line.trim().is_empty() {
                continue;
            }
            let (version, event) = parse_event_record(&line, line_number + 1)?;
            source_versions.insert(version);
            events.push(event);
        }
        Ok(LoadedEvents {
            events,
            source_versions,
        })
    }

    /// Rewrite legacy records atomically. A malformed record leaves the source
    /// untouched, allowing an operator to repair the bad log manually.
    pub fn migrate_in_place(&self) -> Result<MigrationReport, PersistenceError> {
        let loaded = self.read_with_versions()?;
        if loaded.events.is_empty() && !self.path.exists() {
            return Ok(MigrationReport {
                source_version: None,
                target_version: CURRENT_SCHEMA_VERSION,
                records: 0,
                changed: false,
            });
        }
        let source_version = loaded.source_versions.iter().copied().min();
        let changed = loaded
            .source_versions
            .iter()
            .any(|version| *version != CURRENT_SCHEMA_VERSION);
        if changed {
            atomic_write_event_log(&self.path, &loaded.events)?;
        }
        Ok(MigrationReport {
            source_version,
            target_version: CURRENT_SCHEMA_VERSION,
            records: loaded.events.len(),
            changed,
        })
    }
}

fn parse_event_record(
    line: &str,
    line_number: usize,
) -> Result<(u32, AgentEvent), PersistenceError> {
    let value: Value = serde_json::from_str(line)
        .map_err(|error| malformed("event log", format!("line {line_number}: {error}")))?;
    let object = value.as_object().ok_or_else(|| {
        malformed(
            "event log",
            format!("line {line_number} must be a JSON object"),
        )
    })?;
    let has_envelope = object.contains_key("event")
        || object.contains_key("schema_version")
        || object.contains_key("version");
    let version = if has_envelope {
        read_version(object, "event log", line_number)?
    } else {
        LEGACY_SCHEMA_VERSION
    };
    ensure_supported(version, "event log")?;
    let event_value = object.get("event").unwrap_or(&value);
    let event = serde_json::from_value(event_value.clone())
        .map_err(|error| malformed("event log", format!("line {line_number} event: {error}")))?;
    Ok((version, event))
}

fn atomic_write_event_log(path: &Path, events: &[AgentEvent]) -> Result<(), PersistenceError> {
    let temporary = temporary_path(path);
    let result = (|| {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        for event in events {
            let record = serde_json::json!({
                "schema_version": CURRENT_SCHEMA_VERSION,
                "event": event,
            });
            serde_json::to_writer(&mut file, &record)
                .map_err(|error| malformed("event log", error.to_string()))?;
            file.write_all(b"\n")?;
        }
        file.sync_all()?;
        fs::rename(&temporary, path)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

/// Metadata imported from either Python's plain object or the Rust envelope.
/// The map intentionally retains unknown keys for forward compatibility.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionMetadata {
    data: Map<String, Value>,
}

impl SessionMetadata {
    pub fn new(data: Map<String, Value>) -> Self {
        Self { data }
    }

    pub fn empty() -> Self {
        Self::new(Map::new())
    }

    pub fn schema_version(&self) -> u32 {
        CURRENT_SCHEMA_VERSION
    }

    pub fn get(&self, key: &str) -> Option<&Value> {
        self.data.get(key)
    }

    pub fn as_object(&self) -> &Map<String, Value> {
        &self.data
    }

    /// Flattened JSON is intentional: Python's `decode_session_meta` can read
    /// this directly and still locate `roster`/`agent_data`.
    pub fn to_value(&self) -> Value {
        let mut object = self.data.clone();
        object.insert("schema_version".into(), Value::from(CURRENT_SCHEMA_VERSION));
        Value::Object(object)
    }

    pub fn to_json(&self) -> Result<String, PersistenceError> {
        serde_json::to_string(&self.to_value())
            .map_err(|error| malformed("session metadata", error.to_string()))
    }
}

/// A loaded metadata value carries the version from which it was imported.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LoadedSessionMetadata {
    pub metadata: SessionMetadata,
    pub source_version: u32,
}

impl Deref for LoadedSessionMetadata {
    type Target = SessionMetadata;

    fn deref(&self) -> &Self::Target {
        &self.metadata
    }
}

/// File-backed session metadata with schema migration.
#[derive(Clone, Debug)]
pub struct SessionMetadataStore {
    path: PathBuf,
}

impl SessionMetadataStore {
    pub fn open(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn read(&self) -> Result<Option<LoadedSessionMetadata>, PersistenceError> {
        let raw = match fs::read_to_string(&self.path) {
            Ok(raw) => raw,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        let value: Value = serde_json::from_str(&raw)
            .map_err(|error| malformed("session metadata", error.to_string()))?;
        let object = value
            .as_object()
            .ok_or_else(|| malformed("session metadata", "expected a JSON object".into()))?;
        let source_version = read_version(object, "session metadata", 0)?;
        ensure_supported(source_version, "session metadata")?;
        let data = if let Some(metadata) = object.get("metadata") {
            metadata
                .as_object()
                .ok_or_else(|| {
                    malformed(
                        "session metadata",
                        "metadata envelope must be an object".into(),
                    )
                })?
                .clone()
        } else {
            object
                .iter()
                .filter(|(key, _)| key.as_str() != "schema_version" && key.as_str() != "version")
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect()
        };
        Ok(Some(LoadedSessionMetadata {
            metadata: SessionMetadata::new(data),
            source_version,
        }))
    }

    pub fn load(&self) -> Result<Option<LoadedSessionMetadata>, PersistenceError> {
        self.read()
    }

    pub fn write(&self, metadata: &SessionMetadata) -> Result<(), PersistenceError> {
        let json = metadata.to_json()?;
        fs::write(&self.path, format!("{json}\n"))?;
        Ok(())
    }

    pub fn migrate_in_place(&self) -> Result<MigrationReport, PersistenceError> {
        let Some(loaded) = self.read()? else {
            return Ok(MigrationReport {
                source_version: None,
                target_version: CURRENT_SCHEMA_VERSION,
                records: 0,
                changed: false,
            });
        };
        let changed = loaded.source_version != CURRENT_SCHEMA_VERSION
            || serde_json::from_str::<Value>(&fs::read_to_string(&self.path)?)
                .ok()
                .and_then(|value| {
                    value
                        .as_object()
                        .map(|object| object.contains_key("metadata"))
                })
                .unwrap_or(false);
        if changed {
            self.write(&loaded.metadata)?;
        }
        Ok(MigrationReport {
            source_version: Some(loaded.source_version),
            target_version: CURRENT_SCHEMA_VERSION,
            records: 1,
            changed,
        })
    }
}

fn read_version(
    object: &Map<String, Value>,
    kind: &'static str,
    line_number: usize,
) -> Result<u32, PersistenceError> {
    let schema = object
        .get("schema_version")
        .or_else(|| object.get("version"));
    let Some(schema) = schema else {
        return Ok(LEGACY_SCHEMA_VERSION);
    };
    let version = schema.as_u64().ok_or_else(|| {
        malformed(
            kind,
            if line_number == 0 {
                "schema_version must be an unsigned integer".into()
            } else {
                format!("line {line_number} schema_version must be an unsigned integer")
            },
        )
    })?;
    u32::try_from(version).map_err(|_| malformed(kind, "schema_version is too large".into()))
}

fn ensure_supported(version: u32, kind: &'static str) -> Result<(), PersistenceError> {
    if version > CURRENT_SCHEMA_VERSION {
        return Err(PersistenceError::UnsupportedVersion { kind, version });
    }
    Ok(())
}

fn malformed(kind: &'static str, detail: String) -> PersistenceError {
    PersistenceError::Malformed { kind, detail }
}

fn temporary_path(path: &Path) -> PathBuf {
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("data");
    path.with_file_name(format!(".{file_name}.migration-{}.tmp", std::process::id()))
}

/// Convert the Python session metadata blob to a Rust metadata value without
/// requiring the Python process or SQLite to be available.
pub fn import_python_session_metadata(
    value: Option<&str>,
) -> Result<Option<LoadedSessionMetadata>, PersistenceError> {
    let Some(value) = value else { return Ok(None) };
    let parsed: Value = serde_json::from_str(value)
        .map_err(|error| malformed("session metadata", error.to_string()))?;
    let object = parsed
        .as_object()
        .ok_or_else(|| malformed("session metadata", "expected a JSON object".into()))?;
    let source_version = read_version(object, "session metadata", 0)?;
    ensure_supported(source_version, "session metadata")?;
    let data = object
        .iter()
        .filter(|(key, _)| key.as_str() != "schema_version" && key.as_str() != "version")
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect();
    Ok(Some(LoadedSessionMetadata {
        metadata: SessionMetadata::new(data),
        source_version,
    }))
}

#[cfg(test)]
mod tests {
    use std::time::{SystemTime, UNIX_EPOCH};

    use serde_json::{Value, json};

    use super::{
        CURRENT_SCHEMA_VERSION, PersistenceError, SessionMetadata, SessionMetadataStore,
        VersionedEventLog, import_python_session_metadata,
    };
    use crate::AgentEvent;

    fn temp_path(suffix: &str) -> std::path::PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        std::env::temp_dir().join(format!("codeswarm-persistence-{unique}-{suffix}"))
    }

    fn event() -> AgentEvent {
        AgentEvent::Text {
            slot: 0,
            text: "hello".into(),
        }
    }

    #[test]
    fn missing_event_log_and_metadata_are_empty() {
        let event_log = VersionedEventLog::open(temp_path("events.jsonl"));
        assert!(event_log.read().expect("missing log").is_empty());
        let metadata = SessionMetadataStore::open(temp_path("metadata.json"));
        assert!(metadata.read().expect("missing metadata").is_none());
        assert!(
            !metadata
                .migrate_in_place()
                .expect("missing migration")
                .changed
        );
    }

    #[test]
    fn malformed_data_is_rejected_without_rewriting_source() {
        let event_path = temp_path("malformed-events.jsonl");
        std::fs::write(&event_path, "not-json\n").expect("write");
        let event_log = VersionedEventLog::open(&event_path);
        assert!(matches!(
            event_log.read(),
            Err(PersistenceError::Malformed { .. })
        ));
        assert_eq!(
            std::fs::read_to_string(&event_path).expect("read"),
            "not-json\n"
        );
        std::fs::remove_file(event_path).expect("cleanup");

        let metadata_path = temp_path("malformed-metadata.json");
        std::fs::write(&metadata_path, "[]").expect("write");
        let metadata = SessionMetadataStore::open(&metadata_path);
        assert!(matches!(
            metadata.read(),
            Err(PersistenceError::Malformed { .. })
        ));
        assert_eq!(std::fs::read_to_string(&metadata_path).expect("read"), "[]");
        std::fs::remove_file(metadata_path).expect("cleanup");
    }

    #[test]
    fn old_bare_event_log_migrates_to_current_envelope() {
        let path = temp_path("old-events.jsonl");
        std::fs::write(
            &path,
            serde_json::to_string(&event()).expect("event") + "\n",
        )
        .expect("write");
        let log = VersionedEventLog::open(&path);
        let report = log.migrate_in_place().expect("migrate");
        assert_eq!(report.source_version, Some(0));
        assert!(report.changed);
        assert_eq!(log.read().expect("read"), vec![event()]);
        let migrated = std::fs::read_to_string(&path).expect("read raw");
        assert!(migrated.contains(&format!("\"schema_version\":{CURRENT_SCHEMA_VERSION}")));
        std::fs::remove_file(path).expect("cleanup");
    }

    #[test]
    fn old_python_metadata_migrates_and_preserves_unknown_keys() {
        let path = temp_path("old-metadata.json");
        std::fs::write(
            &path,
            r#"{"roster":["openai.com"],"agent_data":{"name":"Codex"}}"#,
        )
        .expect("write");
        let store = SessionMetadataStore::open(&path);
        let loaded = store.read().expect("read").expect("metadata");
        assert_eq!(loaded.source_version, 0);
        assert_eq!(loaded.get("roster"), Some(&json!(["openai.com"])));
        let report = store.migrate_in_place().expect("migrate");
        assert!(report.changed);
        let migrated = std::fs::read_to_string(&path).expect("read");
        assert!(migrated.contains("\"roster\""));
        assert!(migrated.contains("\"schema_version\":1"));
        std::fs::remove_file(path).expect("cleanup");
    }

    #[test]
    fn current_event_and_metadata_data_is_not_rewritten() {
        let event_path = temp_path("current-events.jsonl");
        let log = VersionedEventLog::open(&event_path);
        log.append(&event()).expect("append");
        let before = std::fs::read_to_string(&event_path).expect("read");
        let report = log.migrate_in_place().expect("migrate");
        assert_eq!(report.source_version, Some(1));
        assert!(!report.changed);
        assert_eq!(std::fs::read_to_string(&event_path).expect("read"), before);
        std::fs::remove_file(event_path).expect("cleanup");

        let metadata_path = temp_path("current-metadata.json");
        let store = SessionMetadataStore::open(&metadata_path);
        let mut data = serde_json::Map::new();
        data.insert("roster".into(), json!(["agy"]));
        store.write(&SessionMetadata::new(data)).expect("write");
        let report = store.migrate_in_place().expect("migrate");
        assert_eq!(report.source_version, Some(1));
        assert!(!report.changed);
        std::fs::remove_file(metadata_path).expect("cleanup");
    }

    #[test]
    fn python_import_accepts_missing_and_current_metadata() {
        assert!(
            import_python_session_metadata(None)
                .expect("missing")
                .is_none()
        );
        let loaded = import_python_session_metadata(Some(
            r#"{"schema_version":1,"roster":["agy"],"agent_data":{"name":"Agy"}}"#,
        ))
        .expect("current")
        .expect("metadata");
        assert_eq!(loaded.source_version, 1);
        assert_eq!(
            loaded
                .get("agent_data")
                .and_then(Value::as_object)
                .and_then(|m| m.get("name"))
                .and_then(Value::as_str),
            Some("Agy")
        );
    }

    #[test]
    fn future_versions_are_rejected() {
        let event_path = temp_path("future-events.jsonl");
        std::fs::write(
            &event_path,
            serde_json::to_string(&json!({"schema_version": 99, "event": event()})).expect("json"),
        )
        .expect("write");
        assert!(matches!(
            VersionedEventLog::open(&event_path).read(),
            Err(PersistenceError::UnsupportedVersion { version: 99, .. })
        ));
        std::fs::remove_file(event_path).expect("cleanup");
    }
}
