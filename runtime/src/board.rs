use crate::unicode::CaseFold;
use crate::{
    Error, Result, fail,
    queries::*,
    string,
    transport::{GitHubTransport, next_cursor},
};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
pub const READY: &str = "Ready for work";
pub const PROGRESS: &str = "In Progress";
pub const REVIEW: &str = "Agent review";
pub const HUMAN: &str = "Human review";
/// Current-invocation consent only; never deserialize this from configuration or runtime metadata.
#[derive(Clone, Default)]
pub struct Invocation {
    pub project: Option<String>,
    pub dangerously_allow_public_board: bool,
}
#[derive(Clone)]
pub struct Board {
    pub repo: String,
    pub transport: GitHubTransport,
    pub project: Value,
    pub owner: String,
    pub number: i64,
    pub field: Value,
    pub options: BTreeMap<String, String>,
    pub required_statuses: Vec<String>,
    pub invocation: Invocation,
    pub security: Value,
    pub public_warning_shown: Arc<AtomicBool>,
}
pub fn dependency_count(issue: &Value) -> Result<i64> {
    issue["issueDependenciesSummary"]["blockedBy"]
        .as_i64()
        .filter(|n| *n >= 0)
        .ok_or_else(|| {
            Error::new(
                "github",
                "GitHub dependency status is unavailable; refusing assignment.",
            )
        })
}
impl Board {
    pub fn open(
        repo: &str,
        transport: GitHubTransport,
        project: Option<&str>,
        load_fields: bool,
        required_statuses: &[&str],
    ) -> Result<Self> {
        Self::open_with_policy(
            repo,
            transport,
            project,
            load_fields,
            required_statuses,
            Invocation::default(),
            false,
        )
    }
    #[allow(clippy::too_many_arguments)]
    pub fn open_with_policy(
        repo: &str,
        transport: GitHubTransport,
        project: Option<&str>,
        load_fields: bool,
        required_statuses: &[&str],
        invocation: Invocation,
        check_only: bool,
    ) -> Result<Self> {
        let (owner, name) = repo
            .split_once('/')
            .ok_or_else(|| Error::new("validation", "Invalid repository."))?;
        let (project, owner, number) = if let Some(p) = project.filter(|s| !s.is_empty()) {
            let (o, n) = p
                .rsplit_once('/')
                .ok_or_else(|| Error::new("validation", "Invalid project."))?;
            (
                transport.json(&["project", "view", n, "--owner", o, "--format", "json"])?,
                o.to_string(),
                crate::config::ConfigStore::issue_number(n)?,
            )
        } else {
            let data = transport.graphql(BOARD___INIT___0, &json!({"owner":owner,"name":name}))?;
            let mut candidates = data["repository"]["projectsV2"]["nodes"]
                .as_array()
                .ok_or_else(|| Error::new("github", "Missing project nodes."))?
                .iter()
                .filter(|p| p["closed"] != true)
                .cloned()
                .collect::<Vec<_>>();
            if candidates.is_empty() {
                let p = transport.json(&[
                    "project", "list", "--owner", owner, "--limit", "100", "--format", "json",
                ])?;
                candidates = p["projects"]
                    .as_array()
                    .ok_or_else(|| Error::new("github", "Missing project list."))?
                    .iter()
                    .filter(|p| p["closed"] != true)
                    .cloned()
                    .collect();
            }
            let matches = candidates
                .iter()
                .filter(|p| string(p, "title").casefold() == name.casefold())
                .cloned()
                .collect::<Vec<_>>();
            if !matches.is_empty() {
                candidates = matches;
            }
            if candidates.len() != 1 {
                let choices = candidates
                    .iter()
                    .map(|p| format!("{}: {}", p["number"], string(p, "title")))
                    .collect::<Vec<_>>()
                    .join(", ");
                return fail(format!(
                    "Choose a board with --project OWNER/NUMBER (boards: {}).",
                    if choices.is_empty() { "none" } else { &choices }
                ));
            }
            let p = candidates.pop().unwrap();
            let o = p["owner"]["login"].as_str().unwrap_or(owner).to_string();
            let n = p["number"]
                .as_i64()
                .ok_or_else(|| Error::new("github", "Missing project number."))?;
            (p, o, n)
        };
        let mut board = Self {
            repo: repo.into(),
            transport,
            project,
            owner,
            number,
            field: Value::Null,
            options: BTreeMap::new(),
            required_statuses: required_statuses.iter().map(|s| s.to_string()).collect(),
            invocation,
            security: Value::Null,
            public_warning_shown: Arc::new(AtomicBool::new(false)),
        };
        board.security = board.security_report()?;
        if !check_only {
            board.require_report(&board.security)?;
        }
        if load_fields && (!check_only || board.security["safe"] == true) {
            board.refresh_fields()?;
        }
        Ok(board)
    }
    pub fn security_report(&self) -> Result<Value> {
        let mut report = json!({"visibility":"unknown", "project_viewer_can_update":null,
            "repository_permission":"unknown", "safe":false,
            "public_board_override":self.invocation.dangerously_allow_public_board,
            "verification_limits":["Repository permissions do not establish project collaborator access.",
              "Project collaborator grants and workflow rule settings require manual review.",
              "Public visibility permits reading; it does not grant anonymous writes."]});
        let (owner, name) = self
            .repo
            .split_once('/')
            .ok_or_else(|| Error::new("validation", "Invalid repository."))?;
        match self.transport.graphql(
            r#"query($id:ID!, $owner:String!, $name:String!) {
              node(id:$id) { ... on ProjectV2 { public viewerCanUpdate } }
              repository(owner:$owner, name:$name) { viewerPermission }
            }"#,
            &json!({"id":self.project["id"],"owner":owner,"name":name}),
        ) {
            Ok(data) => {
                let project = &data["node"];
                let public = project["public"].as_bool();
                report["visibility"] = json!(match public {
                    Some(true) => "public",
                    Some(false) => "private",
                    None => "unknown",
                });
                report["project_viewer_can_update"] = project["viewerCanUpdate"].clone();
                report["repository_permission"] = data["repository"]
                    .get("viewerPermission")
                    .cloned()
                    .unwrap_or(json!("unknown"));
                report["safe"] = json!(
                    public.is_some_and(|p| !p || self.invocation.dangerously_allow_public_board)
                        && project["viewerCanUpdate"] == true
                        && matches!(
                            report["repository_permission"].as_str(),
                            Some("WRITE" | "MAINTAIN" | "ADMIN")
                        )
                );
            }
            Err(error) if error.kind == "quota" => return Err(error),
            Err(error) => report["error"] = json!(error.to_string()),
        }
        Ok(report)
    }
    fn require_report(&self, report: &Value) -> Result<()> {
        if report["safe"] != true {
            return fail(format!(
                "Board security preflight refused operation: {}; public boards require explicit --dangerously-allow-public-board.",
                crate::harness::python_json(report)
            ));
        }
        if report["visibility"] == "public"
            && !self.public_warning_shown.swap(true, Ordering::SeqCst)
        {
            eprintln!(
                "WARNING: --dangerously-allow-public-board permits operation on a publicly readable board. Review project access and automation rules; feedback still requires trusted permissions."
            );
        }
        Ok(())
    }
    pub fn require_safe(&self) -> Result<Value> {
        let report = self.security_report()?;
        self.require_report(&report)?;
        Ok(report)
    }
    pub fn refresh_fields(&mut self) -> Result<()> {
        let fields = self.transport.json(&[
            "project",
            "field-list",
            &self.number.to_string(),
            "--owner",
            &self.owner,
            "--format",
            "json",
        ])?;
        self.field = fields["fields"]
            .as_array()
            .and_then(|a| {
                a.iter()
                    .find(|f| f["name"] == "Status" && f["options"].is_array())
            })
            .cloned()
            .ok_or_else(|| {
                Error::new(
                    "github",
                    "The selected project needs a single-select Status field.",
                )
            })?;
        self.options = self.field["options"]
            .as_array()
            .unwrap()
            .iter()
            .map(|o| (string(o, "name").casefold(), string(o, "id")))
            .collect();
        if self.field["id"].as_str().is_none_or(str::is_empty)
            || self.field["options"]
                .as_array()
                .unwrap()
                .iter()
                .any(|o| o["name"].as_str().is_none() || o["id"].as_str().is_none_or(str::is_empty))
        {
            return fail(
                "GitHub returned incomplete Status options; refusing a write destination.",
            );
        }
        for status in &self.required_statuses {
            if !self.options.contains_key(&status.casefold()) {
                return fail(format!("The selected board is missing status '{status}'."));
            }
        }
        Ok(())
    }
    pub fn ensure_review_status(&mut self) -> Result<()> {
        self.require_safe()?;
        if self.options.contains_key(&REVIEW.casefold()) {
            return Ok(());
        }
        let data = self.transport.graphql(
            BOARD_ENSURE_REVIEW_STATUS_0,
            &json!({"id":self.field["id"]}),
        )?;
        let mut options = data["node"]["options"]
            .as_array()
            .cloned()
            .ok_or_else(|| Error::new("github", "Missing status options."))?;
        let i = options
            .iter()
            .position(|o| string(o, "name").casefold() == HUMAN.casefold())
            .ok_or_else(|| Error::new("github", "Missing Human review option."))?;
        options.insert(i,json!({"name":REVIEW,"color":"PINK","description":"Independent agent review before human review"}));
        self.transport.graphql(
            BOARD_ENSURE_REVIEW_STATUS_1,
            &json!({"input":{"fieldId":self.field["id"],"singleSelectOptions":options}}),
        )?;
        self.refresh_fields()?;
        if !self.options.contains_key(&REVIEW.casefold()) {
            return fail("Could not create Agent review status.");
        }
        Ok(())
    }
    pub fn linked_prs(&self, id: &Value, mut c: Value) -> Result<Vec<String>> {
        let mut out = Vec::new();
        let mut seen = BTreeSet::new();
        while !c.is_null() {
            let a = c["nodes"]
                .as_array()
                .ok_or_else(|| Error::new("github", "Missing linked PR nodes."))?;
            for pr in a {
                let url = string(pr, "url");
                if !out.contains(&url) {
                    out.push(url);
                }
            }
            if out.len() > 10000 {
                return fail("Linked PR connection exceeds 10,000 items.");
            }
            let Some(cursor) = next_cursor(&c["pageInfo"], &mut seen)? else {
                break;
            };
            c=self.transport.graphql(BOARD_LINKED_PRS_0,&json!({"id":id,"cursor":cursor}))?["node"]["links"]["pullRequests"].clone();
            if c.is_null() {
                return fail("GitHub returned incomplete linked PR pagination.");
            }
        }
        Ok(out)
    }
    pub fn items(&self) -> Result<Vec<Value>> {
        let mut out = Vec::new();
        let mut seen = BTreeSet::new();
        let mut cursor = Value::Null;
        let mut count = 0;
        loop {
            let data = self.transport.graphql(
                BOARD_ITEMS_0,
                &json!({"id":self.project["id"],"cursor":cursor}),
            )?;
            let c = &data["node"]["items"];
            let nodes = c["nodes"]
                .as_array()
                .ok_or_else(|| Error::new("github", "Missing board nodes."))?;
            count += nodes.len();
            let total = c["totalCount"]
                .as_u64()
                .ok_or_else(|| Error::new("github", "Missing board total count."))?
                as usize;
            if total.max(count) > 10000 {
                return fail("Board exceeds 10,000 items; refusing an incomplete queue.");
            }
            for node in nodes {
                let issue = &node["content"];
                if issue["state"] == "CLOSED"
                    || issue["repository"]["nameWithOwner"]
                        .as_str()
                        .is_none_or(|s| s.casefold() != self.repo.casefold())
                {
                    continue;
                }
                let mut content = json!({});
                for key in ["number", "title", "body", "url"] {
                    content[key] = issue[key].clone();
                }
                content["type"] = json!("Issue");
                content["repository"] = issue["repository"]["nameWithOwner"].clone();
                out.push(json!({"id":node["id"],"status":node["fieldValueByName"]["name"].as_str().unwrap_or(""),"title":issue["title"],"content":content,"blocked_by_count":dependency_count(issue)?,"linked pull requests":self.linked_prs(&node["id"],node["links"]["pullRequests"].clone())?}));
            }
            if let Some(c) = next_cursor(&c["pageInfo"], &mut seen)? {
                cursor = json!(c);
            } else {
                if count != total {
                    return fail("GitHub returned incomplete board pagination.");
                }
                return Ok(out);
            }
        }
    }
    pub fn dependencies_ready(&self, item: &mut Value) -> Result<bool> {
        if item["blocked_by_count"].as_i64().unwrap_or(0) != 0 {
            return Ok(false);
        }
        let (owner, name) = self.repo.split_once('/').unwrap();
        let data = self.transport.graphql(
            BOARD_DEPENDENCIES_READY_0,
            &json!({"owner":owner,"name":name,"number":item["content"]["number"]}),
        )?;
        let count = dependency_count(&data["repository"]["issue"])?;
        item["blocked_by_count"] = json!(count);
        Ok(count == 0)
    }
    pub fn item_status(&self, item: &Value) -> Result<Option<String>> {
        let data = self
            .transport
            .graphql(BOARD_ITEM_STATUS_0, &json!({"id":item["id"]}))?;
        let node = &data["node"];
        if node["project"]["id"] != self.project["id"] {
            return Ok(None);
        }
        Ok(Some(
            node["fieldValueByName"]["name"]
                .as_str()
                .unwrap_or("")
                .casefold(),
        ))
    }
    pub fn move_item(&self, item: &Value, status: &str, expected: &str) -> Result<()> {
        self.require_safe()?;
        if self.item_status(item)?.as_deref() != Some(&expected.casefold()) {
            return fail(format!(
                "Issue status changed; expected '{expected}'. Leaving it for inspection."
            ));
        }
        let option = self
            .options
            .get(&status.casefold())
            .ok_or_else(|| Error::new("validation", format!("Missing status {status}.")))?;
        self.transport.text(
            &[
                "project",
                "item-edit",
                "--id",
                &string(item, "id"),
                "--project-id",
                &string(&self.project, "id"),
                "--field-id",
                &string(&self.field, "id"),
                "--single-select-option-id",
                option,
            ],
            None,
        )?;
        if self.item_status(item)?.as_deref() != Some(&status.casefold()) {
            return fail(format!("Could not verify board transition to {status}."));
        }
        Ok(())
    }
    pub fn add_issue(&self, title: &str, body: &str, status: &str) -> Result<String> {
        self.require_safe()?;
        let option = self.options.get(&status.casefold()).ok_or_else(|| {
            Error::new(
                "validation",
                format!("The selected board is missing status '{status}'."),
            )
        })?;
        let url = self.transport.text(
            &[
                "issue",
                "create",
                "--repo",
                &self.repo,
                "--title",
                title,
                "--body-file",
                "-",
            ],
            Some(body),
        )?;
        let result = (|| -> Result<()> {
            let item = self.transport.json(&[
                "project",
                "item-add",
                &self.number.to_string(),
                "--owner",
                &self.owner,
                "--url",
                &url,
                "--format",
                "json",
            ])?;
            self.transport.text(
                &[
                    "project",
                    "item-edit",
                    "--id",
                    &string(&item, "id"),
                    "--project-id",
                    &string(&self.project, "id"),
                    "--field-id",
                    &string(&self.field, "id"),
                    "--single-select-option-id",
                    option,
                ],
                None,
            )?;
            let after = self.items()?.into_iter().find(|i| i["id"] == item["id"]);
            let statuses = if status.casefold() == READY.casefold() {
                vec![READY, PROGRESS, REVIEW, HUMAN]
            } else {
                vec![status]
            };
            if !after.is_some_and(|a| {
                statuses
                    .iter()
                    .any(|s| s.casefold() == string(&a, "status").casefold())
            }) {
                return fail(format!("Could not verify board status '{status}'."));
            }
            Ok(())
        })();
        if let Err(e) = result {
            return fail(format!(
                "Issue created: {url}, but board placement in {}/{} with status '{status}' failed: {e}. Keep this issue and finish its board placement manually; do not rerun add.",
                self.owner, self.number
            ));
        }
        Ok(url)
    }
}
