//! Shared Linux runtime for Worklane's native frontends.
pub mod cli;
pub mod config;
pub mod harness;
pub mod process;
pub mod state;
pub mod transport;

use serde_json::Value;
use std::{
    fmt,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
};

#[derive(Debug, Clone)]
pub struct Error {
    pub kind: &'static str,
    pub message: String,
    pub retry_at: Option<f64>,
    pub deferred: bool,
}
impl Error {
    pub fn new(kind: &'static str, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            retry_at: None,
            deferred: false,
        }
    }
    pub fn quota(message: impl Into<String>, retry_at: f64) -> Self {
        Self {
            kind: "quota",
            message: message.into(),
            retry_at: Some(retry_at),
            deferred: false,
        }
    }
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for Error {}
impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Self::new("io", e.to_string())
    }
}
impl From<rusqlite::Error> for Error {
    fn from(e: rusqlite::Error) -> Self {
        Self::new("sqlite", e.to_string())
    }
}
impl From<serde_json::Error> for Error {
    fn from(e: serde_json::Error) -> Self {
        Self::new("validation", e.to_string())
    }
}
pub type Result<T> = std::result::Result<T, Error>;
pub fn fail<T>(message: impl Into<String>) -> Result<T> {
    Err(Error::new("blocked", message))
}
pub fn string(v: &Value, key: &str) -> String {
    v[key].as_str().unwrap_or_default().to_owned()
}
pub fn role(stage: &str) -> Result<&'static str> {
    match stage {
        "build" => Ok("worker"),
        "review" => Ok("reviewer"),
        "finalize" => Ok("finalizer"),
        _ => fail(format!("Unknown stage: {stage}")),
    }
}

pub trait Clock: Send + Sync {
    fn now(&self) -> String;
}
pub trait IdSource: Send + Sync {
    fn next(&self) -> String;
}
struct SystemClock;
impl Clock for SystemClock {
    fn now(&self) -> String {
        chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
    }
}
struct SystemIds;
impl IdSource for SystemIds {
    fn next(&self) -> String {
        uuid::Uuid::new_v4().to_string()
    }
}
pub struct FixedClock(pub String);
impl Clock for FixedClock {
    fn now(&self) -> String {
        self.0.clone()
    }
}
#[derive(Default)]
pub struct SequentialIds(pub AtomicU64);
impl IdSource for SequentialIds {
    fn next(&self) -> String {
        uuid::Uuid::from_u128(self.0.fetch_add(1, Ordering::Relaxed) as u128 + 1).to_string()
    }
}
#[derive(Clone)]
pub struct Dependencies {
    pub clock: Arc<dyn Clock>,
    pub ids: Arc<dyn IdSource>,
}
impl Default for Dependencies {
    fn default() -> Self {
        Self {
            clock: Arc::new(SystemClock),
            ids: Arc::new(SystemIds),
        }
    }
}
pub mod actions;
pub mod api;
pub mod board;
pub mod controller;
mod queries;
pub mod read_model;
pub mod tui;
mod unicode;
pub mod workflow;
