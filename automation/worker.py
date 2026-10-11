#!/usr/bin/env python3
"""
Jules Automation Worker

Orchestrates Jules coding sessions and GitHub merge gates for task execution.
Handles task backlog execution, session reconciliation, failure history & retries,
PR CI & review status monitoring, and auto-merge requests.
"""

import json
import os
import sys
import time
import subprocess
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
import requests


# --- API Clients ---

class JulesClient:
    """Client for Jules REST API."""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or os.getenv("JULES_API_KEY", "")
        self.base_url = (base_url or os.getenv("JULES_API_URL", "https://jules.googleapis.com")).rstrip("/")

    def _get_headers(self) -> Dict[str, str]:
        return {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }

    def create_session(self, prompt: str, title: str, repository: Optional[str] = None) -> Dict[str, Any]:
        """POST /v1alpha/sessions to start a session."""
        url = f"{self.base_url}/v1alpha/sessions"
        payload = {
            "title": title,
            "prompt": prompt,
        }
        if repository:
            payload["repository"] = repository

        response = requests.post(url, headers=self._get_headers(), json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    def get_session(self, session_id: str) -> Dict[str, Any]:
        """GET /v1alpha/sessions/{sessionId} to retrieve session status and outputs."""
        url = f"{self.base_url}/v1alpha/sessions/{session_id}"
        response = requests.get(url, headers=self._get_headers(), timeout=30)
        response.raise_for_status()
        return response.json()

    def list_sessions(self) -> List[Dict[str, Any]]:
        """GET /v1alpha/sessions to retrieve recent sessions for reconciliation."""
        url = f"{self.base_url}/v1alpha/sessions"
        response = requests.get(url, headers=self._get_headers(), timeout=30)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            return data
        return data.get("sessions", [])


class GitHubClient:
    """Helper for GitHub API and CLI interactions."""

    def __init__(self, token: Optional[str] = None, repository: Optional[str] = None):
        self.token = token or os.getenv("AUTOMATION_TOKEN") or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN", "")
        self.repository = repository or os.getenv("GITHUB_REPOSITORY", "")

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }

    def get_pr(self, pr_number: int) -> Optional[Dict[str, Any]]:
        """Fetch Pull Request status from GitHub API."""
        if not self.repository or not self.token:
            return None
        url = f"https://api.github.com/repos/{self.repository}/pulls/{pr_number}"
        response = requests.get(url, headers=self._get_headers(), timeout=30)
        if response.status_code == 200:
            return response.json()
        return None

    def get_pr_checks(self, pr_number: int) -> Dict[str, Any]:
        """
        Evaluates CI check status for a PR.
        Returns dict with status: 'success', 'failure', or 'pending'.
        """
        pr = self.get_pr(pr_number)
        if not pr:
            return {"status": "unknown"}

        head_sha = pr.get("head", {}).get("sha")
        if not head_sha:
            return {"status": "unknown"}

        url = f"https://api.github.com/repos/{self.repository}/commits/{head_sha}/check-runs"
        response = requests.get(url, headers=self._get_headers(), timeout=30)
        if response.status_code != 200:
            return {"status": "unknown"}

        check_runs = response.json().get("check_runs", [])
        if not check_runs:
            return {"status": "success"}

        statuses = [run.get("status") for run in check_runs]
        conclusions = [run.get("conclusion") for run in check_runs]

        if any(s in ["in_progress", "queued", "requested"] for s in statuses):
            return {"status": "pending"}

        if any(c in ["failure", "timed_out", "cancelled"] for c in conclusions):
            return {"status": "failure"}

        if all(c in ["success", "neutral", "skipped"] for c in conclusions):
            return {"status": "success"}

        return {"status": "pending"}

    def get_pr_review_status(self, pr_number: int) -> str:
        """
        Check PR review status.
        Returns: 'approved', 'changes_requested', or 'pending'.
        """
        if not self.repository or not self.token:
            return "approved"  # Default if no repo/token configured in local tests

        url = f"https://api.github.com/repos/{self.repository}/pulls/{pr_number}/reviews"
        response = requests.get(url, headers=self._get_headers(), timeout=30)
        if response.status_code != 200:
            return "approved"

        reviews = response.json()
        if not reviews:
            return "pending"

        states = [r.get("state") for r in reviews]
        if "CHANGES_REQUESTED" in states:
            return "changes_requested"
        if "APPROVED" in states:
            return "approved"

        return "pending"

    def enable_auto_merge(self, pr_number: int) -> bool:
        """Attempts to enable auto-merge on a PR via gh CLI or GitHub GraphQL API."""
        if not self.repository:
            return False

        # Attempt 1: gh CLI with injected token environment
        try:
            cmd = [
                "gh", "pr", "merge", str(pr_number),
                "--repo", self.repository,
                "--auto", "--squash"
            ]
            env = os.environ.copy()
            if self.token:
                env["GH_TOKEN"] = self.token
                env["GITHUB_TOKEN"] = self.token
            res = subprocess.run(cmd, capture_output=True, text=True, env=env)
            if res.returncode == 0:
                return True
        except Exception:
            pass

        # Attempt 2: GraphQL API fallback if gh CLI failed
        if self.token:
            try:
                pr = self.get_pr(pr_number)
                node_id = pr.get("node_id") if pr else None
                if node_id:
                    mutation = """
                    mutation($prId: ID!) {
                        enablePullRequestAutoMerge(input: {pullRequestId: $prId, mergeMethod: SQUASH}) {
                            pullRequest { id }
                        }
                    }
                    """
                    resp = requests.post(
                        "https://api.github.com/graphql",
                        headers=self._get_headers(),
                        json={"query": mutation, "variables": {"prId": node_id}},
                        timeout=30
                    )
                    if resp.status_code == 200 and "errors" not in resp.json():
                        return True
            except Exception:
                pass

        return False


# --- Memory & Self-Reflection Manager ---

class MemoryManager:
    """Manages persistent repository memory and post-mortem lessons learned."""

    def __init__(self, memory_file: str = "automation/memory.json", max_items: int = 20):
        self.memory_file = memory_file
        self.max_items = max_items
        self.memories: List[Dict[str, Any]] = []
        self.load_memory()

    def load_memory(self):
        if self.memory_file and os.path.exists(self.memory_file):
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.memories = data if isinstance(data, list) else []
            except Exception as e:
                print(f"[Warning] Failed to load memory from {self.memory_file}: {e}")
                self.memories = []
        else:
            self.memories = []

    def save_memory(self):
        if self.memory_file:
            os.makedirs(os.path.dirname(self.memory_file), exist_ok=True)
            try:
                with open(self.memory_file, "w", encoding="utf-8") as f:
                    json.dump(self.memories, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"[Warning] Failed to save memory to {self.memory_file}: {e}")

    def add_lesson(self, task_id: str, lesson: str, trigger_reason: str = ""):
        """Adds a lesson learned, deduplicating and trimming to max_items."""
        if not lesson or not lesson.strip():
            return

        clean_lesson = lesson.strip()
        # Avoid duplicate lessons for the same task
        for m in self.memories:
            if m.get("task_id") == task_id and m.get("lesson") == clean_lesson:
                return

        entry = {
            "task_id": task_id,
            "lesson": clean_lesson,
            "trigger_reason": trigger_reason.strip() if trigger_reason else "",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.memories.append(entry)
        if len(self.memories) > self.max_items:
            self.memories = self.memories[-self.max_items:]
        self.save_memory()
        print(f"[Memory Updated] Recorded new lesson from {task_id}: {clean_lesson}")

    def get_recent_lessons(self, limit: int = 5) -> List[Dict[str, Any]]:
        return self.memories[-limit:]


# --- Task Manager Orchestrator ---

class AutomationWorker:
    """Task orchestrator implementing state reconciliation and merge gates."""

    def __init__(
        self,
        tasks_file: str = "automation/tasks.json",
        config_file: str = "automation/config.json",
        memory_file: str = "automation/memory.json",
        jules_client: Optional[JulesClient] = None,
        github_client: Optional[GitHubClient] = None,
        memory_manager: Optional[MemoryManager] = None
    ):
        self.tasks_file = tasks_file
        self.config_file = config_file
        self.memory_file = memory_file
        self.jules_client = jules_client or JulesClient()
        self.github_client = github_client or GitHubClient()
        self.tasks: List[Dict[str, Any]] = []
        self.config: Dict[str, Any] = {}
        self.load_config()
        self.load_tasks()
        self.memory_manager = memory_manager or MemoryManager(
            memory_file=self.memory_file,
            max_items=self.config.get("max_memory_items", 20)
        )

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    self.config = json.load(f)
            except Exception as e:
                print(f"[Warning] Failed to load config from {self.config_file}: {e}")
                self.config = {}
        else:
            self.config = {}

    def load_tasks(self):
        if os.path.exists(self.tasks_file):
            with open(self.tasks_file, "r") as f:
                self.tasks = json.load(f)
        else:
            self.tasks = []

    def save_tasks(self):
        os.makedirs(os.path.dirname(self.tasks_file), exist_ok=True)
        with open(self.tasks_file, "w") as f:
            json.dump(self.tasks, f, indent=2)

    def reconcile_sessions(self):
        """
        Rule 1: Don't duplicate sessions after a crash.
        Reconcile recent Jules sessions with pending/in_progress tasks.
        Excludes sessions already in task failure_history or terminal failed/succeeded state.
        """
        try:
            recent_sessions = self.jules_client.list_sessions()
        except Exception as e:
            print(f"[Warning] Unable to fetch recent sessions for reconciliation: {e}")
            return

        for task in self.tasks:
            # Reconcile if task is in_progress without active_session_id or pending
            if task["status"] in ["pending", "in_progress"] and not task.get("active_session_id"):
                failed_session_ids = {
                    f.get("session_id") for f in task.get("failure_history", []) if f.get("session_id")
                }

                candidate_sessions = []
                for s in recent_sessions:
                    s_id = s.get("id") or s.get("sessionId") or s.get("name")
                    title = s.get("title", "")
                    s_state = (s.get("state") or "").upper()

                    # Ignore session if already in failure history
                    if s_id in failed_session_ids:
                        continue

                    # Ignore session if in terminal state
                    if s_state in ["FAILED", "ERROR", "SUCCEEDED", "COMPLETED"]:
                        continue

                    if task["id"] in title or task["title"] in title:
                        candidate_sessions.append(s)

                if len(candidate_sessions) == 1:
                    session = candidate_sessions[0]
                    s_id = session.get("id") or session.get("sessionId") or session.get("name")
                    task["active_session_id"] = s_id
                    task["status"] = "in_progress"
                    print(f"[Reconciled] Task {task['id']} re-linked to active session {s_id}")
                elif len(candidate_sessions) > 1:
                    print(f"[Reconciliation Conflict] Task {task['id']} matched multiple active sessions: {[s.get('id') for s in candidate_sessions]}")
                    task["status"] = "blocked"
                    task["blocked_reason"] = "Ambiguous active session matching during crash recovery"

    def is_dependency_satisfied(self, task: Dict[str, Any]) -> bool:
        deps = task.get("dependencies", [])
        if not deps:
            return True

        task_map = {t["id"]: t for t in self.tasks}
        for dep_id in deps:
            dep_task = task_map.get(dep_id)
            if not dep_task or dep_task.get("status") != "done":
                return False
        return True

    def build_task_prompt(self, task: Dict[str, Any]) -> str:
        prompt_lines = []

        # Inject project context/guidelines from config if present
        project_context = self.config.get("project_context") or self.config.get("context")
        if project_context:
            prompt_lines.extend([
                "=== Project Context & Guidelines ===",
                str(project_context).strip(),
                "====================================\n"
            ])

        # Inject lessons learned from repository memory bank (Phase 1 Self-Improvement)
        if self.config.get("enable_memory", True) and self.memory_manager:
            inject_count = self.config.get("inject_memory_count", 3)
            recent_lessons = self.memory_manager.get_recent_lessons(limit=inject_count)
            if recent_lessons:
                prompt_lines.extend([
                    "=== Repository Lessons Learned (Avoid repeating past mistakes) ==="
                ])
                for m in recent_lessons:
                    prompt_lines.append(f"- [{m.get('task_id')}]: {m.get('lesson')}")
                prompt_lines.extend([
                    "===================================================================\n"
                ])

        prompt_lines.extend([
            f"Task ID: {task['id']}",
            f"Title: {task['title']}",
            f"Description: {task['description']}",
            "Acceptance Criteria:",
        ])
        for ac in task.get("acceptance_criteria", []):
            prompt_lines.append(f"- {ac}")

        # Rule 2: Include failure details if this is a retry attempt
        failures = task.get("failure_history", [])
        if failures:
            prompt_lines.append("\nPrevious Attempt Failures:")
            for idx, fail in enumerate(failures, 1):
                prompt_lines.append(
                    f"Attempt {idx} Failed at {fail.get('timestamp')}:\n"
                    f"  Session ID: {fail.get('session_id')}\n"
                    f"  Error / Reason: {fail.get('reason')}"
                )
            prompt_lines.append("\nPlease address the issues that caused the prior attempts to fail.")

        return "\n".join(prompt_lines)

    def start_task_session(self, task: Dict[str, Any]):
        """Starts a new session for a task."""
        prompt = self.build_task_prompt(task)
        title = f"[{task['id']}] {task['title']}"
        repo = self.github_client.repository or None

        try:
            session = self.jules_client.create_session(prompt=prompt, title=title, repository=repo)
            session_id = session.get("id") or session.get("sessionId") or session.get("name")
            task["active_session_id"] = session_id
            task["status"] = "in_progress"
            print(f"[Started] Created session {session_id} for Task {task['id']}")
        except Exception as e:
            print(f"[Error] Failed to start session for Task {task['id']}: {e}")

    def update_task_session_status(self, task: Dict[str, Any]):
        """Polls active session status and extracts PR URL/Number when complete."""
        session_id = task.get("active_session_id")
        if not session_id:
            return

        try:
            session = self.jules_client.get_session(session_id)
            state = session.get("state", "").upper()

            if state in ["SUCCEEDED", "COMPLETED", "DONE"]:
                pr_url = session.get("pr_url") or session.get("pullRequestUrl")
                pr_number = session.get("pr_number") or session.get("pullRequestNumber")

                # Check output field if pr info in outputs
                outputs = session.get("outputs", {})
                if not pr_url and isinstance(outputs, dict):
                    pr_url = outputs.get("pull_request_url") or outputs.get("pr_url")
                    pr_number = outputs.get("pull_request_number") or outputs.get("pr_number")

                if pr_url:
                    task["pr_url"] = pr_url
                if pr_number:
                    task["pr_number"] = int(pr_number)

                task["status"] = "waiting_for_ci"
                print(f"[Session Complete] Task {task['id']} completed session {session_id}. PR: {task.get('pr_url')}")

            elif state in ["FAILED", "ERROR"]:
                print(f"[Session Failed] Task {task['id']} session {session_id} failed.")
                self.record_task_failure(task, session_id, f"Jules session state reached {state}")

        except Exception as e:
            print(f"[Error] Failed to poll session {session_id} for task {task['id']}: {e}")

    def record_task_failure(self, task: Dict[str, Any], session_id: Optional[str], reason: str):
        """
        Rule 2: Don't retry using failed session. Clear active session pointer,
        record failure history, and increment attempts.
        """
        failures = task.setdefault("failure_history", [])
        failures.append({
            "session_id": session_id,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

        # Clear active session pointer
        task["active_session_id"] = None

        limit = task.get("max_attempts") or self.config.get("default_max_attempts", 3)
        if len(failures) < limit:
            print(f"[Retry Scheduled] Task {task['id']} attempt {len(failures)} failed. Retrying (Max: {limit}).")
            task["status"] = "pending"
        else:
            print(f"[Task Blocked] Task {task['id']} exceeded max attempts ({limit}).")
            task["status"] = "blocked"
            task["blocked_reason"] = f"Exceeded max attempts ({limit}). Last reason: {reason}"
            if self.config.get("enable_memory", True) and self.memory_manager:
                self.memory_manager.add_lesson(
                    task_id=task["id"],
                    lesson=f"Avoid recurring pitfall: Task was blocked after repeated failures: {reason[:100]}.",
                    trigger_reason=reason
                )

    def on_task_completed(self, task: Dict[str, Any]):
        """
        Phase 1 Self-Improvement:
        Callback when a task transitions to 'done'. If the task previously experienced
        failures before succeeding, synthesize a lesson learned and save to memory bank.
        """
        if not self.config.get("enable_memory", True) or not self.memory_manager:
            return

        failures = task.get("failure_history", [])
        if failures:
            lesson = self.synthesize_lesson_from_recovery(task)
            if lesson:
                last_reason = failures[-1].get("reason", "")
                self.memory_manager.add_lesson(
                    task_id=task["id"],
                    lesson=lesson,
                    trigger_reason=last_reason
                )

    def synthesize_lesson_from_recovery(self, task: Dict[str, Any]) -> str:
        """
        Synthesizes a concise takeaway from a resolved issue.
        """
        failures = task.get("failure_history", [])
        if not failures:
            return ""

        last_reason = failures[-1].get("reason", "")
        if "CI checks failed" in last_reason:
            return f"Ensure all tests and assertions match expected schema before concluding PR (resolved after CI failure in {task['id']})."
        elif "Review requested changes" in last_reason:
            return f"Adhere strictly to project conventions and review feedback (resolved after review request in {task['id']})."
        elif "closed without merging" in last_reason:
            return f"Avoid closing PRs prematurely; ensure PR meets all merge criteria (resolved in {task['id']})."
        else:
            return f"Resolved prior issue: {last_reason[:100]} (fixed in {task['id']})."

    def check_pr_and_merge_gates(self, task: Dict[str, Any]):
        """
        Rule 3: Don't treat pending checks as failed checks.
        Evaluates GitHub PR status, CI status, review status, and requests auto-merge.
        """
        pr_number = task.get("pr_number")
        if not pr_number:
            # If no PR number, assume waiting_for_ci if PR URL exists, else fallback
            return

        pr = self.github_client.get_pr(pr_number)
        if pr and pr.get("merged"):
            task["status"] = "done"
            print(f"[Task Done] PR #{pr_number} merged for Task {task['id']}")
            self.on_task_completed(task)
            return

        if pr and pr.get("state") == "closed" and not pr.get("merged"):
            self.record_task_failure(task, task.get("active_session_id"), f"PR #{pr_number} was closed without merging.")
            return

        # Check CI checks status
        ci_status = self.github_client.get_pr_checks(pr_number)
        status_name = ci_status.get("status")

        if status_name == "pending":
            task["status"] = "waiting_for_ci"
            print(f"[Waiting CI] Task {task['id']} PR #{pr_number} checks are pending.")
            return

        if status_name == "failure":
            print(f"[CI Failed] Task {task['id']} PR #{pr_number} checks failed.")
            self.record_task_failure(task, task.get("active_session_id"), f"CI checks failed on PR #{pr_number}")
            return

        # CI Passed - check review status
        review_status = self.github_client.get_pr_review_status(pr_number)
        if review_status == "pending":
            task["status"] = "waiting_for_review"
            print(f"[Waiting Review] Task {task['id']} PR #{pr_number} is awaiting required reviews.")
            return

        if review_status == "changes_requested":
            print(f"[Changes Requested] Task {task['id']} PR #{pr_number} requested changes.")
            self.record_task_failure(task, task.get("active_session_id"), f"Review requested changes on PR #{pr_number}")
            return

        # Request Auto-merge if enabled in config
        if self.config.get("auto_merge", True):
            print(f"[Requesting Auto-Merge] Task {task['id']} PR #{pr_number} passing all merge gates.")
            self.github_client.enable_auto_merge(pr_number)
        task["status"] = "waiting_for_review"  # Polling continues until GitHub reports MERGED

    def run_cycle(self):
        """Runs one iteration of the orchestration lifecycle."""
        print("=== Starting Automation Worker Cycle ===")

        # Step 1: Reconcile sessions (Rule 1)
        self.reconcile_sessions()

        # Step 2: Process tasks
        for task in self.tasks:
            status = task.get("status")

            if status == "pending":
                if self.is_dependency_satisfied(task):
                    self.start_task_session(task)
                else:
                    print(f"[Skipped] Task {task['id']} dependencies not yet satisfied.")

            elif status == "in_progress":
                self.update_task_session_status(task)

            elif status in ["waiting_for_ci", "waiting_for_review"]:
                self.check_pr_and_merge_gates(task)

            elif status in ["done", "blocked"]:
                continue

        # Save updated state
        self.save_tasks()
        print("=== Automation Worker Cycle Complete ===")


def main():
    tasks_file = os.getenv("TASKS_FILE", "automation/tasks.json")
    config_file = os.getenv("CONFIG_FILE", "automation/config.json")
    memory_file = os.getenv("MEMORY_FILE", "automation/memory.json")
    worker = AutomationWorker(tasks_file=tasks_file, config_file=config_file, memory_file=memory_file)
    worker.run_cycle()


if __name__ == "__main__":
    main()
