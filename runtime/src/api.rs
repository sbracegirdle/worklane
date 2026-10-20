//! Stable shared domain values; extension fields remain lossless typed values.
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    Worker,
    Reviewer,
    Finalizer,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Stage {
    Build,
    Review,
    Finalize,
}
impl Stage {
    pub fn role(self) -> Role {
        match self {
            Self::Build => Role::Worker,
            Self::Review => Role::Reviewer,
            Self::Finalize => Role::Finalizer,
        }
    }
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Backend {
    Codex,
    Opencode,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Verdict {
    Pass,
    ChangesRequested,
    Blocked,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentResult {
    pub verdict: Verdict,
    pub pr_url: String,
    pub head_sha: String,
    pub summary: String,
    pub findings: Vec<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Task {
    pub number: i64,
    #[serde(default)]
    pub phase: String,
    #[serde(flatten)]
    pub fields: Map<String, Value>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Session {
    pub number: i64,
    pub role: Role,
    pub session_id: String,
    pub backend: Backend,
    pub model: String,
    pub worktree: std::path::PathBuf,
    pub created_at: String,
    pub updated_at: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct StackEdge {
    pub number: i64,
    pub pr_url: String,
    pub branch: String,
    pub base: String,
    pub head_sha: String,
    pub reviewed_head: String,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum RunStatus {
    Starting,
    Running,
    Completed,
    Failed,
    Interrupted,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Run {
    pub id: String,
    pub number: i64,
    pub role: Role,
    pub stage: Stage,
    pub session_id: Option<String>,
    pub status: RunStatus,
    pub started_at: String,
    pub finished_at: Option<String>,
    pub pid: Option<i32>,
    pub identity: Option<String>,
    pub worktree: std::path::PathBuf,
    pub error: Option<String>,
    pub result: Option<AgentResult>,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum InputStatus {
    Pending,
    Sending,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Input {
    pub text: String,
    pub status: InputStatus,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Log {
    pub id: i64,
    pub run_id: Option<String>,
    pub stream: String,
    pub content: String,
    pub created_at: String,
}
