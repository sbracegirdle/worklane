use crate::unicode::CaseFold;
use crate::{
    Error, Result,
    board::{Board, HUMAN, PROGRESS, READY, REVIEW, dependency_count},
    config::ConfigStore,
    fail,
    harness::AgentHarness,
    queries::*,
    state::StateStore,
    string,
    transport::{command, next_cursor},
};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    os::fd::RawFd,
    path::{Path, PathBuf},
};
fn flag(v: &Value, k: &str) -> bool {
    v[k].as_bool().unwrap_or(false)
}
fn nonempty(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.),
    }
}
fn remove(v: &mut Value, keys: &[&str]) {
    if let Some(o) = v.as_object_mut() {
        for k in keys {
            o.remove(*k);
        }
    }
}
fn list(v: &Value) -> Vec<Value> {
    v.as_array().cloned().unwrap_or_default()
}
fn required_array<'a>(value: &'a Value, label: &str) -> Result<&'a Vec<Value>> {
    value.as_array().ok_or_else(|| {
        Error::new(
            "github",
            format!("Missing or invalid {label}; refusing an incomplete snapshot."),
        )
    })
}
fn sha(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        s.len() == 40
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}
fn time(s: &str) -> Result<chrono::DateTime<chrono::FixedOffset>> {
    chrono::DateTime::parse_from_rfc3339(s).map_err(|e| Error::new("validation", e.to_string()))
}
type StackPolicy = (BTreeSet<(i64, i64)>, BTreeSet<i64>);
#[derive(Clone)]
pub struct Workflow {
    pub state: StateStore,
    pub board: Board,
    pub agent: AgentHarness,
    pub root: PathBuf,
    pub base: String,
    pub stack_items: Vec<Value>,
    pub feedback_unavailable: BTreeSet<i64>,
}
impl Workflow {
    pub fn new(
        state: StateStore,
        board: Board,
        agent: AgentHarness,
        root: PathBuf,
        base: String,
    ) -> Self {
        let mut workflow = Self {
            state,
            board,
            agent,
            root,
            base,
            stack_items: Vec::new(),
            feedback_unavailable: BTreeSet::new(),
        };
        let guard = workflow.clone();
        workflow.agent.preflight = Some(std::sync::Arc::new(move |stage, task| {
            guard.before_turn(stage, task)
        }));
        workflow
    }
    pub fn issue(&self, number: i64) -> Result<Value> {
        self.board.transport.json(&[
            "issue",
            "view",
            &number.to_string(),
            "--repo",
            &self.state.repo,
            "--json",
            "number,title,body,state,labels,comments,url",
        ])
    }
    pub fn pr_number(&self, url: &str) -> Result<i64> {
        let prefix = format!("https://github.com/{}/pull/", self.state.repo);
        if !url.casefold().starts_with(&prefix.casefold()) {
            return fail("Agent returned a PR outside the assigned repository.");
        }
        let number = &url[prefix.len()..];
        if number.is_empty() || !number.bytes().all(|c| c.is_ascii_digit()) {
            return fail("Agent returned a PR outside the assigned repository.");
        }
        number
            .parse()
            .map_err(|_| Error::new("validation", "Invalid PR number."))
    }
    pub fn pr(&self, url: &str) -> Result<Value> {
        self.pr_number(url)?;
        let pr=self.board.transport.json(&["pr","view",url,"--repo",&self.state.repo,"--json","url,headRefOid,headRefName,baseRefName,state,isDraft,mergeable,mergedAt,isCrossRepository"])?;
        if !pr["isDraft"].is_boolean()
            || !pr["isCrossRepository"].is_boolean()
            || [
                "url",
                "headRefOid",
                "headRefName",
                "baseRefName",
                "state",
                "mergeable",
            ]
            .iter()
            .any(|k| !pr[*k].is_string())
        {
            return fail("GitHub returned incomplete PR metadata; refusing handoff.");
        }
        Ok(pr)
    }
    pub fn review_threads(&self, url: &str) -> Result<Vec<Value>> {
        let n = self.pr_number(url)?;
        let (owner, name) = self.state.repo.split_once('/').unwrap();
        let mut threads = Vec::new();
        let mut seen = BTreeSet::new();
        let mut cursor = Value::Null;
        loop {
            let data = self.board.transport.graphql(
                RUNNER_REVIEW_THREADS_0,
                &json!({"owner":owner,"name":name,"number":n,"cursor":cursor}),
            )?;
            let c = &data["repository"]["pullRequest"]["reviewThreads"];
            let nodes = c["nodes"]
                .as_array()
                .ok_or_else(|| Error::new("github", "Missing review threads."))?;
            for t in nodes {
                let mut t = t.clone();
                let mut comments = t["comments"].clone();
                let mut replies = required_array(&comments["nodes"], "thread replies")?.clone();
                let mut reply_seen = BTreeSet::new();
                while let Some(c) = next_cursor(&comments["pageInfo"], &mut reply_seen)? {
                    let p = self
                        .board
                        .transport
                        .graphql(RUNNER_REVIEW_THREADS_1, &json!({"id":t["id"],"cursor":c}))?;
                    comments = p["node"]["comments"].clone();
                    replies.extend(
                        required_array(&comments["nodes"], "thread replies")?
                            .iter()
                            .cloned(),
                    );
                    if replies.len() > 10000 {
                        return fail("Review thread exceeds 10,000 replies.");
                    }
                }
                t["comments"] = json!(replies);
                threads.push(t);
            }
            if threads.len() > 10000 {
                return fail("Review exceeds 10,000 threads.");
            }
            if let Some(c) = next_cursor(&c["pageInfo"], &mut seen)? {
                cursor = json!(c);
            } else {
                return Ok(threads);
            }
        }
    }
    pub fn refresh_review_threads(&self, task: &mut Value, require_resolved: bool) -> Result<()> {
        let threads = if nonempty(&task["pr_url"]) && !flag(task, "question") {
            self.review_threads(&string(task, "pr_url"))?
        } else {
            Vec::new()
        };
        task["review_threads"] = json!(threads);
        self.state.save(task)?;
        let unresolved = threads
            .iter()
            .filter(|t| !flag(t, "isResolved"))
            .map(|t| format!("{} ({})", string(t, "id"), string(t, "path")))
            .collect::<Vec<_>>();
        if require_resolved && !unresolved.is_empty() {
            return fail(format!(
                "Unresolved PR review threads block completion: {}",
                unresolved.join("; ")
            ));
        }
        Ok(())
    }
    pub fn human_feedback(&self, task: &Value) -> Result<Value> {
        let mut sources = vec![("issue", format!("issues/{}/comments", task["number"]))];
        if nonempty(&task["pr_url"]) {
            let pr = self.pr(&string(task, "pr_url"))?;
            if pr["state"] != "OPEN"
                || pr["headRefName"] != task["branch"]
                || pr["baseRefName"] != task["base"]
                || pr["headRefOid"] != task["head_sha"]
            {
                return fail(
                    "Human feedback PR was closed, retargeted or changed; inspect before rework.",
                );
            }
            let n = self.pr_number(&string(task, "pr_url"))?;
            sources.extend([
                ("pr", format!("issues/{n}/comments")),
                ("review", format!("pulls/{n}/reviews")),
                ("inline", format!("pulls/{n}/comments")),
            ]);
        }
        let mut feedback = json!({});
        let mut permissions = std::collections::BTreeMap::new();
        for (source, endpoint) in sources {
            let pages = self.board.transport.json(&[
                "api",
                &format!("repos/{}/{endpoint}?per_page=100", self.state.repo),
                "--paginate",
                "--slurp",
            ])?;
            for page in required_array(&pages, "feedback pages")? {
                for entry in required_array(page, "feedback entries")? {
                    let body = string(entry, "body");
                    let user = &entry["user"];
                    let state = string(entry, "state").to_uppercase();
                    if user["type"] != "User"
                        || string(user, "login").ends_with("[bot]")
                        || body.trim_start().starts_with("<!-- worklane:")
                        || source == "review"
                            && !matches!(state.as_str(), "COMMENTED" | "CHANGES_REQUESTED")
                        || body.trim().is_empty() && state != "CHANGES_REQUESTED"
                    {
                        continue;
                    }
                    let login = string(user, "login");
                    if !self.cached_trusted_user(&login, &mut permissions)? {
                        continue;
                    }
                    match self.feedback_editor(entry)? {
                        Value::Null => (),
                        Value::String(editor)
                            if self.cached_trusted_user(&editor, &mut permissions)? => {}
                        _ => continue,
                    }
                    let updated = entry["updated_at"]
                        .as_str()
                        .or_else(|| entry["submitted_at"].as_str())
                        .or_else(|| entry["created_at"].as_str())
                        .ok_or_else(|| Error::new("github", "Missing feedback timestamp."))?;
                    let mut f = json!({"body":body,"author":user["login"],"state":state,"updated_at":updated,"url":entry["html_url"].as_str().unwrap_or("")});
                    if source == "inline" {
                        f["path"] = entry["path"].clone();
                        f["line"] = entry["line"].clone();
                    }
                    feedback[format!("{source}:{}", entry["id"])] = f;
                }
            }
        }
        Ok(feedback)
    }
    fn cached_trusted_user(
        &self,
        login: &str,
        permissions: &mut std::collections::BTreeMap<String, bool>,
    ) -> Result<bool> {
        if let Some(value) = permissions.get(login) {
            return Ok(*value);
        }
        let trusted = self.trusted_feedback_user(login)?;
        permissions.insert(login.into(), trusted);
        Ok(trusted)
    }
    pub fn trusted_feedback_user(&self, login: &str) -> Result<bool> {
        if login.is_empty()
            || !login
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'-')
        {
            return Ok(false);
        }
        let access = self.board.transport.json(&[
            "api",
            &format!("repos/{}/collaborators/{login}/permission", self.state.repo),
        ])?;
        Ok(matches!(
            access["permission"].as_str(),
            Some("write" | "maintain" | "admin")
        ))
    }
    pub fn feedback_editor(&self, entry: &Value) -> Result<Value> {
        if entry["node_id"].as_str().is_none_or(str::is_empty) {
            return fail("Feedback edit provenance is unavailable; refusing rework.");
        }
        let data = self.board.transport.graphql(
            r#"query($id:ID!) { node(id:$id) {
          ... on IssueComment { body lastEditedAt editor { login __typename } }
          ... on PullRequestReview { body lastEditedAt editor { login __typename } }
          ... on PullRequestReviewComment { body lastEditedAt editor { login __typename } }
        } }"#,
            &json!({"id":entry["node_id"]}),
        )?;
        let node = &data["node"];
        if node.get("lastEditedAt").is_none() || node["body"] != string(entry, "body") {
            return fail("Feedback edit provenance changed or is unavailable; refusing rework.");
        }
        if !nonempty(&node["lastEditedAt"]) {
            return Ok(Value::Null);
        }
        let editor = &node["editor"];
        if editor["__typename"] != "User" || string(editor, "login").is_empty() {
            return Ok(json!(false));
        }
        Ok(editor["login"].clone())
    }
    pub fn require_trusted_feedback(&self, task: &Value) -> Result<()> {
        if nonempty(&task["human_feedback"]) {
            let snapshot = self.human_feedback(task)?;
            let saved = task["human_feedback"]
                .as_object()
                .ok_or_else(|| Error::new("validation", "Invalid saved feedback."))?;
            if saved
                .iter()
                .any(|(key, value)| snapshot.get(key) != Some(value))
            {
                return fail("Feedback authorization or content changed; inspect before rework.");
            }
        }
        Ok(())
    }
    pub fn before_turn(&self, stage: &str, task: &Value) -> Result<()> {
        self.board.require_safe()?;
        if stage == "build" {
            self.require_trusted_feedback(task)?;
        }
        Ok(())
    }
    pub fn feedback_quiet(&self, task: &Value) -> Result<bool> {
        let Some(observed) = task["human_feedback_observed_at"].as_str() else {
            return Ok(false);
        };
        Ok((time(&self.state.deps.clock.now())? - time(observed)?).num_seconds() >= 300)
    }
    pub fn poll_human_feedback(
        &mut self,
        items: &mut [Value],
        active: &BTreeSet<i64>,
    ) -> Result<()> {
        self.board.require_safe()?;
        self.feedback_unavailable.clear();
        for mut task in self.state.all()? {
            let number = task["number"].as_i64().unwrap();
            if active.contains(&number) {
                continue;
            }
            let mut pending_rework =
                task["phase"] == "rework" && flag(&task, "human_feedback_pending");
            if task["phase"] != "human_review" && !pending_rework {
                continue;
            }
            let Some(current) = items.iter_mut().find(|i| i["id"] == task["item"]["id"]) else {
                continue;
            };
            if task["phase"] == "human_review"
                && flag(&task, "human_feedback_pending")
                && string(current, "status").casefold() == READY.casefold()
            {
                task["phase"] = json!("rework");
                task["round"] = json!(0);
                self.state.save(&mut task)?;
                pending_rework = true;
            }
            let expected = if pending_rework { READY } else { HUMAN };
            if string(current, "status").casefold() != expected.casefold() {
                continue;
            }
            if !pending_rework && !nonempty(&task["human_feedback"]) {
                if let Some(p) = task["human_feedback_polled_at"].as_str() {
                    if (time(&self.state.deps.clock.now())? - time(p)?).num_seconds() < 300 {
                        continue;
                    }
                }
            }
            let attempt = (|| -> Result<()> {
                let issue = self.issue(number)?;
                if issue["state"] != "OPEN" {
                    return Ok(());
                }
                let snapshot = self.human_feedback(&task)?;
                if task.get("human_feedback_seen").is_none() {
                    let cutoff = time(&string(&task, "updated_at"))?;
                    let mut baseline = json!({});
                    for (k, v) in snapshot.as_object().unwrap() {
                        if time(&string(v, "updated_at"))? <= cutoff {
                            baseline[k] = v.clone();
                        }
                    }
                    task["human_feedback_seen"] = baseline;
                }
                let mut pending = json!({});
                for (k, v) in snapshot.as_object().unwrap() {
                    if ["body", "author", "state", "updated_at"]
                        .iter()
                        .any(|f| task["human_feedback_seen"][k][*f] != v[*f])
                    {
                        pending[k] = v.clone();
                    }
                }
                if pending != task.get("human_feedback").cloned().unwrap_or(json!({})) {
                    task["human_feedback_observed_at"] = json!(self.state.deps.clock.now());
                }
                task["human_feedback"] = pending.clone();
                task["human_feedback_snapshot"] = snapshot;
                task["issue"] = issue;
                task["human_feedback_polled_at"] = json!(self.state.deps.clock.now());
                if nonempty(&pending) && self.feedback_quiet(&task)? && !pending_rework {
                    if !flag(&task, "human_feedback_pending") {
                        task["human_feedback_generation"] =
                            json!(task["human_feedback_generation"].as_i64().unwrap_or(0) + 1);
                    }
                    task["human_feedback_pending"] = json!(true);
                    self.state.save(&mut task)?;
                    self.board.move_item(current, READY, HUMAN)?;
                    current["status"] = json!(READY);
                    task["phase"] = json!("rework");
                    task["round"] = json!(0);
                }
                self.state.save(&mut task)
            })();
            if let Err(e) = attempt {
                if e.kind == "quota" {
                    return Err(e);
                }
                self.feedback_unavailable.insert(number);
                self.state.append_log(
                    None,
                    "controller-error",
                    &format!("#{number} human feedback unavailable: {e}"),
                )?;
            }
        }
        Ok(())
    }
    fn stack_policy(&self) -> Result<StackPolicy> {
        let c = ConfigStore::resolve(&self.root, &json!({}))?;
        let edges = list(&c["stack_dependencies"])
            .into_iter()
            .map(|e| {
                let s = e.as_str().unwrap();
                let (a, b) = s.split_once(':').unwrap();
                Ok((ConfigStore::issue_number(a)?, ConfigStore::issue_number(b)?))
            })
            .collect::<Result<_>>()?;
        let gates = list(&c["completion_gates"])
            .iter()
            .filter_map(Value::as_i64)
            .collect();
        Ok((edges, gates))
    }
    fn dependency_block(&self, item: &mut Value, reason: &str) -> Result<bool> {
        item["dependency_reason"] = json!(reason);
        self.state.set_meta(
            &format!("dependency:{}", item["content"]["number"]),
            &json!({"reason":reason,"at":self.state.deps.clock.now()}),
        )?;
        Ok(false)
    }
    pub fn dependencies_ready(&self, item: &mut Value, saved: Option<&Value>) -> Result<bool> {
        let (edges, gates) = self.stack_policy()?;
        let number = item["content"]["number"]
            .as_i64()
            .ok_or_else(|| Error::new("validation", "Missing issue number."))?;
        let saved = if let Some(v) = saved {
            v.clone()
        } else {
            self.state.load(number)?
        };
        if !edges.iter().any(|(c, _)| *c == number)
            && !nonempty(&saved["stack"])
            && gates.is_empty()
        {
            return self.board.dependencies_ready(item);
        }
        let (owner, name) = self.state.repo.split_once('/').unwrap();
        let data = self.board.transport.graphql(
            RUNNER_DEPENDENCIES_READY_0,
            &json!({"owner":owner,"name":name,"number":number}),
        )?;
        let count = dependency_count(&data["repository"]["issue"])?;
        item["blocked_by_count"] = json!(count);
        let pages = self.board.transport.json(&[
            "api",
            &format!(
                "repos/{}/issues/{number}/dependencies/blocked_by?per_page=100",
                self.state.repo
            ),
            "--paginate",
            "--slurp",
        ])?;
        let mut deps = Vec::new();
        for page in required_array(&pages, "dependency pages")? {
            deps.extend(required_array(page, "dependency entries")?.iter().cloned());
        }
        if deps
            .iter()
            .any(|d| !matches!(d["state"].as_str(), Some("open" | "closed")))
        {
            return self.dependency_block(item, "Incomplete dependency states; inspect GitHub.");
        }
        if deps.iter().filter(|d| d["state"] == "open").count() != count as usize {
            return self.dependency_block(
                item,
                "Dependency list changed or is incomplete; retry next poll.",
            );
        }
        let mut parents = Vec::new();
        for d in deps {
            let n = d["number"]
                .as_i64()
                .ok_or_else(|| Error::new("github", "Missing dependency number."))?;
            if string(&d, "repository_url").casefold()
                != format!("https://api.github.com/repos/{}", self.state.repo).casefold()
            {
                if d["state"] == "open" {
                    return self
                        .dependency_block(item, "Open external dependency requires completion.");
                }
                continue;
            }
            if gates.contains(&n) {
                let gate = self.state.load(n)?;
                let head = &gate["merged_head_sha"];
                if d["state"] != "closed"
                    || gate["phase"] != "done"
                    || gate["completion_source"] != "merged_pr"
                    || !sha(head)
                    || gate["head_sha"] != *head
                    || ["review_result", "handoff_result"].iter().any(|k| {
                        gate[*k]["verdict"] != "pass"
                            || gate[*k]["head_sha"] != *head
                            || nonempty(&gate[*k]["findings"])
                    })
                {
                    return self.dependency_block(
                        item,
                        &format!("Hard completion gate #{n} requires its verified merged handoff."),
                    );
                }
            }
            if d["state"] == "closed" {
                continue;
            }
            if !edges.contains(&(number, n)) {
                return self.dependency_block(
                    item,
                    &format!(
                        "Hard dependency #{n} must complete; edge is not opted into stacking."
                    ),
                );
            }
            let pr = match self.reviewed_parent(n) {
                Ok((_, p)) => p,
                Err(e) if e.kind == "quota" => return Err(e),
                Err(e) => return self.dependency_block(item, &e.to_string()),
            };
            parents.push(json!({"number":n,"pr_url":pr["url"],"branch":pr["headRefName"],"base":pr["baseRefName"],"head_sha":pr["headRefOid"],"reviewed_head":pr["headRefOid"]}));
        }
        if parents.len() > 1 {
            return self.dependency_block(item,"Multiple open stack parents are unsupported; wait for completion or choose a linear dependency chain.");
        }
        let mut stack = Vec::new();
        if let Some(p) = parents.pop() {
            let parent = self.state.load(p["number"].as_i64().unwrap())?;
            stack = list(&parent["stack"]);
            stack.push(p);
            let numbers = stack
                .iter()
                .map(|e| e["number"].as_i64().unwrap_or(0))
                .collect::<BTreeSet<_>>();
            if numbers.contains(&number) || numbers.len() != stack.len() {
                return self.dependency_block(item, "Dependency stack cycle detected.");
            }
            if nonempty(&saved["stack"]) && saved["stack"] != json!(stack) {
                return self.dependency_block(
                    item,
                    "Pinned upstream head changed; restack and independent review required.",
                );
            }
        }
        item["stack_plan"] = json!(stack);
        remove(item, &["dependency_reason"]);
        self.state.set_meta(
            &format!("dependency:{number}"),
            &json!({"reason":"","at":self.state.deps.clock.now()}),
        )?;
        Ok(true)
    }
    fn reviewed_parent(&self, n: i64) -> Result<(Value, Value)> {
        let mut parent = self.state.load(n)?;
        let current = self
            .stack_items
            .iter()
            .find(|i| i["content"]["number"] == n);
        if parent["phase"] != "human_review"
            || nonempty(&parent["stack_blocked"])
            || nonempty(&parent["restack_pending"])
            || nonempty(&parent["human_feedback_pending"])
            || nonempty(&parent["human_feedback"])
            || self.feedback_unavailable.contains(&n)
            || current.is_none()
        {
            return fail(format!("Stack parent #{n} is not ready in Human review."));
        }
        if self.board.item_status(current.unwrap())?.as_deref() != Some(&HUMAN.casefold()) {
            return fail(format!("Stack parent #{n} is not ready in Human review."));
        }
        let head = &parent["head_sha"];
        if !sha(head)
            || ["review_result", "handoff_result"].iter().any(|k| {
                parent[*k]["verdict"] != "pass"
                    || parent[*k]["head_sha"] != *head
                    || nonempty(&parent[*k]["findings"])
            })
        {
            return fail(format!(
                "Stack parent #{n} lacks independent review and handoff at its exact head."
            ));
        }
        let mut pr = self.pr(&string(&parent, "pr_url"))?;
        if flag(&pr, "isCrossRepository")
            || pr["state"] != "OPEN"
            || flag(&pr, "isDraft")
            || pr["mergeable"] != "MERGEABLE"
            || pr["headRefOid"] != *head
            || pr["headRefName"] != parent["branch"]
            || pr["baseRefName"] != parent["base"]
        {
            return fail(format!(
                "Stack parent #{n} PR changed, conflicted or closed."
            ));
        }
        self.refresh_review_threads(&mut parent, true)?;
        if self.human_feedback(&parent)?
            != parent
                .get("human_feedback_seen")
                .cloned()
                .unwrap_or(json!({}))
        {
            return fail(format!("Stack parent #{n} has unprocessed human feedback."));
        }
        pr["url"] = parent["pr_url"].clone();
        Ok((parent, pr))
    }
    pub fn validate_stack(&self, task: &Value) -> Result<()> {
        if nonempty(&task["stack_blocked"]) || nonempty(&task["restack_pending"]) {
            return fail(
                task["stack_blocked"]
                    .as_str()
                    .unwrap_or("Restack is incomplete."),
            );
        }
        for e in list(&task["stack"]) {
            let (_, p) = self.reviewed_parent(e["number"].as_i64().unwrap_or(0))?;
            if p["headRefName"] != e["branch"]
                || p["baseRefName"] != e["base"]
                || p["headRefOid"] != e["head_sha"]
            {
                return fail(format!(
                    "Stack prerequisite #{} changed; reconcile the stack before continuing.",
                    e["number"]
                ));
            }
        }
        Ok(())
    }
    pub fn verify_pr(&self, task: &Value, head: &str) -> Result<Value> {
        self.validate_stack(task)?;
        let pr = self.pr(&string(task, "pr_url"))?;
        if pr["state"] != "OPEN" || pr["headRefOid"] != head {
            return fail("PR was closed or its head changed; a fresh review is required.");
        }
        if pr["headRefName"] != task["branch"]
            || pr["baseRefName"] != task["base"]
            || flag(&pr, "isCrossRepository")
        {
            return fail("PR does not target the assigned branch/base.");
        }
        if nonempty(&task["stack"]) {
            command(&["git", "fetch", "origin"], &self.root, None)?;
            if command(
                &[
                    "git",
                    "rev-parse",
                    &format!("origin/{}", string(task, "branch")),
                ],
                &self.root,
                None,
            )? != head
            {
                return fail("PR head is not the exact pushed child branch revision.");
            }
            for edge in list(&task["stack"]) {
                command(
                    &[
                        "git",
                        "merge-base",
                        "--is-ancestor",
                        &string(&edge, "head_sha"),
                        head,
                    ],
                    &self.root,
                    None,
                )?;
            }
        }
        Ok(pr)
    }
    pub fn worktree(&self, task: &mut Value, review: bool) -> Result<PathBuf> {
        let session = self.state.session(
            task["number"].as_i64().unwrap_or(0),
            if review { "reviewer" } else { "worker" },
        )?;
        let key = if review {
            "review_worktree"
        } else {
            "worker_worktree"
        };
        let saved = task[key]
            .as_str()
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .or_else(|| session.as_ref().map(|s| string(s, "worktree")));
        let path = saved.as_ref().map(PathBuf::from).unwrap_or_else(|| {
            self.state.path.join("worktrees").join(if review {
                format!("review-{}", task["number"])
            } else {
                task["number"].to_string()
            })
        });
        if saved.is_some() && !path.exists() {
            return fail(format!(
                "Session worktree is missing: {}. Restore it before resuming.",
                path.display()
            ));
        }
        if path.exists() {
            if review {
                if command(&["git", "rev-parse", "HEAD"], &path, None)? != string(task, "head_sha")
                {
                    if !command(&["git", "diff", "HEAD", "--"], &path, None)?.is_empty() {
                        return fail(
                            "Reviewer worktree has tracked changes; inspect before switching revisions.",
                        );
                    }
                    command(&["git", "fetch", "origin"], &self.root, None)?;
                    command(
                        &["git", "checkout", "--detach", &string(task, "head_sha")],
                        &path,
                        None,
                    )?;
                }
                return Ok(path);
            }
            if command(&["git", "branch", "--show-current"], &path, None)? != string(task, "branch")
            {
                return fail("Saved worktree is on an unexpected branch.");
            }
            return Ok(path);
        }
        command(&["git", "fetch", "origin"], &self.root, None)?;
        std::fs::create_dir_all(path.parent().unwrap())?;
        if review {
            command(
                &[
                    "git",
                    "worktree",
                    "add",
                    "--detach",
                    &path.to_string_lossy(),
                    &string(task, "head_sha"),
                ],
                &self.root,
                None,
            )?;
        } else {
            let branch = string(task, "branch");
            let remote = command(
                &[
                    "git",
                    "show-ref",
                    "--verify",
                    "--quiet",
                    &format!("refs/remotes/origin/{branch}"),
                ],
                &self.root,
                None,
            )
            .is_ok();
            if command(
                &[
                    "git",
                    "show-ref",
                    "--verify",
                    "--quiet",
                    &format!("refs/heads/{branch}"),
                ],
                &self.root,
                None,
            )
            .is_ok()
            {
                return fail(format!(
                    "Branch {branch} already exists without a saved worktree; inspect before retrying."
                ));
            }
            let start = if remote {
                format!("origin/{branch}")
            } else {
                task["base_sha"]
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| format!("origin/{}", string(task, "base")))
            };
            if nonempty(&task["stack"])
                && !remote
                && command(
                    &[
                        "git",
                        "rev-parse",
                        &format!("origin/{}", string(task, "base")),
                    ],
                    &self.root,
                    None,
                )? != string(task, "base_sha")
            {
                return fail("Stack parent branch changed before worktree creation.");
            }
            command(
                &[
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    &branch,
                    &path.to_string_lossy(),
                    &start,
                ],
                &self.root,
                None,
            )?;
        }
        task[key] = json!(path);
        self.state.save(task)?;
        Ok(path)
    }
    fn verify_stack_ancestry(&self, task: &Value, tree: &Path) -> Result<()> {
        for e in list(&task["stack"]) {
            if command(
                &[
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    &string(&e, "head_sha"),
                    "HEAD",
                ],
                tree,
                None,
            )
            .is_err()
            {
                return fail(format!(
                    "Child branch does not contain pinned parent #{}; inspect before continuing.",
                    e["number"]
                ));
            }
        }
        Ok(())
    }
    pub fn post(&self, task: &Value, stage: &str, result: &Value) -> Result<()> {
        self.board.require_safe()?;
        let mut marker = format!(
            "<!-- worklane:{}:{stage}:{}:{}",
            task["number"],
            task["head_sha"].as_str().unwrap_or("question"),
            task["round"].as_i64().unwrap_or(0)
        );
        if nonempty(&task["human_feedback_generation"]) {
            marker.push_str(&format!(":human-{}", task["human_feedback_generation"]));
        }
        marker.push_str(" -->");
        let role = match stage {
            "agent review" => "reviewer",
            "human testing handoff" => "finalizer",
            _ => "worker",
        };
        let session = self
            .state
            .session(task["number"].as_i64().unwrap_or(0), role)?;
        let model = if let Some(s) = session {
            format!(
                "{} {}",
                string(&s, "backend"),
                s["model"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .unwrap_or("default model")
            )
        } else {
            self.agent.selection(role, None).1
        };
        let mut body = format!(
            "{marker}\nWorklane {stage} ({model})\n\n{}",
            string(result, "summary")
        );
        if nonempty(&result["findings"]) {
            body.push_str("\n\n");
            body.push_str(
                &list(&result["findings"])
                    .iter()
                    .map(|f| format!("- {}", f.as_str().unwrap_or_default()))
                    .collect::<Vec<_>>()
                    .join("\n"),
            );
        }
        if nonempty(&task["stack"]) {
            body.push_str("\n\nStack (oldest first):\n");
            body.push_str(
                &list(&task["stack"])
                    .iter()
                    .map(|e| {
                        format!(
                            "- #{}: {} @ {}",
                            e["number"],
                            string(e, "pr_url"),
                            string(e, "head_sha")
                        )
                    })
                    .collect::<Vec<_>>()
                    .join("\n"),
            );
            body.push_str(&format!(
                "\n- #{}: {} @ {} (base {})",
                task["number"],
                string(task, "pr_url"),
                string(task, "head_sha"),
                string(task, "base")
            ));
        }
        let (resource, target) = if nonempty(&task["pr_url"]) {
            ("pr", string(task, "pr_url"))
        } else {
            ("issue", task["number"].to_string())
        };
        let comments = self.board.transport.json(&[
            resource,
            "view",
            &target,
            "--repo",
            &self.state.repo,
            "--json",
            "comments",
        ])?;
        let mut posted = false;
        for comment in required_array(&comments["comments"], "handoff comments")? {
            let text = comment["body"].as_str().ok_or_else(|| {
                Error::new(
                    "github",
                    "Missing handoff comment body; inspect before retrying a write.",
                )
            })?;
            posted |= text.contains(&marker);
        }
        if !posted {
            self.board.transport.text(
                &[
                    resource,
                    "comment",
                    &target,
                    "--repo",
                    &self.state.repo,
                    "--body-file",
                    "-",
                ],
                Some(&body),
            )?;
        }
        Ok(())
    }
    pub fn agent_result(
        &self,
        task: &mut Value,
        stage: &str,
        tree: &Path,
        extra: &str,
        fd: RawFd,
    ) -> Result<Value> {
        self.before_turn(stage, task)?;
        if task.get("completed_turns").is_none() {
            task["completed_turns"] = json!({});
        }
        if task["completed_turns"].get(stage).is_none() {
            let result = if let Some(saved) = self.state.committed_turn(task, stage, tree)? {
                saved
            } else {
                self.agent.run(stage, task, tree, extra, fd)?
            };
            task["completed_turns"][stage] = result;
            self.state.save_turn(task, stage)?;
        }
        Ok(task["completed_turns"][stage].clone())
    }
    fn extra(&self) -> Result<String> {
        let path = self.root.join("AGENTS.md");
        if path.exists() {
            Ok(format!(
                "\nPrimary checkout instructions:\n{}",
                std::fs::read_to_string(path)?
            ))
        } else {
            Ok(String::new())
        }
    }
    pub fn build(&self, task: &mut Value, fd: RawFd) -> Result<()> {
        self.validate_stack(task)?;
        let tree = self.worktree(task, false)?;
        self.verify_stack_ancestry(task, &tree)?;
        self.refresh_review_threads(task, false)?;
        let result = self.agent_result(task, "build", &tree, &self.extra()?, fd)?;
        task["build_result"] = result.clone();
        self.state.save(task)?;
        if result["verdict"] != "pass" {
            return fail(format!(
                "Implementation blocked: {}",
                string(&result, "summary")
            ));
        }
        if nonempty(&result["findings"]) {
            return fail("Worker reported unresolved findings with a passing verdict.");
        }
        if flag(task, "question") {
            if nonempty(&result["pr_url"]) || nonempty(&result["head_sha"]) {
                return fail("Question-only work unexpectedly created a PR; inspect manually.");
            }
        } else {
            task["pr_url"] = result["pr_url"].clone();
            task["head_sha"] = result["head_sha"].clone();
            if !sha(&task["head_sha"]) {
                return fail("Worker did not provide an exact commit SHA.");
            }
            self.verify_pr(task, &string(task, "head_sha"))?;
            if command(&["git", "rev-parse", "HEAD"], &tree, None)? != string(task, "head_sha") {
                return fail("Worker checkout differs from the pushed PR head.");
            }
            if !command(&["git", "status", "--porcelain"], &tree, None)?.is_empty() {
                return fail("Worker left uncommitted changes.");
            }
            self.refresh_review_threads(task, true)?;
        }
        self.post(task, "implementation complete", &result)?;
        self.state.save(task)?;
        self.board.move_item(&task["item"], REVIEW, PROGRESS)?;
        task["phase"] = json!("awaiting_review");
        self.state.save(task)
    }
    fn request_rework(&self, task: &mut Value) -> Result<()> {
        if task["round"].as_i64().unwrap_or(0) >= 3 {
            return fail("Three review rounds failed; human inspection is needed.");
        }
        self.board.move_item(&task["item"], READY, REVIEW)?;
        task["phase"] = json!("rework");
        self.state.save(task)
    }
    fn requeue_conflict(&self, task: &mut Value, pr: &Value) -> Result<bool> {
        if pr["mergeable"] != "CONFLICTING" {
            return Ok(false);
        }
        let finding = format!(
            "PR has merge conflicts with {}; merge the latest base into the assigned branch, resolve conflicts, test and push without force.",
            string(task, "base")
        );
        let result = json!({"verdict":"changes_requested","pr_url":task["pr_url"],"head_sha":task["head_sha"],"summary":"Merge conflicts prevent review and human handoff.","findings":[finding]});
        task["review_result"] = result.clone();
        self.state.save(task)?;
        self.post(task, "merge conflict", &result)?;
        self.request_rework(task)?;
        Ok(true)
    }
    pub fn review(&self, task: &mut Value, fd: RawFd) -> Result<()> {
        let question = flag(task, "question");
        let tree = if question {
            self.worktree(task, false)?
        } else {
            let pr = self.verify_pr(task, &string(task, "head_sha"))?;
            if self.requeue_conflict(task, &pr)? {
                return Ok(());
            }
            self.worktree(task, true)?
        };
        self.refresh_review_threads(task, false)?;
        let extra = self.extra()?;
        let reuse = task["resume_stage"] == "finalize"
            && task["review_result"]["head_sha"] == task["head_sha"];
        let result = if reuse {
            task["review_result"].clone()
        } else {
            self.agent_result(task, "review", &tree, &extra, fd)?
        };
        task["review_result"] = result.clone();
        self.state.save(task)?;
        if !question {
            self.verify_pr(task, &string(task, "head_sha"))?;
            if result["head_sha"] != task["head_sha"] {
                return fail("Review verdict does not identify the current PR head.");
            }
            if command(&["git", "rev-parse", "HEAD"], &tree, None)? != string(task, "head_sha")
                || !command(&["git", "diff", "HEAD", "--"], &tree, None)?.is_empty()
            {
                return fail("Reviewer modified the revision under review.");
            }
        }
        self.post(task, "agent review", &result)?;
        match result["verdict"].as_str() {
            Some("blocked") => {
                return fail(format!("Review blocked: {}", string(&result, "summary")));
            }
            Some("changes_requested") => return self.request_rework(task),
            _ => (),
        }
        if nonempty(&result["findings"]) {
            return fail("Review passed with unresolved findings; human review is blocked.");
        }
        self.refresh_review_threads(task, true)?;
        task["phase"] = json!("finalizing");
        self.state.save(task)?;
        if !question {
            let handoff = self.agent_result(task, "finalize", &tree, &extra, fd)?;
            task["handoff_result"] = handoff.clone();
            self.state.save(task)?;
            if handoff["verdict"] != "pass"
                || handoff["head_sha"] != task["head_sha"]
                || nonempty(&handoff["findings"])
            {
                return fail(format!(
                    "Local human-testing handoff is incomplete: {}",
                    string(&handoff, "summary")
                ));
            }
            self.verify_pr(task, &string(task, "head_sha"))?;
            self.refresh_review_threads(task, true)?;
            self.post(task, "human testing handoff", &handoff)?;
        }
        if task.get("human_feedback_seen").is_none() {
            let cutoff = task["human_feedback_cutoff"]
                .as_str()
                .map(time)
                .transpose()?;
            let snapshot = self.human_feedback(task)?;
            let mut seen = json!({});
            for (k, v) in snapshot.as_object().unwrap() {
                if cutoff
                    .as_ref()
                    .is_some_and(|c| time(&string(v, "updated_at")).is_ok_and(|t| t <= *c))
                {
                    seen[k] = v.clone();
                }
            }
            task["human_feedback_seen"] = seen;
            self.state.save(task)?;
        }
        if !question {
            let pr = self.verify_pr(task, &string(task, "head_sha"))?;
            if self.requeue_conflict(task, &pr)? {
                return Ok(());
            }
            if pr["mergeable"] == "UNKNOWN" {
                return Err(Error::new(
                    "interrupted",
                    "GitHub is calculating PR mergeability; the saved handoff will be retried.",
                ));
            }
            if flag(&pr, "isDraft") {
                self.board.require_safe()?;
                self.board.transport.text(
                    &[
                        "pr",
                        "ready",
                        &string(task, "pr_url"),
                        "--repo",
                        &self.state.repo,
                    ],
                    None,
                )?;
                if flag(&self.verify_pr(task, &string(task, "head_sha"))?, "isDraft") {
                    return fail("PR is still a draft; human review is blocked.");
                }
            }
        }
        self.board.move_item(&task["item"], HUMAN, REVIEW)?;
        task["phase"] = json!("human_review");
        self.state.save(task)
    }
    pub fn execute(&self, task: &mut Value, review: bool, fd: RawFd) -> Result<()> {
        let mut outcome = if self.state.get_meta("shutdown", json!({}))?["immediate"] == true {
            Err(Error::new(
                "interrupted",
                "Immediate stop requested before workflow execution.",
            ))
        } else if review {
            self.review(task, fd)
        } else {
            self.build(task, fd)
        };
        if let Err(e) = &mut outcome {
            self.board.transport.record_quota(e)?;
            if matches!(e.kind, "interrupted" | "quota") {
                task["resume_stage"] = if e.kind == "quota" {
                    json!(if task["phase"] == "finalizing" {
                        "finalize"
                    } else if review {
                        "review"
                    } else {
                        "build"
                    })
                } else {
                    task.get("active_stage")
                        .cloned()
                        .unwrap_or(json!(if review { "review" } else { "build" }))
                };
                task["phase"] = json!("interrupted");
            } else {
                task["failed_phase"] = task["phase"].clone();
                task["phase"] = json!("blocked");
            }
            task["error"] = json!(e.to_string());
            self.state.append_log(
                None,
                "controller",
                &match e.kind {
                    "interrupted" => format!(
                        "#{} interrupted; saved session will resume on start: {e}",
                        task["number"]
                    ),
                    "quota" => format!(
                        "#{} waiting for GitHub quota; completed turns are saved.",
                        task["number"]
                    ),
                    _ => format!("#{} blocked: {e}", task["number"]),
                },
            )?;
        }
        remove(task, &["child_pid", "child_identity"]);
        if !matches!(task["phase"].as_str(), Some("interrupted" | "blocked")) {
            remove(task, &["resume_stage"]);
        }
        self.state.save(task)?;
        outcome
    }
    pub fn recover_tasks(&self) -> Result<()> {
        self.state.recover_runs()?;
        for mut task in self.state.all()? {
            if matches!(
                task["phase"].as_str(),
                Some("claiming" | "building" | "reviewing" | "finalizing")
            ) {
                let stage = match task["phase"].as_str() {
                    Some("reviewing") => "review",
                    Some("finalizing") => "finalize",
                    _ => "build",
                }
                .to_string();
                task["phase"] = json!("interrupted");
                task["resume_stage"] = json!(stage);
                remove(&mut task, &["child_pid", "child_identity"]);
                self.state.save(&mut task)?;
            }
        }
        Ok(())
    }
    pub fn recover(&self, items: &mut [Value], slot: &str) -> Result<Option<Value>> {
        self.board.require_safe()?;
        for mut task in self.state.all()? {
            let explicit_retry = task["phase"] == "retry";
            if nonempty(&task["stack_blocked"])
                || nonempty(&task["restack_pending"])
                || !matches!(task["phase"].as_str(), Some("interrupted" | "retry"))
            {
                continue;
            }
            let mut stage = task["resume_stage"]
                .as_str()
                .or_else(|| task["active_stage"].as_str())
                .unwrap_or("build")
                .to_string();
            if (slot == "reviewer") != (stage != "build") {
                continue;
            }
            let Some(current) = items.iter_mut().find(|i| i["id"] == task["item"]["id"]) else {
                task["phase"] = json!("blocked");
                task["error"] = json!("Saved item is no longer on this board.");
                self.state.save(&mut task)?;
                continue;
            };
            if !self.dependencies_ready(current, Some(&task))? {
                continue;
            }
            if stage == "build" {
                self.require_trusted_feedback(&task)?;
            }
            let status = string(current, "status").casefold();
            let expected = if stage == "build" { PROGRESS } else { REVIEW };
            if stage == "build" && status == READY.casefold() {
                self.board.move_item(current, PROGRESS, READY)?;
            } else if status != expected.casefold() {
                let phase = if stage == "build"
                    && status == REVIEW.casefold()
                    && task["build_result"]["verdict"] == "pass"
                {
                    "awaiting_review"
                } else if stage == "review"
                    && status == READY.casefold()
                    && task["review_result"]["verdict"] == "changes_requested"
                {
                    "rework"
                } else if status == HUMAN.casefold()
                    && (stage == "finalize" && task["handoff_result"]["verdict"] == "pass"
                        || flag(&task, "question") && task["review_result"]["verdict"] == "pass")
                {
                    "human_review"
                } else {
                    task["error"] = json!(format!(
                        "Saved work expected {expected}; board is {}. Human status changes are preserved.",
                        string(current, "status")
                    ));
                    "blocked"
                };
                task["phase"] = json!(phase);
                self.state.save(&mut task)?;
                continue;
            }
            let issue = self.issue(task["number"].as_i64().unwrap())?;
            if issue["state"] != "OPEN" {
                task["phase"] = json!("blocked");
                task["error"] = json!("Interrupted issue was closed; not resuming.");
                self.state.save(&mut task)?;
                continue;
            }
            if stage != "build" && !flag(&task, "question") {
                let pr = self.pr(&string(&task, "pr_url"))?;
                if pr["state"] != "OPEN"
                    || pr["headRefName"] != task["branch"]
                    || pr["baseRefName"] != task["base"]
                {
                    task["phase"] = json!("blocked");
                    task["error"] =
                        json!("Interrupted PR was closed or retargeted; inspect before retrying.");
                    self.state.save(&mut task)?;
                    continue;
                }
                if task["phase"] == "retry" && pr["headRefOid"] != task["head_sha"] {
                    task["head_sha"] = pr["headRefOid"].clone();
                    task["resume_stage"] = json!("review");
                    task["round"] = json!(task["round"].as_i64().unwrap_or(0) + 1);
                    remove(&mut task, &["completed_turns"]);
                    stage = "review".into();
                } else if pr["headRefOid"] != task["head_sha"] {
                    task["phase"] = json!("blocked");
                    task["error"] = json!(
                        "PR head changed during interruption; explicitly retry for a fresh review turn."
                    );
                    self.state.save(&mut task)?;
                    continue;
                }
            }
            task["item"] = current.clone();
            task["issue"] = issue;
            task["slot"] = json!(slot);
            task["phase"] = json!(match stage.as_str() {
                "build" => "building",
                "review" => "reviewing",
                _ => "finalizing",
            });
            remove(&mut task, &["error"]);
            if explicit_retry {
                self.state.save_assignment(&mut task)?;
            } else {
                self.state.save(&mut task)?;
            }
            return Ok(Some(task));
        }
        Ok(None)
    }
    pub fn pick(&self, item: &mut Value, slot: &str) -> Result<Option<Value>> {
        self.board.require_safe()?;
        let n = item["content"]["number"]
            .as_i64()
            .ok_or_else(|| Error::new("validation", "Missing issue number."))?;
        let mut previous = self.state.load(n)?;
        let review = slot == "reviewer";
        if nonempty(&previous["stack_blocked"])
            || nonempty(&previous["restack_pending"])
            || matches!(
                previous["phase"].as_str(),
                Some(
                    "done"
                        | "blocked"
                        | "interrupted"
                        | "building"
                        | "reviewing"
                        | "finalizing"
                        | "claiming"
                )
            )
        {
            return Ok(None);
        }
        if !self.dependencies_ready(item, Some(&previous))? {
            return Ok(None);
        }
        if !review && flag(&previous, "human_feedback_pending") {
            if self.feedback_unavailable.contains(&n)
                || !nonempty(&previous["human_feedback"])
                || !self.feedback_quiet(&previous)?
            {
                return Ok(None);
            }
            self.require_trusted_feedback(&previous)?;
            previous["human_feedback_seen"] = previous["human_feedback_snapshot"].clone();
            remove(&mut previous, &["human_feedback_pending"]);
        }
        let issue = self.issue(n)?;
        if issue["state"] != "OPEN" {
            return Ok(None);
        }
        let question = list(&issue["labels"])
            .iter()
            .any(|l| string(l, "name").casefold() == "question");
        let had_previous = nonempty(&previous);
        let mut task = if review {
            if !had_previous {
                let links = list(&item["linked pull requests"]);
                if links.len() != 1 && !question {
                    return fail(format!(
                        "#{n} needs exactly one linked PR before agent review."
                    ));
                }
                let pr = if let Some(url) = links.first().and_then(Value::as_str) {
                    Some(self.pr(url)?)
                } else {
                    None
                };
                previous = if let Some(p) = pr {
                    json!({"number":n,"branch":p["headRefName"],"base":p["baseRefName"],"pr_url":p["url"],"head_sha":p["headRefOid"],"round":0})
                } else {
                    json!({"number":n,"branch":format!("codex/{n}-worklane"),"base":self.base,"pr_url":"","head_sha":"","round":0})
                };
            } else if previous["phase"] == "retry" && nonempty(&previous["pr_url"]) {
                let pr = self.pr(&string(&previous, "pr_url"))?;
                if pr["state"] != "OPEN"
                    || pr["headRefName"] != previous["branch"]
                    || pr["baseRefName"] != previous["base"]
                {
                    return fail("Cannot retry a closed or retargeted PR.");
                }
                previous["head_sha"] = pr["headRefOid"].clone();
            }
            let mut t = previous.clone();
            t["round"] = json!(previous["round"].as_i64().unwrap_or(0) + 1);
            t["phase"] = json!("reviewing");
            t
        } else {
            if !had_previous && nonempty(&item["linked pull requests"]) {
                return fail(
                    "Ready issue already has a linked PR; inspect it or move it to Agent review instead of creating duplicate work.",
                );
            }
            let mut t = json!({"number":n,"branch":format!("codex/{n}-worklane"),"base":self.base,"round":0,"pr_url":"","head_sha":""});
            for (k, v) in previous.as_object().unwrap() {
                t[k] = v.clone();
            }
            t["phase"] = json!("claiming");
            t
        };
        if !had_previous && nonempty(&item["stack_plan"]) {
            task["stack"] = item["stack_plan"].clone();
            let edge = task["stack"].as_array().unwrap().last().unwrap().clone();
            task["base"] = edge["branch"].clone();
            task["base_sha"] = edge["head_sha"].clone();
            task["default_base"] = json!(self.base);
        }
        if task.get("human_feedback_cutoff").is_none() {
            task["human_feedback_cutoff"] = previous
                .get("started_at")
                .cloned()
                .unwrap_or(json!(self.state.deps.clock.now()));
        }
        task["item"] = item.clone();
        task["issue"] = issue;
        task["question"] = json!(question);
        task["slot"] = json!(slot);
        task["started_at"] = json!(self.state.deps.clock.now());
        remove(&mut task, &["completed_turns", "error"]);
        self.state.save_assignment(&mut task)?;
        if !review {
            if let Err(e) = self.board.move_item(item, PROGRESS, READY) {
                if e.kind == "quota" {
                    task["phase"] = json!("interrupted");
                    task["resume_stage"] = json!("build");
                    self.state.save(&mut task)?;
                }
                return Err(e);
            }
            task["phase"] = json!("building");
            self.state.save(&mut task)?;
        }
        Ok(Some(task))
    }
    pub fn reconcile_completed(&self, items: &[Value], active: &BTreeSet<i64>) -> Result<()> {
        for mut task in self.state.all()? {
            let n = task["number"].as_i64().unwrap();
            if matches!(
                task["phase"].as_str(),
                Some("done" | "building" | "reviewing" | "finalizing" | "claiming")
            ) || active.contains(&n)
                || !nonempty(&task["pr_url"])
            {
                continue;
            }
            let current = items.iter().find(|i| i["id"] == task["item"]["id"]);
            let status = current
                .map(|i| string(i, "status").casefold())
                .unwrap_or("missing".into());
            if task["phase"] != "human_review" && current.is_some() && status != "done" {
                continue;
            }
            let key = format!("completion:{n}");
            let previous = self.state.get_meta(&key, json!({}))?;
            if previous["status"] == status {
                if let Some(p) = previous["polled_at"].as_str() {
                    if (time(&self.state.deps.clock.now())? - time(p)?).num_seconds() < 300 {
                        continue;
                    }
                }
            }
            let result = (|| -> Result<()> {
                let pr = self.pr(&string(&task, "pr_url"))?;
                if pr["headRefName"] != task["branch"] || pr["baseRefName"] != task["base"] {
                    return fail("Assigned PR was retargeted; completion needs inspection.");
                }
                if pr["state"] == "MERGED" {
                    task["phase"] = json!("done");
                    task["completion_source"] = json!("merged_pr");
                    task["merged_head_sha"] = pr["headRefOid"].clone();
                    task["completed_at"] = pr
                        .get("mergedAt")
                        .filter(|v| nonempty(v))
                        .cloned()
                        .unwrap_or(json!(self.state.deps.clock.now()));
                    remove(
                        &mut task,
                        &[
                            "error",
                            "failed_phase",
                            "blocker",
                            "resume_stage",
                            "human_feedback_pending",
                            "child_pid",
                            "child_identity",
                        ],
                    );
                    self.state.save(&mut task)?;
                }
                self.state.set_meta(
                    &key,
                    &json!({"status":status,"polled_at":self.state.deps.clock.now()}),
                )
            })();
            if let Err(e) = result {
                if e.kind == "quota" {
                    return Err(e);
                }
                self.state.append_log(
                    None,
                    "controller-error",
                    &format!("#{n} completion unavailable: {e}"),
                )?;
            }
        }
        Ok(())
    }
    pub fn reconcile_waiting(&self, items: &mut [Value], active: &BTreeSet<i64>) -> Result<()> {
        for mut task in self.state.all()? {
            if task["phase"] != "awaiting_review"
                || active.contains(&task["number"].as_i64().unwrap())
            {
                continue;
            }
            let current = items.iter_mut().find(|i| i["id"] == task["item"]["id"]);
            if current
                .as_ref()
                .is_some_and(|i| string(i, "status").casefold() == REVIEW.casefold())
            {
                continue;
            }
            let status = if let Some(i) = current.as_ref() {
                self.board.item_status(i)?
            } else {
                None
            };
            if status.as_deref() == Some(&REVIEW.casefold()) {
                if let Some(i) = current {
                    i["status"] = json!(REVIEW);
                }
                continue;
            }
            task["phase"] = json!("blocked");
            task["failed_phase"] = json!("awaiting_review");
            task["resume_stage"] = json!("build");
            task["blocker"] = json!("board_status_drift");
            task["error"] = json!(format!(
                "Completed implementation expected {REVIEW}; board is {}. Check project automations/manual edits, then run worklane retry {} to revalidate the saved build. Board status was preserved.",
                status
                    .as_deref()
                    .filter(|s| !s.is_empty())
                    .unwrap_or("missing/closed"),
                task["number"]
            ));
            self.state.save(&mut task)?;
        }
        Ok(())
    }
    pub fn restack(&self, task: &mut Value, remaining: &[Value], item: &mut Value) -> Result<()> {
        self.board.require_safe()?;
        let target = remaining
            .last()
            .map(|e| string(e, "branch"))
            .unwrap_or_else(|| task["default_base"].as_str().unwrap_or(&self.base).into());
        command(&["git", "fetch", "origin"], &self.root, None)?;
        let remote = command(
            &["git", "rev-parse", &format!("origin/{target}")],
            &self.root,
            None,
        )?;
        let target_sha = remaining
            .last()
            .map(|e| string(e, "head_sha"))
            .unwrap_or_else(|| remote.clone());
        if remote != target_sha {
            return fail("Restack target moved before fetch; retry after upstream review.");
        }
        let tree = PathBuf::from(string(task, "worker_worktree"));
        if !tree.is_dir() {
            return fail("Restore the saved worker worktree before restacking.");
        }
        if !command(&["git", "status", "--porcelain"], &tree, None)?.is_empty() {
            return fail(
                "Worker worktree has changes; preserving them until restacking can proceed cleanly.",
            );
        }
        if command(&["git", "branch", "--show-current"], &tree, None)? != string(task, "branch") {
            return fail("Worker worktree branch changed; inspect before restacking.");
        }
        let head = command(&["git", "rev-parse", "HEAD"], &tree, None)?;
        if !nonempty(&task["restack_pending"]) {
            if nonempty(&task["head_sha"]) && task["head_sha"] != head {
                return fail("Child head changed outside Worklane; inspect before restacking.");
            }
            task["restack_pending"] = json!({"old_base":task["base"],"old_head":head,"target":target,"target_sha":target_sha,"stack":remaining,"started_at":self.state.deps.clock.now()});
            self.state.save(task)?;
        }
        let pending = task["restack_pending"].clone();
        if pending["target"] != target
            || pending["target_sha"] != target_sha
            || pending["stack"] != json!(remaining)
        {
            return fail(
                "Upstream changed during an interrupted restack; inspect saved restack intent.",
            );
        }
        if head
            != pending["result_head"]
                .as_str()
                .unwrap_or(pending["old_head"].as_str().unwrap_or_default())
        {
            let parents = command(&["git", "show", "-s", "--format=%P", "HEAD"], &tree, None)?;
            if parents.split_whitespace().collect::<Vec<_>>()
                != vec![
                    pending["old_head"].as_str().unwrap_or_default(),
                    &target_sha,
                ]
            {
                return fail("Local head changed during restack; preserving it for inspection.");
            }
        }
        if pending.get("result_head").is_none() {
            if command(
                &["git", "merge", "--no-ff", "--no-edit", &target_sha],
                &tree,
                None,
            )
            .is_err()
            {
                command(&["git", "merge", "--abort"], &tree, None)?;
                return fail(
                    "Restack conflicts; merge aborted and original work preserved. Resolve in the saved worker session.",
                );
            }
            task["restack_pending"]["result_head"] =
                json!(command(&["git", "rev-parse", "HEAD"], &tree, None)?);
            self.state.save(task)?;
        }
        let pending = task["restack_pending"].clone();
        if nonempty(&task["pr_url"]) {
            let pr = self.pr(&string(task, "pr_url"))?;
            if pr["state"] != "OPEN"
                || pr["headRefName"] != task["branch"]
                || ![&pending["old_head"], &pending["result_head"]].contains(&&pr["headRefOid"])
                || ![&pending["old_base"], &json!(target)].contains(&&pr["baseRefName"])
            {
                return fail("Child PR changed during restack; inspect before pushing.");
            }
            self.board.require_safe()?;
            command(
                &[
                    "git",
                    "push",
                    "origin",
                    &format!("HEAD:refs/heads/{}", string(task, "branch")),
                ],
                &tree,
                None,
            )?;
            self.board.require_safe()?;
            self.board.transport.text(
                &[
                    "pr",
                    "edit",
                    &string(task, "pr_url"),
                    "--repo",
                    &self.state.repo,
                    "--base",
                    &target,
                ],
                None,
            )?;
            let pr = self.pr(&string(task, "pr_url"))?;
            if pr["headRefOid"] != pending["result_head"] || pr["baseRefName"] != target {
                return fail("Restack push/retarget is not yet verified; saved intent retained.");
            }
            let status = self.board.item_status(item)?;
            if status.as_deref() == Some(&HUMAN.casefold()) {
                self.board.move_item(item, REVIEW, HUMAN)?;
                item["status"] = json!(REVIEW);
            } else if status.as_deref() != Some(&REVIEW.casefold()) {
                return fail(
                    "Restacked PR needs Agent review; move the board item there before retrying.",
                );
            }
        }
        if task.get("stack_history").is_none() {
            task["stack_history"] = json!([]);
        }
        let history = json!({"stack":task["stack"],"base":task["base"],"base_sha":task["base_sha"],"head_sha":pending["old_head"],"restacked_at":self.state.deps.clock.now()});
        task["stack_history"]
            .as_array_mut()
            .ok_or_else(|| Error::new("validation", "Invalid stack history."))?
            .push(history);
        task["stack"] = json!(remaining);
        task["base"] = json!(target);
        task["base_sha"] = json!(target_sha);
        task["head_sha"] = pending["result_head"].clone();
        remove(
            task,
            &[
                "completed_turns",
                "review_result",
                "handoff_result",
                "stack_blocked",
                "restack_pending",
            ],
        );
        if nonempty(&task["pr_url"]) {
            task["phase"] = json!("awaiting_review");
            task["round"] = json!(task["round"].as_i64().unwrap_or(0) + 1);
        }
        self.state.save(task)
    }
    pub fn reconcile_stacks(&mut self, items: &mut [Value], active: &BTreeSet<i64>) -> Result<()> {
        self.stack_items = items.to_vec();
        let mut tasks = self.state.all()?;
        tasks.sort_by_key(|t| list(&t["stack"]).len());
        for mut task in tasks {
            let n = task["number"].as_i64().unwrap();
            if (!nonempty(&task["stack"]) && !nonempty(&task["restack_pending"]))
                || task["phase"] == "done"
                || active.contains(&n)
            {
                continue;
            }
            let Some(item) = items.iter_mut().find(|i| i["content"]["number"] == n) else {
                continue;
            };
            let key = format!("stack_poll:{n}");
            let last = self.state.get_meta(&key, json!(0))?.as_f64().unwrap_or(0.);
            let now = time(&self.state.deps.clock.now())?.timestamp() as f64;
            if !nonempty(&task["stack_blocked"])
                && !nonempty(&task["restack_pending"])
                && now - last < 300.
            {
                continue;
            }
            let result = (|| -> Result<()> {
                let mut remaining = Vec::new();
                for e in list(&task["stack"]) {
                    let pr = self.pr(&string(&e, "pr_url"))?;
                    if pr["state"] == "MERGED" {
                        if pr["headRefOid"] != e["head_sha"] {
                            return fail(format!(
                                "Merged parent #{} differs from the pinned reviewed revision.",
                                e["number"]
                            ));
                        }
                        continue;
                    }
                    if pr["state"] != "OPEN" {
                        return fail(format!("Parent #{} closed without merging.", e["number"]));
                    }
                    let (_, pr) = self.reviewed_parent(e["number"].as_i64().unwrap_or(0))?;
                    remaining.push(json!({"number":e["number"],"pr_url":e["pr_url"],"branch":pr["headRefName"],"base":pr["baseRefName"],"head_sha":pr["headRefOid"],"reviewed_head":pr["headRefOid"]}));
                }
                if json!(remaining) != task["stack"] || nonempty(&task["restack_pending"]) {
                    self.restack(&mut task, &remaining, item)?;
                } else {
                    remove(&mut task, &["stack_blocked"]);
                    self.state.save(&mut task)?;
                }
                self.state.set_meta(&key, &json!(now))
            })();
            if let Err(e) = result {
                if e.kind == "quota" {
                    return Err(e);
                }
                task["stack_blocked"] = json!(e.to_string());
                self.state.save(&mut task)?;
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod recovery_tests {
    use super::*;
    use crate::transport::GitHubTransport;
    fn workflow(state: &StateStore) -> Workflow {
        let mut transport = GitHubTransport::new(state, &state.path).unwrap();
        let gh = state.path.join("test-gh");
        std::fs::write(&gh, "#!/bin/sh\ncat >/dev/null\nprintf '%s' '{\"data\":{\"node\":{\"public\":false,\"viewerCanUpdate\":true},\"repository\":{\"viewerPermission\":\"ADMIN\"}}}'\n").unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&gh, std::fs::Permissions::from_mode(0o700)).unwrap();
        transport.executable = gh;
        let board = Board {
            repo: state.repo.clone(),
            transport,
            project: json!({}),
            owner: "owner".into(),
            number: 1,
            field: json!({}),
            options: Default::default(),
            required_statuses: Vec::new(),
            invocation: Default::default(),
            security: Value::Null,
            public_warning_shown: Default::default(),
        };
        let mut agent = AgentHarness::new(&state.path, state, &json!({})).unwrap();
        // A regression must fail before any provider invocation.
        agent.required_backend = Some("not-a-test-provider".into());
        Workflow::new(
            state.clone(),
            board,
            agent,
            state.path.clone(),
            "main".into(),
        )
    }
    #[test]
    fn committed_turn_survives_crash_before_workflow_task_commit_without_another_agent_turn() {
        let root = tempfile::tempdir().unwrap();
        let state = StateStore::open("owner/repo", Some(root.path())).unwrap();
        let mut task = json!({"number":1,"phase":"building"});
        state.save(&mut task).unwrap();
        let id = state.begin_run(&mut task, "build", &state.path).unwrap();
        state
            .attach_session(&id, "ses_committed", "m", "opencode")
            .unwrap();
        let result = json!({"verdict":"pass","pr_url":"","head_sha":"","summary":"committed before crash","findings":[]});
        state.advance_run(&id, Some(&result)).unwrap();
        let mut recovered = state.load(1).unwrap();
        assert!(recovered.get("completed_turns").is_none());
        let workflow = workflow(&state);
        workflow.recover_tasks().unwrap();
        assert_eq!(
            workflow
                .agent_result(&mut recovered, "build", &state.path, "", -1)
                .unwrap(),
            result
        );
        assert_eq!(state.runs(Some(1)).unwrap().len(), 1);
    }
    #[test]
    fn finalizing_phase_cannot_recover_as_previous_reviewer_role() {
        let root = tempfile::tempdir().unwrap();
        let state = StateStore::open("owner/repo", Some(root.path())).unwrap();
        let mut task = json!({"number":1,"phase":"finalizing","active_stage":"review"});
        state.save(&mut task).unwrap();
        workflow(&state).recover_tasks().unwrap();
        assert_eq!(state.load(1).unwrap()["resume_stage"], "finalize");
    }
}
