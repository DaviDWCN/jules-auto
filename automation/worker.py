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
            # Extract detailed error traceback from the first failed check run
            error_details = ""
            for run in check_runs:
                if run.get("conclusion") in ["failure", "timed_out", "cancelled"]:
                    output = run.get("output", {})
                    title = output.get("title", "")
                    summary = output.get("summary", "")
                    text = output.get("text", "")

                    details = []
                    if title: details.append(f"Title: {title}")
                    if summary: details.append(f"Summary:\n{summary}")
                    if text: details.append(f"Text:\n{text}")

                    if details:
                        full_error = "\n".join(details)
                        # Retain up to ~1000 characters, prioritizing the end (traceback)
                        if len(full_error) > 1000:
                            error_details = f"...(truncated)...\n{full_error[-950:]}"
                        else:
                            error_details = full_error
                    break

            return {"status": "failure", "error_details": error_details}

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
                        pass
        return todos

    def scan_untested_functions(self) -> List[Dict[str, Any]]:
        """
        Uses AST analysis to detect public functions in src/ that are not referenced in tests/.
        """
        if not os.path.isdir(self.src_dir) or not os.path.isdir(self.tests_dir):
            return []

        import ast
        untested_funcs = []

        for root, _, files in os.walk(self.src_dir):
            for file in files:
                if file.endswith(".py") and file != "__init__.py":
                    base_name = file[:-3]
                    src_file_path = os.path.join(root, file)
                    test_file_path = os.path.join(self.tests_dir, f"test_{base_name}.py")

                    # If test file does not exist, scan_untested_modules handles it
                    if not os.path.exists(test_file_path):
                        continue

                    try:
                        with open(src_file_path, "r", encoding="utf-8") as f:
                            tree = ast.parse(f.read(), filename=src_file_path)

                        public_funcs = [
                            node.name for node in tree.body
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and not node.name.startswith("_")
                        ]

                        with open(test_file_path, "r", encoding="utf-8") as tf:
                            test_content = tf.read()

                        for func_name in public_funcs:
                            if func_name not in test_content:
                                try:
                                    rel_path = os.path.relpath(src_file_path, start=".")
                                except ValueError:
                                    rel_path = os.path.relpath(src_file_path, start=self.src_dir)
                                untested_funcs.append({
                                    "file": rel_path,
                                    "base_name": base_name,
                                    "func_name": func_name
                                })
                    except Exception:
                        pass

        return untested_funcs

    def scan_coverage_reports(self, coverage_file: str = "coverage.json", threshold_percent: float = 80.0) -> List[Dict[str, Any]]:
        """Parses coverage.json if available from pytest-cov to find files with low coverage."""
        if not os.path.exists(coverage_file):
            return []

        low_coverage = []
        try:
            with open(coverage_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            files = data.get("files", {})
            for f_path, f_data in files.items():
                summary = f_data.get("summary", {})
                percent = summary.get("percent_covered", 100.0)
                if percent < threshold_percent:
                    low_coverage.append({
                        "file": f_path,
                        "percent": percent
                    })
        except Exception:
            pass
        return low_coverage

    def audit_and_expand_backlog(self, existing_tasks: List[Dict[str, Any]], auto_status: str = "pending") -> List[Dict[str, Any]]:
        """
        Synthesizes new backlog tasks based on code inspection.
        Enforces deduplication, dependencies, and max_auto_tasks guardrails.
        """
        active_auto_tasks = [
            t for t in existing_tasks
            if t.get("id", "").startswith("TASK-AUTO-") and t.get("status") in ["pending", "in_progress", "waiting_for_ci", "waiting_for_review"]
        ]
        if len(active_auto_tasks) >= self.max_auto_tasks:
            return []

        allowed_slots = self.max_auto_tasks - len(active_auto_tasks)
        new_tasks = []

        existing_descriptions = " ".join([
            f"{t.get('title', '')} {t.get('description', '')}" for t in existing_tasks
        ])

        # Anchor dependency to latest completed task if available
        done_task_ids = [t["id"] for t in existing_tasks if t.get("status") == "done"]
        if done_task_ids:
            dep_id = [done_task_ids[-1]]
        elif existing_tasks:
            dep_id = [existing_tasks[-1]["id"]]
        else:
            dep_id = []

        existing_ids = [t["id"] for t in existing_tasks]
        def next_auto_id():
            idx = 101
            while f"TASK-AUTO-{idx}" in existing_ids:
                idx += 1
            generated_id = f"TASK-AUTO-{idx}"
            existing_ids.append(generated_id)
            return generated_id

        # 1. Inspect untested modules
        untested_files = self.scan_untested_modules()
        for u_file in untested_files:
            if allowed_slots <= 0:
                break
            if u_file in existing_descriptions:
                continue

            base = os.path.basename(u_file)
            task_id = next_auto_id()
            new_tasks.append({
                "id": task_id,
                "title": f"补全 {base} 模块单元测试与覆盖率",
                "description": f"代码质量巡检发现 {u_file} 缺少自动化测试用例文件。请在 tests/ 目录下创建对应测试，覆盖核心函数与异常分支。",
                "acceptance_criteria": [
                    f"在 tests/ 目录下新建 test_{base} 并覆盖 {base} 的公开函数",
                    "测试包含正常输入与边界异常断言",
                    "pytest 全部通过"
                ],
                "dependencies": dep_id,
                "max_attempts": 3,
                "status": auto_status,
                "active_session_id": None,
                "pr_number": None,
                "pr_url": None,
                "failure_history": [],
                "is_auto_generated": True
            })
            dep_id = [task_id]
            allowed_slots -= 1

        # 2. Inspect untested functions within existing modules (AST analysis)
        if allowed_slots > 0:
            untested_funcs = self.scan_untested_functions()
            for uf in untested_funcs:
                if allowed_slots <= 0:
                    break
                if uf["func_name"] in existing_descriptions:
                    continue

                task_id = next_auto_id()
                new_tasks.append({
                    "id": task_id,
                    "title": f"为 {uf['file']} 的 {uf['func_name']} 函数补全单测",
                    "description": f"AST 语法树巡检发现 {uf['file']} 中的公开函数 `{uf['func_name']}` 在现有测试用例中缺少引用与断言。请在 tests/test_{uf['base_name']}.py 中补充针对该函数的独立测试。",
                    "acceptance_criteria": [
                        f"在 tests/test_{uf['base_name']}.py 中编写 test_{uf['func_name']} 测试用例",
                        f"断言覆盖 `{uf['func_name']}` 的正常返回值与异常输入",
                        "pytest 全部通过"
                    ],
                    "dependencies": dep_id,
                    "max_attempts": 3,
                    "status": auto_status,
                    "active_session_id": None,
                    "pr_number": None,
                    "pr_url": None,
                    "failure_history": [],
                    "is_auto_generated": True
                })
                dep_id = [task_id]
                allowed_slots -= 1

        # 3. Inspect TODO comments
        if allowed_slots > 0:
            todos = self.scan_todos()
            for td in todos:
                if allowed_slots <= 0:
                    break
                if td["comment"] in existing_descriptions:
                    continue

                task_id = next_auto_id()
                new_tasks.append({
                    "id": task_id,
                    "title": f"实现 {os.path.basename(td['file'])} 待办: {td['comment'][:30]}",
                    "description": f"代码审计在 {td['file']} 第 {td['line']} 行发现待办事项: {td['comment']}。请实现该功能并移除该 TODO 注释。",
                    "acceptance_criteria": [
                        f"完成 {td['file']} 中的待办需求: {td['comment']}",
                        "编写或补充对应单元测试",
                        "pytest 全部通过"
                    ],
                    "dependencies": dep_id,
                    "max_attempts": 3,
                    "status": auto_status,
                    "active_session_id": None,
                    "pr_number": None,
                    "pr_url": None,
                    "failure_history": [],
                    "is_auto_generated": True
                })
                dep_id = [task_id]
                allowed_slots -= 1

        if new_tasks:
            print(f"[Architect Agent] Synthesized {len(new_tasks)} new task(s) from code inspection: {[t['id'] for t in new_tasks]}")
        return new_tasks


# --- Level 3: Global Guideline Self-Optimizer ---

class GuidelineOptimizer:
    """
    Self-Refining Guidelines Agent:
    Monitors retry failure patterns across multiple tasks, extracts recurring failure modes,
    and autonomously optimizes config.json's project_context guidelines.
    """

    HEURISTIC_RULES = [
        {
            "keyword": "syntax",
            "rule": "严格遵守 Python 语法与模块导入规范，确保所有新增文件无语法解析错误。",
        },
        {
            "keyword": "assert",
            "rule": "编写单元测试时注意比对接口返回字典的全部字段，断言类型与格式需与 API 定义完全一致。",
        },
        {
            "keyword": "async",
            "rule": "编写异步逻辑时必须配合 pytest-asyncio，并使用 async with 正确管理资源与上下文。",
        },
        {
            "keyword": "mock",
            "rule": "单元测试严禁直接发起真实网络或外部系统连接，必须使用 unittest.mock 进行安全隔离。",
        },
        {
            "keyword": "import",
            "rule": "跨模块导入遵循项目标准 package 路径（如 `from src.xxx import ...`），避免相对路径歧义。",
        },
    ]

    def __init__(self, retry_threshold: int = 2):
        self.retry_threshold = retry_threshold

    def analyze_failure_patterns(self, tasks: List[Dict[str, Any]]) -> Dict[str, int]:
        """Counts occurrences of specific failure themes across task failure histories."""
        counts: Dict[str, int] = {}
        for task in tasks:
            for failure in task.get("failure_history", []):
                reason = (failure.get("reason") or "").lower()
                for rule_entry in self.HEURISTIC_RULES:
                    kw = rule_entry["keyword"]
                    if kw in reason:
                        counts[kw] = counts.get(kw, 0) + 1
        return counts

    def optimize_guidelines_if_needed(self, tasks: List[Dict[str, Any]], config: Dict[str, Any], config_file: str = "automation/config.json") -> bool:
        """
        Detects recurring failure themes exceeding retry_threshold and appends distilled
        guidelines to config['project_context'] to prevent future occurrences.
        """
        pattern_counts = self.analyze_failure_patterns(tasks)
        current_context = config.get("project_context", "")
        updated = False

        for rule_entry in self.HEURISTIC_RULES:
            kw = rule_entry["keyword"]
            rule_text = rule_entry["rule"]
            frequency = pattern_counts.get(kw, 0)

            if frequency >= self.retry_threshold and rule_text not in current_context:
                formatted_rule = f"\n- [自进化规范 针对高频重试] {rule_text}"
                current_context = current_context.rstrip() + formatted_rule
                config["project_context"] = current_context
                updated = True
                print(f"[Guideline Evolved] Injected new rule for recurrent '{kw}' failure (Frequency: {frequency}): {rule_text}")

        if updated and config_file and os.path.exists(config_file):
            try:
                with open(config_file, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=2, ensure_ascii=False)
                print(f"[Guideline Saved] Successfully updated {config_file}")
            except Exception as e:
                print(f"[Warning] Failed to save updated guidelines to {config_file}: {e}")

        return updated


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
        memory_manager: Optional[MemoryManager] = None,
        architect_auditor: Optional[ArchitectAuditor] = None,
        guideline_optimizer: Optional[GuidelineOptimizer] = None
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
        self.architect_auditor = architect_auditor or ArchitectAuditor(
            src_dir="src",
            tests_dir="tests",
            max_auto_tasks=self.config.get("max_auto_tasks", 2)
        )
        self.guideline_optimizer = guideline_optimizer or GuidelineOptimizer(
            retry_threshold=self.config.get("guideline_retry_threshold", 2)
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
            with open(self.tasks_file, "r", encoding="utf-8") as f:
                self.tasks = json.load(f)
        else:
            self.tasks = []

    def save_tasks(self):
        os.makedirs(os.path.dirname(self.tasks_file), exist_ok=True)
        with open(self.tasks_file, "w", encoding="utf-8") as f:
            json.dump(self.tasks, f, indent=2, ensure_ascii=False)

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

        # Inject lessons learned from repository memory bank (Phase 1 Self-Improvement / RSI Relevance)
        if self.config.get("enable_memory", True) and self.memory_manager:
            inject_count = self.config.get("inject_memory_count", 3)
            relevant_lessons = self.memory_manager.get_relevant_lessons(
                title=task.get("title", ""),
                description=task.get("description", ""),
                limit=inject_count
            )
            if relevant_lessons:
                prompt_lines.extend([
                    "=== Repository Lessons Learned (Avoid repeating past mistakes) ==="
                ])
                for m in relevant_lessons:
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

        if not self.jules_client.api_key:
            print(f"[Configuration Error] Cannot start session for Task {task['id']}: JULES_API_KEY is not set! Please configure JULES_API_KEY in GitHub Repository Secrets.")
            return

        try:
            session = self.jules_client.create_session(prompt=prompt, title=title, repository=repo)
            session_id = session.get("id") or session.get("sessionId") or session.get("name")
            task["active_session_id"] = session_id
            task["session_started_at"] = datetime.now(timezone.utc).isoformat()
            task["status"] = "in_progress"
            print(f"[Started] Created session {session_id} for Task {task['id']}")
        except Exception as e:
            print(f"[Error] Failed to start session for Task {task['id']}: {e}")

    def update_task_session_status(self, task: Dict[str, Any]):
        """Polls active session status and extracts PR URL/Number when complete."""
        session_id = task.get("active_session_id")
        if not session_id:
            return

        # Session Timeout Watchdog
        timeout_minutes = self.config.get("session_timeout_minutes", 60)
        started_at_str = task.get("session_started_at")
        if started_at_str and timeout_minutes > 0:
            try:
                started_dt = datetime.fromisoformat(started_at_str)
                now_dt = datetime.now(timezone.utc)
                elapsed_minutes = (now_dt - started_dt).total_seconds() / 60.0
                if elapsed_minutes >= timeout_minutes:
                    print(f"[Watchdog Timeout] Session {session_id} for task {task['id']} exceeded {timeout_minutes}m (elapsed: {elapsed_minutes:.1f}m). Terminating session wait.")
                    self.record_task_failure(task, session_id, f"Session timed out after {elapsed_minutes:.1f} minutes without completion.")
                    return
            except Exception as e:
                print(f"[Warning] Failed to calculate session timeout: {e}")

        try:
            session = self.jules_client.get_session(session_id)
            state = session.get("state", "").upper()

            if state in ["SUCCEEDED", "COMPLETED", "DONE"]:
                pr_url = session.get("pr_url") or session.get("pullRequestUrl")
                pr_number = session.get("pr_number") or session.get("pullRequestNumber")

                # Parse outputs which is a list in official Jules API: outputs[].pullRequest.url
                outputs = session.get("outputs", [])
                if not pr_url and isinstance(outputs, list):
                    for item in outputs:
                        if isinstance(item, dict) and "pullRequest" in item:
                            pr_info = item["pullRequest"]
                            pr_url = pr_info.get("url")
                            break
                elif not pr_url and isinstance(outputs, dict):
                    pr_url = outputs.get("pull_request_url") or outputs.get("pr_url")
                    pr_number = outputs.get("pull_request_number") or outputs.get("pr_number")

                if pr_url and not pr_number:
                    match = re.search(r"/pull/(\d+)", str(pr_url))
                    if match:
                        pr_number = int(match.group(1))

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

        # Clear active session pointer & started timestamp
        task["active_session_id"] = None
        task["session_started_at"] = None

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
        task["session_started_at"] = None
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
            error_details = ci_status.get("error_details")
            reason = f"CI checks failed on PR #{pr_number}"
            if error_details:
                reason += f"\nDetailed Error Traceback:\n{error_details}"
            self.record_task_failure(task, task.get("active_session_id"), reason)
            return

        # CI Passed - check review status
        require_review = self.config.get("require_review", False)
        review_status = self.github_client.get_pr_review_status(pr_number, require_review=require_review)
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

        # Step 2: Process tasks with exception isolation (RSI Item 2)
        # Two-pass design so that tasks that complete (done/blocked) in this cycle
        # free their concurrency slot before we try to start new pending tasks.
        active_statuses = {"in_progress", "waiting_for_ci", "waiting_for_review"}
        max_active_tasks = self.config.get("max_active_tasks", 1)

        # Pass 1: advance already-active tasks (may mark them done/blocked)
        for task in self.tasks:
            try:
                status = task.get("status")
                if status == "in_progress":
                    self.update_task_session_status(task)
                elif status in ["waiting_for_ci", "waiting_for_review"]:
                    self.check_pr_and_merge_gates(task)
            except Exception as e:
                print(f"[Error Handler] Exception encountered while processing task {task.get('id')}: {e}")

        # Recompute active count after completions
        active_tasks_count = sum(1 for t in self.tasks if t.get("status") in active_statuses)

        # Pass 2: start ready pending tasks against the updated count
        for task in self.tasks:
            try:
                if task.get("status") != "pending":
                    continue
                if not self.is_dependency_satisfied(task):
                    print(f"[Skipped] Task {task['id']} dependencies not yet satisfied.")
                    continue
                if active_tasks_count >= max_active_tasks:
                    print(f"[Concurrency Gate] Active tasks count {active_tasks_count} reaches max limit {max_active_tasks} — skipping {task['id']}")
                    continue

                self.start_task_session(task)
                if task.get("status") == "in_progress":
                    active_tasks_count += 1
            except Exception as e:
                print(f"[Error Handler] Exception encountered while processing task {task.get('id')}: {e}")

        # Step 3 (Level 3): Global Guideline Self-Optimization
        if self.config.get("enable_guideline_optimization", True) and self.guideline_optimizer:
            self.guideline_optimizer.optimize_guidelines_if_needed(
                tasks=self.tasks,
                config=self.config,
                config_file=self.config_file
            )

        # Step 4 (Level 2): Code Quality Audit & Backlog Self-Expansion
        if self.config.get("enable_backlog_expansion", True) and self.architect_auditor:
            auto_status = self.config.get("auto_task_status", "pending")
            new_auto_tasks = self.architect_auditor.audit_and_expand_backlog(
                existing_tasks=self.tasks,
                auto_status=auto_status
            )
            if new_auto_tasks:
                self.tasks.extend(new_auto_tasks)

        # Save updated state
        self.save_tasks()

        # Step 5 (RSI Item 3): Report Self-Improvement & System Health Telemetry
        print(self.generate_improvement_report())
        print("=== Automation Worker Cycle Complete ===")


    def get_self_improvement_metrics(self) -> Dict[str, Any]:
        """
        RSI Feature Item 3: Computes system health and self-improvement telemetry.
        """
        total = len(self.tasks)
        done_tasks = [t for t in self.tasks if t.get("status") == "done"]
        blocked_tasks = [t for t in self.tasks if t.get("status") == "blocked"]
        in_progress_tasks = [t for t in self.tasks if t.get("status") in ["in_progress", "waiting_for_ci", "waiting_for_review"]]
        pending_tasks = [t for t in self.tasks if t.get("status") == "pending"]

        first_pass_done = [t for t in done_tasks if len(t.get("failure_history", [])) == 0]
        total_failures = sum(len(t.get("failure_history", [])) for t in self.tasks)

        completion_rate = (len(done_tasks) / total * 100.0) if total > 0 else 0.0
        first_pass_rate = (len(first_pass_done) / len(done_tasks) * 100.0) if done_tasks else 0.0
        avg_retries = (total_failures / total) if total > 0 else 0.0

        memory_count = len(self.memory_manager.memories) if self.memory_manager else 0
        context_str = str(self.config.get("project_context", ""))
        guidelines_count = context_str.count("[自进化规范")

        return {
            "total_tasks": total,
            "done_tasks": len(done_tasks),
            "pending_tasks": len(pending_tasks),
            "in_progress_tasks": len(in_progress_tasks),
            "blocked_tasks": len(blocked_tasks),
            "completion_rate_pct": round(completion_rate, 1),
            "first_pass_success_pct": round(first_pass_rate, 1),
            "average_retries_per_task": round(avg_retries, 2),
            "memory_bank_lessons_count": memory_count,
            "evolved_guidelines_count": guidelines_count,
        }

    def generate_improvement_report(self) -> str:
        """Generates formatted diagnostic report of RSI performance."""
        metrics = self.get_self_improvement_metrics()
        report_lines = [
            "\n=== 🚀 Jules-Auto Self-Improvement & System Health Telemetry ===",
            f"Tasks Progress: Total: {metrics['total_tasks']} | Done: {metrics['done_tasks']} | In-Progress: {metrics['in_progress_tasks']} | Blocked: {metrics['blocked_tasks']}",
            f"Completion Rate: {metrics['completion_rate_pct']}% | First-Pass Success: {metrics['first_pass_success_pct']}%",
            f"Average Retries/Task: {metrics['average_retries_per_task']}",
            f"Repository Memory Bank: {metrics['memory_bank_lessons_count']} lessons learned",
            f"Evolved Project Guidelines: {metrics['evolved_guidelines_count']} rules active",
            "=================================================================\n"
        ]
        return "\n".join(report_lines)

    def run_polling_loop(self):
        """
        Runs the orchestration lifecycle in a spin loop until all active tasks
        complete or the maximum polling duration is reached.
        """
        if not self.config.get("enable_active_polling", True):
            print("[Polling Loop] Active polling is disabled in config. Running single cycle instead.")
            return self.run_cycle()

        interval_seconds = self.config.get("poll_interval_seconds", 20)
        max_minutes = self.config.get("max_poll_minutes", 10)
        start_time = time.time()
        max_seconds = max_minutes * 60

        print(f"=== Starting Active Polling Loop (Max {max_minutes}m, Interval {interval_seconds}s) ===")

        while True:
            # 1. Execute single cycle to reconcile state and advance tasks
            self.run_cycle()

            # 2. Re-read active statuses after cycle runs (and possibly updates self.tasks)
            active_statuses = {"in_progress", "waiting_for_ci", "waiting_for_review"}
            active_count = sum(1 for t in self.tasks if t.get("status") in active_statuses)

            # 3. Check for exit condition: no active tasks remaining
            if active_count == 0:
                print("[Polling Loop] No active tasks remaining. Exiting loop.")
                break

            # 4. Check for exit condition: max polling timeout
            elapsed = time.time() - start_time
            if elapsed >= max_seconds:
                print(f"[Polling Loop] Maximum polling duration of {max_minutes} minutes reached. Exiting loop.")
                break

            # 5. Wait for the interval duration before checking again
            print(f"[Polling Loop] Sleeping for {interval_seconds} seconds... (Elapsed: {elapsed:.1f}s / {max_seconds}s)")
            time.sleep(interval_seconds)

        print("=== Active Polling Loop Complete ===")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Jules Automation Worker")
    parser.add_argument("--poll", action="store_true", help="Enable active polling loop")
    args = parser.parse_args()

    tasks_file = os.getenv("TASKS_FILE", "automation/tasks.json")
    config_file = os.getenv("CONFIG_FILE", "automation/config.json")
    memory_file = os.getenv("MEMORY_FILE", "automation/memory.json")
    worker = AutomationWorker(tasks_file=tasks_file, config_file=config_file, memory_file=memory_file)

    if args.poll:
        worker.run_polling_loop()
    else:
        worker.run_cycle()


if __name__ == "__main__":
    main()
