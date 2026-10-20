use crate::{Error, Result};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::Write,
    path::Path,
};

pub struct ConfigStore;
impl ConfigStore {
    pub fn defaults() -> Value {
        json!({"harness":"codex","workers":3,"model":"","effort":"high","interval":60,"project":"","stack_dependencies":[],"completion_gates":[],"worker_harness":"","worker_model":"","reviewer_harness":"","reviewer_model":"","finalizer_harness":"","finalizer_model":""})
    }
    pub fn validate(values: &Value) -> Result<Value> {
        let invalid = |m: String| Error::new("validation", m);
        let mut config = Self::defaults();
        let values = values
            .as_object()
            .ok_or_else(|| invalid("Configuration must be a table.".into()))?;
        let mut unknown: Vec<_> = values
            .keys()
            .filter(|k| config.get(k.as_str()).is_none())
            .cloned()
            .collect();
        unknown.sort();
        if !unknown.is_empty() {
            return Err(invalid(format!(
                "Unknown configuration setting: {}",
                unknown.join(", ")
            )));
        }
        for (k, v) in values {
            config[k] = v.clone();
        }
        if !matches!(config["harness"].as_str(), Some("codex" | "opencode")) {
            return Err(invalid("harness must be codex or opencode.".into()));
        }
        for role in ["worker", "reviewer", "finalizer"] {
            if !matches!(
                config[format!("{role}_harness")].as_str(),
                Some("" | "codex" | "opencode")
            ) {
                return Err(invalid(format!(
                    "{role}_harness must be codex, opencode or empty to inherit."
                )));
            }
        }
        if !config["workers"]
            .as_i64()
            .is_some_and(|n| (1..=16).contains(&n))
        {
            return Err(invalid(
                "workers must be an integer between 1 and 16.".into(),
            ));
        }
        for key in ["model", "worker_model", "reviewer_model", "finalizer_model"] {
            if config[key]
                .as_str()
                .is_none_or(|s| s.chars().any(crate::unicode::is_space))
            {
                return Err(invalid(format!(
                    "{key} must be a model name without whitespace, or empty for the harness default."
                )));
            }
        }
        if !matches!(
            config["effort"].as_str(),
            Some("low" | "medium" | "high" | "xhigh")
        ) {
            return Err(invalid("effort must be low, medium, high or xhigh.".into()));
        }
        if config["interval"].as_i64().is_none_or(|n| n < 10) {
            return Err(invalid(
                "interval must be an integer of at least 10 seconds.".into(),
            ));
        }
        let project = regex::Regex::new(r"^[\w-]+/[1-9]\d*$").unwrap();
        if !config["project"]
            .as_str()
            .is_some_and(|s| s.is_empty() || project.is_match(s))
        {
            return Err(invalid(
                "project must be OWNER/NUMBER, or empty for automatic discovery.".into(),
            ));
        }
        let edge_re = regex::Regex::new(r"^[1-9]\d*:[1-9]\d*$").unwrap();
        let edges = config["stack_dependencies"]
            .as_array()
            .filter(|a| {
                a.iter()
                    .all(|e| e.as_str().is_some_and(|s| edge_re.is_match(s)))
            })
            .ok_or_else(|| {
                invalid("stack_dependencies must list CHILD:PARENT issue numbers.".into())
            })?;
        let gates = config["completion_gates"]
            .as_array()
            .filter(|a| a.iter().all(|e| e.as_i64().is_some_and(|n| n > 0)))
            .ok_or_else(|| invalid("completion_gates must list positive issue numbers.".into()))?;
        let mut graph: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for edge in edges {
            let (c, p) = edge.as_str().unwrap().split_once(':').unwrap();
            if gates.iter().any(|n| n == p) {
                return Err(invalid(format!(
                    "Completion gate #{p} cannot be stackable."
                )));
            }
            graph.entry(c.into()).or_default().push(p.into());
        }
        fn visit(
            node: &str,
            graph: &BTreeMap<String, Vec<String>>,
            visiting: &mut BTreeSet<String>,
            done: &mut BTreeSet<String>,
        ) -> bool {
            if done.contains(node) {
                return true;
            }
            if !visiting.insert(node.into()) {
                return false;
            }
            if let Some(parents) = graph.get(node) {
                for p in parents {
                    if !visit(p, graph, visiting, done) {
                        return false;
                    }
                }
            }
            visiting.remove(node);
            done.insert(node.into());
            true
        }
        let mut done = BTreeSet::new();
        for child in graph.keys() {
            if !visit(child, &graph, &mut BTreeSet::new(), &mut done) {
                return Err(invalid("stack_dependencies contains a cycle.".into()));
            }
        }
        Ok(config)
    }
    pub fn resolve(checkout: &Path, overrides: &Value) -> Result<Value> {
        let path = checkout.join(".worklane.toml");
        let mut values = match fs::read_to_string(&path) {
            Ok(s) => {
                let table: toml::Value = toml::from_str(&s)
                    .map_err(|e| Error::new("validation", format!("{}: {e}", path.display())))?;
                serde_json::to_value(table)?
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => json!({}),
            Err(e) => return Err(e.into()),
        };
        // Validate the file before applying overrides: malformed saved configuration never resets silently.
        Self::validate(&values)?;
        for (k, v) in overrides
            .as_object()
            .ok_or_else(|| Error::new("validation", "Configuration overrides must be a table."))?
        {
            values[k] = v.clone();
        }
        Self::validate(&values)
    }
    pub fn save(checkout: &Path, values: &Value) -> Result<Value> {
        let config = Self::validate(values)?;
        let mut file = tempfile::Builder::new()
            .prefix(".worklane-")
            .tempfile_in(checkout)?;
        let table: toml::Value = serde_json::from_value(config.clone())?;
        file.write_all(
            toml::to_string(&table)
                .map_err(|e| Error::new("validation", e.to_string()))?
                .as_bytes(),
        )?;
        file.as_file().sync_all()?;
        file.persist(checkout.join(".worklane.toml"))
            .map_err(|e| e.error)?;
        fs::File::open(checkout)?.sync_all()?;
        Ok(config)
    }
}
