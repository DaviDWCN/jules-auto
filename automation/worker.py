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
import re
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

    def list_sources(self) -> List[Dict[str, Any]]:
        """GET /v1alpha/sources to retrieve connected GitHub repositories."""
        url = f"{self.base_url}/v1alpha/sources"
        try:
            response = requests.get(url, headers=self._get_headers(), timeout=30)
            if response.status_code != 200:
                print(f"[Warning] Failed to list sources ({response.status_code}): {response.text}")
                return []
            data = response.json()
            if isinstance(data, list):
                return data
            return data.get("sources", [])
        except Exception as e:
            print(f"[Warning] Exception while listing sources: {e}")
            return []

    def get_source_for_repo(self, repository: str) -> Optional[str]:
        """Finds source name (e.g. 'sources/github/owner/repo') for a given owner/repo."""
        if not repository:
            return None
        parts = repository.strip("/").split("/")
        if len(parts) < 2:
            return None
        owner, repo = parts[-2].lower(), parts[-1].lower()

        try:
            sources = self.list_sources()
            for s in sources:
                gh_repo = s.get("githubRepo", {})
                s_owner = (gh_repo.get("owner") or "").lower()
                s_repo = (gh_repo.get("repo") or "").lower()
                if s_owner == owner and s_repo == repo:
                    return s.get("name")
                s_name = (s.get("name") or "").lower()
                if f"{owner}/{repo}" in s_name or f"{owner}-{repo}" in s_name:
                    return s.get("name")
            if sources:
                return sources[0].get("name")
        except Exception as e:
            print(f"[Warning] Error matching repository to source: {e}")

        # Default fallback standard format
        return f"sources/github/{owner}/{repo}"

    def create_session(
        self,
        prompt: str,
        title: str,
        repository: Optional[str] = None,
        starting_branch: str = "main"
    ) -> Dict[str, Any]:
        """POST /v1alpha/sessions to start a session with official schema."""
        url = f"{self.base_url}/v1alpha/sessions"
        payload: Dict[str, Any] = {
            "title": title,
            "prompt": prompt,
            "automationMode": "AUTO_CREATE_PR",
        }
        if repository:
            source_name = self.get_source_for_repo(repository)
            if source_name:
                payload["sourceContext"] = {
                    "source": source_name,
                    "githubRepoContext": {
                        "startingBranch": starting_branch
                    }
                }

        response = requests.post(url, headers=self._get_headers(), json=payload, timeout=30)
        if response.status_code not in [200, 201]:
            print(f"[Jules API Error] POST {url} returned {response.status_code}: {response.text}")
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

    def get_pr_review_status(self, pr_number: int, require_review: bool = False) -> str:
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
            return "pending" if require_review else "approved"

        states = [r.get("state") for r in reviews]
        if "CHANGES_REQUESTED" in states:
            return "changes_requested"
        if "APPROVED" in states:
            return "approved"

        return "pending" if require_review else "approved"

    def direct_merge_pr(self, pr_number: int, merge_method: str = 'squash') -> bool:
        """Fallback to directly merge the PR via REST API if auto-merge fails or PR is already clean."""
        if not self.repository or not self.token:
            return False

        url = f"https://api.github.com/repos/{self.repository}/pulls/{pr_number}/merge"
        try:
            response = requests.put(
                url,
                headers=self._get_headers(),
                json={"merge_method": merge_method},
                timeout=30
            )
            if response.status_code in [200, 204]:
                return True
        except Exception:
            pass
        return False

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

        # Attempt 3: Direct merge fallback
        return self.direct_merge_pr(pr_number)


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

    def get_relevant_lessons(self, title: str = "", description: str = "", limit: int = 5) -> List[Dict[str, Any]]:
        """
        RSI Feature Item 1: Relevance-based experience memory retrieval.
        Scores memories based on keyword overlap with task title and description.
        Falls back to recency when scores are equal.
        """
        if not self.memories:
            return []

        text = f"{title} {description}".lower()
        words = set(w for w in text.replace("-", " ").replace("_", " ").split() if len(w) > 2)

        if not words:
            return self.get_recent_lessons(limit=limit)

        scored = []
        for idx, m in enumerate(self.memories):
            mem_text = f"{m.get('lesson', '')} {m.get('trigger_reason', '')} {m.get('task_id', '')}".lower()
            score = sum(1 for w in words if w in mem_text)
            # Tie breaker: recency index
            scored.append((score, idx, m))

        # Sort descending by score, then index (recency)
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [item[2] for item in scored[:limit]]


# --- Level 2: Code Quality Auditor & Backlog Synthesizer ---

class ArchitectAuditor:
    """
    Architect / Auditor Agent:
    Inspects repository codebase for test coverage gaps, TODOs, and code health,
    autonomously synthesizing new tasks for tasks.json with safety guardrails.
    """

    def __init__(self, src_dir: str = "src", tests_dir: str = "tests", max_auto_tasks: int = 2):
        self.src_dir = src_dir
        self.tests_dir = tests_dir
        self.max_auto_tasks = max_auto_tasks

    def scan_untested_modules(self) -> List[str]:
        """Finds python modules in src/ that have no corresponding test file in tests/."""
        if not os.path.isdir(self.src_dir):
            return []

        untested = []
        for root, _, files in os.walk(self.src_dir):
            for file in files:
                if file.endswith(".py") and file != "__init__.py":
                    base_name = file[:-3]
                    expected_test = f"test_{base_name}.py"
                    # Check if test file exists in tests/
                    test_found = False
                    if os.path.isdir(self.tests_dir):
                        for _, _, t_files in os.walk(self.tests_dir):
                            if expected_test in t_files:
                                test_found = True
                                break
                    if not test_found:
                        full_path = os.path.join(root, file)
                        try:
                            rel_path = os.path.relpath(full_path, start=".")
                        except ValueError:
                            rel_path = os.path.relpath(full_path, start=self.src_dir)
                        untested.append(rel_path)
        return untested

    def scan_todos(self) -> List[Dict[str, Any]]:
        """Scans source files for TODO and FIXME annotations."""
        if not os.path.isdir(self.src_dir):
            return []

        todos = []
        for root, _, files in os.walk(self.src_dir):
            for file in files:
                if file.endswith(".py"):
                    file_path = os.path.join(root, file)
                    try:
                        rel_path = os.path.relpath(file_path, start=".")
                    except ValueError:
                        rel_path = os.path.relpath(file_path, start=self.src_dir)
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            for idx, line in enumerate(f, 1):
                                if "TODO:" in line or "FIXME:" in line:
                                    tag = "TODO" if "TODO:" in line else "FIXME"
                                    content = line.split(f"{tag}:", 1)[1].strip()
                                    if content:
                                        todos.append({
                                            "file": rel_path,
                                            "line": idx,
                                            "tag": tag,
                                            "comment": content
                                        })
                    except Exception:
                        continue
        return todos

    # ... (rest of original file continues, but for the patch we only changed run_cycle)
