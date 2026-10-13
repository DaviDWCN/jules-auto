import json
import pytest
from unittest.mock import MagicMock, patch
from automation.worker import AutomationWorker, JulesClient, GitHubClient


@pytest.fixture
def sample_tasks(tmp_path):
    tasks_file = tmp_path / "tasks.json"
    tasks = [
        {
            "id": "TASK-1",
            "title": "First task",
            "description": "Do something",
            "acceptance_criteria": ["Criteria 1"],
            "dependencies": [],
            "max_attempts": 3,
            "status": "pending",
            "active_session_id": None,
            "pr_number": None,
            "pr_url": None,
            "failure_history": []
        },
        {
            "id": "TASK-2",
            "title": "Second task",
            "description": "Dependent task",
            "acceptance_criteria": ["Criteria 2"],
            "dependencies": ["TASK-1"],
            "max_attempts": 3,
            "status": "pending",
            "active_session_id": None,
            "pr_number": None,
            "pr_url": None,
            "failure_history": []
        }
    ]
    tasks_file.write_text(json.dumps(tasks))
    return str(tasks_file)


def test_dependency_checking(sample_tasks, tmp_path):
    worker = AutomationWorker(tasks_file=sample_tasks, memory_file=str(tmp_path / "memory.json"))

    # TASK-1 has no dependencies
    assert worker.is_dependency_satisfied(worker.tasks[0]) is True
    # TASK-2 depends on TASK-1, which is pending
    assert worker.is_dependency_satisfied(worker.tasks[1]) is False

    # Mark TASK-1 as done
    worker.tasks[0]["status"] = "done"
    assert worker.is_dependency_satisfied(worker.tasks[1]) is True


def test_concurrency_gate_max_active_tasks(sample_tasks, capsys, tmp_path):
    worker = AutomationWorker(tasks_file=sample_tasks, memory_file=str(tmp_path / "memory.json"))
    # Set config to max 1 active task
    worker.config["max_active_tasks"] = 1

    # Remove dependency of TASK-2 on TASK-1 so it can start if allowed
    worker.tasks[1]["dependencies"] = []

    # Mark TASK-1 as active
    worker.tasks[0]["status"] = "in_progress"
    worker.tasks[0]["active_session_id"] = "session-1"

    # TASK-2 is pending
    assert worker.tasks[1]["status"] == "pending"

    # We need to mock update_task_session_status so it doesn't try to fetch real session for TASK-1
    # and we also mock start_task_session just in case, though it shouldn't be called.
    with patch.object(worker, "update_task_session_status") as mock_update, \
         patch.object(worker, "start_task_session") as mock_start:

        worker.run_cycle()

        # Output should mention the concurrency gate
        captured = capsys.readouterr()
        assert "[Concurrency Gate] Active tasks count" in captured.out
        assert "reaches max limit" in captured.out

        # TASK-2 should still be pending
        assert worker.tasks[1]["status"] == "pending"

        # start_task_session should not have been called for TASK-2
        mock_start.assert_not_called()

        # update_task_session_status should have been called for TASK-1
        mock_update.assert_called_once()


def test_session_reconciliation_rule1(sample_tasks, tmp_path):
    mock_jules = MagicMock()
    mock_jules.list_sessions.return_value = [
        {"id": "session-123", "title": "[TASK-1] First task", "state": "IN_PROGRESS"}
    ]

    worker = AutomationWorker(tasks_file=sample_tasks, jules_client=mock_jules, memory_file=str(tmp_path / "memory.json"))
    worker.reconcile_sessions()

    # TASK-1 should now be re-linked to session-123 and status set to in_progress
    assert worker.tasks[0]["active_session_id"] == "session-123"
    assert worker.tasks[0]["status"] == "in_progress"


def test_session_reconciliation_ignores_failed_history(sample_tasks, tmp_path):
    mock_jules = MagicMock()
    mock_jules.list_sessions.return_value = [
        {"id": "session-failed-1", "title": "[TASK-1] First task", "state": "FAILED"}
    ]

    worker = AutomationWorker(tasks_file=sample_tasks, jules_client=mock_jules, memory_file=str(tmp_path / "memory.json"))
    task = worker.tasks[0]
    task["failure_history"] = [{"session_id": "session-failed-1", "reason": "Failed run"}]
    task["status"] = "pending"

    worker.reconcile_sessions()

    # TASK-1 should NOT re-link to the failed session from failure_history
    assert task["active_session_id"] is None
    assert task["status"] == "pending"


def test_session_reconciliation_ambiguous_conflict(sample_tasks, tmp_path):
    mock_jules = MagicMock()
    mock_jules.list_sessions.return_value = [
        {"id": "session-123", "title": "[TASK-1] First task", "state": "IN_PROGRESS"},
        {"id": "session-456", "title": "[TASK-1] First task (retry)", "state": "IN_PROGRESS"}
    ]

    worker = AutomationWorker(tasks_file=sample_tasks, jules_client=mock_jules, memory_file=str(tmp_path / "memory.json"))
    worker.reconcile_sessions()

    # Ambiguous active sessions set task status to blocked
    assert worker.tasks[0]["status"] == "blocked"
    assert "Ambiguous" in worker.tasks[0]["blocked_reason"]


def test_retry_prompt_history_rule2(sample_tasks, tmp_path):
    worker = AutomationWorker(tasks_file=sample_tasks, memory_file=str(tmp_path / "memory.json"))
    task = worker.tasks[0]
    task["failure_history"] = [
        {"session_id": "sess-old", "reason": "Syntax error in src/app.py", "timestamp": "2025-01-01T00:00:00Z"}
    ]

    prompt = worker.build_task_prompt(task)
    assert "Previous Attempt Failures:" in prompt
    assert "Syntax error in src/app.py" in prompt
    assert "sess-old" in prompt


def test_blocked_task_creates_github_issue(sample_tasks, tmp_path):
    mock_github = MagicMock()
    mock_github.create_issue.return_value = {"html_url": "https://github.com/org/repo/issues/42"}

    worker = AutomationWorker(tasks_file=sample_tasks, github_client=mock_github, memory_file=str(tmp_path / "memory.json"))
    worker.config["enable_issue_alerts"] = True

    task = worker.tasks[0]
    task["max_attempts"] = 2
    task["description"] = "Test description"
    task["acceptance_criteria"] = ["Criteria 1"]

    # Simulate first failure
    worker.record_task_failure(task, "sess-1", "Error 1")
    assert task["status"] == "pending"
    assert mock_github.create_issue.call_count == 0
    assert "alert_issue_url" not in task

    # Simulate second (final) failure
    worker.record_task_failure(task, "sess-2", "Error 2")

    assert task["status"] == "blocked"
    mock_github.create_issue.assert_called_once()

    call_args, call_kwargs = mock_github.create_issue.call_args
    assert call_kwargs["title"] == f"[Blocked Task Alert] {task['id']}: {task['title']}"
    assert "Error 1" in call_kwargs["body"]
    assert "Error 2" in call_kwargs["body"]
    assert "Test description" in call_kwargs["body"]
    assert "Criteria 1" in call_kwargs["body"]
    assert call_kwargs["labels"] == ["automation-blocked", "bug"]

    assert task["alert_issue_url"] == "https://github.com/org/repo/issues/42"

    # Simulate another failure call when alert_issue_url is already set
    worker.record_task_failure(task, "sess-3", "Error 3")
    assert task["status"] == "blocked"
    # Should not call create_issue again
    assert mock_github.create_issue.call_count == 1


def test_record_task_failure_rule2(sample_tasks, tmp_path):
    worker = AutomationWorker(tasks_file=sample_tasks, memory_file=str(tmp_path / "memory.json"))
    task = worker.tasks[0]
    task["active_session_id"] = "sess-failed"

    worker.record_task_failure(task, "sess-failed", "Unit tests failed")

    assert task["active_session_id"] is None
    assert len(task["failure_history"]) == 1
    assert task["failure_history"][0]["reason"] == "Unit tests failed"
    assert task["status"] == "pending"  # Eligible for retry

    # Test max attempts reached
    task["failure_history"] = [
        {"session_id": "s1", "reason": "err"},
        {"session_id": "s2", "reason": "err"},
    ]
    worker.record_task_failure(task, "s3", "Final error")
    assert task["status"] == "blocked"


def test_check_pr_merge_gates_pending_ci_rule3(sample_tasks, tmp_path):
    mock_github = MagicMock()
    mock_github.get_pr.return_value = {"merged": False, "state": "open", "head": {"sha": "abc"}}
    mock_github.get_pr_checks.return_value = {"status": "pending"}

    worker = AutomationWorker(tasks_file=sample_tasks, github_client=mock_github, memory_file=str(tmp_path / "memory.json"))
    task = worker.tasks[0]
    task["pr_number"] = 42
    task["status"] = "waiting_for_ci"

    worker.check_pr_and_merge_gates(task)

    # Pending checks should NOT fail or block, status stays waiting_for_ci
    assert task["status"] == "waiting_for_ci"


def test_check_pr_merge_gates_ci_failed_rule3(sample_tasks, tmp_path):
    mock_github = MagicMock()
    mock_github.get_pr.return_value = {"merged": False, "state": "open", "head": {"sha": "abc"}}
    mock_github.get_pr_checks.return_value = {"status": "failure"}

    worker = AutomationWorker(tasks_file=sample_tasks, github_client=mock_github, memory_file=str(tmp_path / "memory.json"))
    task = worker.tasks[0]
    task["pr_number"] = 42

    worker.check_pr_and_merge_gates(task)

    assert task["active_session_id"] is None
    assert len(task["failure_history"]) == 1
    assert "CI checks failed" in task["failure_history"][0]["reason"]


def test_check_pr_merge_gates_merged_done(sample_tasks, tmp_path):
    mock_github = MagicMock()
    mock_github.get_pr.return_value = {"merged": True, "state": "closed"}

    worker = AutomationWorker(tasks_file=sample_tasks, github_client=mock_github, memory_file=str(tmp_path / "memory.json"))
    task = worker.tasks[0]
    task["pr_number"] = 42

    worker.check_pr_and_merge_gates(task)

    assert task["status"] == "done"


def test_memory_manager_lifecycle(tmp_path):
    from automation.worker import MemoryManager
    mem_file = tmp_path / "memory.json"
    mgr = MemoryManager(memory_file=str(mem_file), max_items=2)

    mgr.add_lesson("TASK-1", "Always mock external API in tests", trigger_reason="CI failed")
    assert len(mgr.memories) == 1
    assert mgr.memories[0]["task_id"] == "TASK-1"

    # Test deduplication for exact same task and lesson
    mgr.add_lesson("TASK-1", "Always mock external API in tests")
    assert len(mgr.memories) == 1

    # Test max_items limit
    mgr.add_lesson("TASK-2", "Second lesson")
    mgr.add_lesson("TASK-3", "Third lesson")
    assert len(mgr.memories) == 2
    assert mgr.memories[0]["task_id"] == "TASK-2"
    assert mgr.memories[1]["task_id"] == "TASK-3"


def test_prompt_injects_repository_memory(sample_tasks, tmp_path):
    from automation.worker import MemoryManager
    mem_file = tmp_path / "memory.json"
    mgr = MemoryManager(memory_file=str(mem_file))
    mgr.add_lesson("TASK-PREV", "Ensure fixture teardown runs to avoid deadlocks")

    worker = AutomationWorker(tasks_file=sample_tasks, memory_manager=mgr)
    prompt = worker.build_task_prompt(worker.tasks[0])

    assert "Repository Lessons Learned" in prompt
    assert "TASK-PREV" in prompt
    assert "Ensure fixture teardown runs to avoid deadlocks" in prompt


def test_task_merged_records_reflection_if_failures_occurred(sample_tasks, tmp_path):
    from automation.worker import MemoryManager
    mem_file = tmp_path / "memory.json"
    mgr = MemoryManager(memory_file=str(mem_file))

    mock_github = MagicMock()
    mock_github.get_pr.return_value = {"merged": True, "state": "closed"}

    worker = AutomationWorker(tasks_file=sample_tasks, github_client=mock_github, memory_manager=mgr)
    task = worker.tasks[0]
    task["pr_number"] = 42
    task["failure_history"] = [
        {"session_id": "sess-old", "reason": "CI checks failed on PR #42", "timestamp": "2026-10-11T00:00:00Z"}
    ]

    worker.check_pr_and_merge_gates(task)

    assert task["status"] == "done"
    # Should have recorded a lesson learned from the recovery!
    assert len(mgr.memories) == 1
    assert mgr.memories[0]["task_id"] == "TASK-1"
    assert "resolved after CI failure" in mgr.memories[0]["lesson"]


def test_architect_auditor_backlog_synthesis(tmp_path):
    from automation.worker import ArchitectAuditor

    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()

    # Create one tested module and one untested module with a TODO
    (src_dir / "auth.py").write_text("# auth logic\n# TODO: add rate limiting\ndef login(): pass")
    (src_dir / "payment.py").write_text("# payment logic\ndef pay(): pass")
    (tests_dir / "test_auth.py").write_text("def test_login(): pass")

    auditor = ArchitectAuditor(src_dir=str(src_dir), tests_dir=str(tests_dir), max_auto_tasks=2)

    untested = auditor.scan_untested_modules()
    assert any("payment.py" in f for f in untested)
    assert not any("auth.py" in f for f in untested)

    todos = auditor.scan_todos()
    assert len(todos) >= 1
    assert "rate limiting" in todos[0]["comment"]

    existing_tasks = [
        {"id": "TASK-1", "title": "Initial task", "status": "done", "description": "done"}
    ]
    new_tasks = auditor.audit_and_expand_backlog(existing_tasks=existing_tasks)

    assert len(new_tasks) == 2
    assert any("payment" in t["title"] for t in new_tasks)
    assert any("rate limiting" in t["title"] for t in new_tasks)
    # The first new task depends on TASK-1, the second depends on the first new task.
    assert new_tasks[0]["dependencies"] == ["TASK-1"]
    assert new_tasks[1]["dependencies"] == [new_tasks[0]["id"]]


def test_guideline_optimizer_recurrence(tmp_path):
    from automation.worker import GuidelineOptimizer

    config_file = tmp_path / "config.json"
    initial_config = {
        "project_name": "test",
        "project_context": "Base guidelines."
    }
    config_file.write_text(json.dumps(initial_config), encoding="utf-8")

    optimizer = GuidelineOptimizer(retry_threshold=2)

    tasks = [
        {
            "id": "T1",
            "failure_history": [
                {"reason": "Failed because mock was not used on network call"},
                {"reason": "Assertion error in API response payload"}
            ]
        },
        {
            "id": "T2",
            "failure_history": [
                {"reason": "Failed again, network timeout without mock"}
            ]
        }
    ]

    updated = optimizer.optimize_guidelines_if_needed(tasks=tasks, config=initial_config, config_file=str(config_file))

    assert updated is True
    assert "mock" in initial_config["project_context"]

    # Re-reading saved config from disk with utf-8 encoding
    saved_data = json.loads(config_file.read_text(encoding="utf-8"))
    assert "mock" in saved_data["project_context"]


def test_worker_run_cycle_level2_and_level3_integration(sample_tasks, tmp_path):
    mock_auditor = MagicMock()
    mock_auditor.audit_and_expand_backlog.return_value = [
        {
            "id": "TASK-AUTO-999",
            "title": "Synthesized task",
            "description": "Auto",
            "acceptance_criteria": ["ac"],
            "dependencies": [],
            "max_attempts": 3,
            "status": "pending",
            "active_session_id": None,
            "pr_number": None,
            "pr_url": None,
            "failure_history": []
        }
    ]

    mock_optimizer = MagicMock()

    worker = AutomationWorker(
        tasks_file=sample_tasks,
        architect_auditor=mock_auditor,
        guideline_optimizer=mock_optimizer
    ,
        memory_file=str(tmp_path / "memory.json"))
    worker.config["enable_backlog_expansion"] = True
    worker.config["enable_guideline_optimization"] = True

    worker.run_cycle()

    mock_optimizer.optimize_guidelines_if_needed.assert_called_once()
    mock_auditor.audit_and_expand_backlog.assert_called_once()
    assert any(t["id"] == "TASK-AUTO-999" for t in worker.tasks)


def test_session_timeout_watchdog(sample_tasks, tmp_path):
    from datetime import datetime, timezone, timedelta

    worker = AutomationWorker(tasks_file=sample_tasks, memory_file=str(tmp_path / "memory.json"))
    worker.config["session_timeout_minutes"] = 30

    task = worker.tasks[0]
    task["status"] = "in_progress"
    task["active_session_id"] = "sess-hang"
    # Started 45 minutes ago (exceeds 30 mins)
    past_time = datetime.now(timezone.utc) - timedelta(minutes=45)
    task["session_started_at"] = past_time.isoformat()

    worker.update_task_session_status(task)

    # Active session should be aborted and recorded as failure
    assert task["active_session_id"] is None
    assert task["session_started_at"] is None
    assert len(task["failure_history"]) == 1
    assert "timed out" in task["failure_history"][0]["reason"]
    assert task["status"] == "pending"  # Eligible for clean retry


def test_architect_auditor_dependencies_append_to_backlog_tail(tmp_path):
    from automation.worker import ArchitectAuditor
    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()

    (src_dir / "untested_module.py").write_text("def my_func(): pass")
    (src_dir / "untested_module2.py").write_text("def my_func2(): pass")

    auditor = ArchitectAuditor(src_dir=str(src_dir), tests_dir=str(tests_dir), max_auto_tasks=5)

    existing_tasks = [
        {"id": "TASK-EXISTING-1", "title": "Existing 1", "status": "pending"},
        {"id": "TASK-EXISTING-2", "title": "Existing 2", "status": "in_progress"}
    ]

    new_tasks = auditor.audit_and_expand_backlog(existing_tasks)

    assert len(new_tasks) >= 2

    # First generated task should depend on the last task in the existing tasks list
    assert new_tasks[0]["dependencies"] == ["TASK-EXISTING-2"]
    # The subsequent generated tasks should depend on the previous generated task
    assert new_tasks[1]["dependencies"] == [new_tasks[0]["id"]]

def test_save_tasks_preserves_utf8_chinese(tmp_path):
    from automation.worker import AutomationWorker

    # Create worker with a temp task file
    task_file = tmp_path / "tasks.json"
    worker = AutomationWorker(
        tasks_file=str(task_file),
        jules_client=None,
        memory_manager=None,
        guideline_optimizer=None
    )

    worker.tasks = [
        {"id": "TASK-CH-1", "title": "测试中文", "description": "这是描述"}
    ]

    worker.save_tasks()

    # Read raw content as text
    with open(task_file, "r", encoding="utf-8") as f:
        content = f.read()

    # Verify we actually see Chinese characters, not \u escaped
    assert "测试中文" in content
    assert "\\u" not in content

def test_architect_auditor_scan_untested_functions(tmp_path):
    from automation.worker import ArchitectAuditor

    src_dir = tmp_path / "src"
    tests_dir = tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()

    # Create module with two functions, test only covers func_a
    (src_dir / "service.py").write_text("def func_a(): pass\ndef func_b(): pass\ndef _private_func(): pass")
    (tests_dir / "test_service.py").write_text("def test_func_a(): func_a()")

    auditor = ArchitectAuditor(src_dir=str(src_dir), tests_dir=str(tests_dir))
    untested = auditor.scan_untested_functions()

    assert len(untested) == 1
    assert untested[0]["func_name"] == "func_b"
    assert "service.py" in untested[0]["file"]


def test_memory_manager_get_relevant_lessons(tmp_path):
    from automation.worker import MemoryManager

    mem_file = tmp_path / "memory.json"
    mgr = MemoryManager(memory_file=str(mem_file))

    mgr.add_lesson("TASK-101", "Always mock database connection in authentication tests", trigger_reason="Database error")
    mgr.add_lesson("TASK-102", "Ensure async cleanup runs when closing websocket sessions", trigger_reason="Websocket timeout")
    mgr.add_lesson("TASK-103", "Validate JSON schema for health check payload responses", trigger_reason="Schema mismatch")

    # Query matching authentication & database
    relevant = mgr.get_relevant_lessons(title="User authentication middleware", description="Fix database user lookup", limit=2)

    assert len(relevant) == 2
    # TASK-101 matches 'authentication' and 'database', so it should rank first
    assert relevant[0]["task_id"] == "TASK-101"


def test_worker_run_cycle_exception_isolation(sample_tasks, tmp_path):
    worker = AutomationWorker(tasks_file=sample_tasks, memory_file=str(tmp_path / "memory.json"))

    # Task 1 raises error during processing
    with patch.object(worker, "start_task_session", side_effect=RuntimeError("Simulated API failure")):
        # Should not raise exception out of run_cycle; handles error gracefully
        worker.run_cycle()

    # Worker completes cycle without crashing
    assert worker.tasks[0]["status"] == "pending"


def test_worker_rsi_metrics_and_report_generation(sample_tasks, tmp_path):
    from automation.worker import MemoryManager

    mem_file = tmp_path / "memory.json"
    mgr = MemoryManager(memory_file=str(mem_file))
    mgr.add_lesson("TASK-1", "Always mock network calls")

    worker = AutomationWorker(tasks_file=sample_tasks, memory_manager=mgr)
    worker.config["project_context"] = "Base context.\n- [自进化规范 针对高频重试] Mock network calls"
    worker.tasks[0]["status"] = "done"

    metrics = worker.get_self_improvement_metrics()

    assert metrics["total_tasks"] == 2
    assert metrics["done_tasks"] == 1
    assert metrics["completion_rate_pct"] == 50.0
    assert metrics["memory_bank_lessons_count"] == 1
    assert metrics["evolved_guidelines_count"] == 1

    report = worker.generate_improvement_report()
    assert "Jules-Auto Self-Improvement & System Health Telemetry" in report
    assert "Completion Rate: 50.0%" in report


def test_jules_client_create_session_payload(tmp_path):
    with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "sources": [
                {
                    "name": "sources/github-org-repo",
                    "githubRepo": {"owner": "org", "repo": "repo"}
                }
            ]
        }
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"id": "sess-123"}

        client = JulesClient(api_key="test-key")
        session = client.create_session(
            prompt="Test prompt",
            title="[TASK-1] Test title",
            repository="org/repo"
        )

        assert session["id"] == "sess-123"
        call_kwargs = mock_post.call_args[1]
        payload = call_kwargs["json"]
        assert payload["title"] == "[TASK-1] Test title"
        assert payload["automationMode"] == "AUTO_CREATE_PR"
        assert payload["sourceContext"]["source"] == "sources/github-org-repo"
        assert payload["sourceContext"]["githubRepoContext"]["startingBranch"] == "main"


def test_jules_session_outputs_array_parsing(sample_tasks, tmp_path):
    mock_jules = MagicMock()
    mock_jules.get_session.return_value = {
        "id": "sess-999",
        "state": "COMPLETED",
        "outputs": [
            {
                "pullRequest": {
                    "url": "https://github.com/org/repo/pull/42",
                    "title": "PR Title"
                }
            }
        ]
    }

    worker = AutomationWorker(tasks_file=sample_tasks, jules_client=mock_jules, memory_file=str(tmp_path / "memory.json"))
    task = worker.tasks[0]
    task["status"] = "in_progress"
    task["active_session_id"] = "sess-999"

    worker.update_task_session_status(task)

    assert task["status"] == "waiting_for_ci"
    assert task["pr_url"] == "https://github.com/org/repo/pull/42"
    assert task["pr_number"] == 42



def test_get_pr_review_status_defaults_approved_when_no_reviews_and_not_required(tmp_path):
    from automation.worker import GitHubClient
    with patch("requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = []

        client = GitHubClient(token="test_token", repository="test/repo")

        # When require_review is False and reviews is empty, it should be approved
        assert client.get_pr_review_status(1, require_review=False) == "approved"

        # When require_review is True and reviews is empty, it should be pending
        assert client.get_pr_review_status(1, require_review=True) == "pending"

def test_direct_merge_fallback_on_clean_status(tmp_path):
    from automation.worker import GitHubClient
    with patch("subprocess.run") as mock_run, \
         patch("requests.post") as mock_post, \
         patch("requests.put") as mock_put:

        # Mock gh CLI failure
        mock_run.return_value.returncode = 1

        # Mock GraphQL failure
        mock_post.return_value.status_code = 400

        # Mock direct merge success
        mock_put.return_value.status_code = 200

        client = GitHubClient(token="test_token", repository="test/repo")

        # We need to mock get_pr since GraphQL attempt needs it to fetch node_id
        with patch.object(client, "get_pr", return_value={"node_id": "test_node"}):
            assert client.enable_auto_merge(1) is True

            # verify direct merge was called since others failed
            mock_put.assert_called_once()
            args, kwargs = mock_put.call_args
            assert "merge" in args[0]
            assert kwargs["json"]["merge_method"] == "squash"


def test_active_polling_loop(sample_tasks, tmp_path):
    worker = AutomationWorker(tasks_file=sample_tasks, memory_file=str(tmp_path / "memory.json"))
    worker.config["enable_active_polling"] = True
    worker.config["poll_interval_seconds"] = 1
    worker.config["max_poll_minutes"] = 1 # We'll mock time anyway

    task = worker.tasks[0]
    task["status"] = "in_progress"

    # We use a mock to verify run_cycle is called multiple times and to simulate task completion
    # so the loop eventually exits. We'll use a side effect that modifies the task status.
    cycle_calls = []

    def mock_run_cycle(*args, **kwargs):
        cycle_calls.append(1)
        if len(cycle_calls) >= 3:
            # Simulate completion on 3rd cycle to exit the loop
            task["status"] = "done"

    with patch.object(worker, "run_cycle", side_effect=mock_run_cycle) as mock_cycle, \
         patch("time.sleep") as mock_sleep, \
         patch("time.time") as mock_time:

        # mock_time needs to return increasing values so elapsed works, but we exit before timeout
        # in this test because status changes to "done".
        time_values = [0, 1, 2, 3, 4, 5, 6]
        mock_time.side_effect = lambda: time_values.pop(0)

        worker.run_polling_loop()

        # Should have called run_cycle 3 times before exiting due to no active tasks
        assert mock_cycle.call_count == 3
        # Should have slept 2 times
        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(1)

def test_active_polling_loop_timeout(sample_tasks, tmp_path):
    worker = AutomationWorker(tasks_file=sample_tasks, memory_file=str(tmp_path / "memory.json"))
    worker.config["enable_active_polling"] = True
    worker.config["poll_interval_seconds"] = 1
    worker.config["max_poll_minutes"] = 1

    task = worker.tasks[0]
    task["status"] = "in_progress"

    with patch.object(worker, "run_cycle") as mock_cycle, \
         patch("time.sleep") as mock_sleep, \
         patch("time.time") as mock_time:

        # max_poll_minutes = 1 -> 60 seconds
        # time values simulating 0s, 30s, 60s, 61s...
        time_values = [0, 30, 61, 62]
        mock_time.side_effect = lambda: time_values.pop(0)

        worker.run_polling_loop()

        # Should have called run_cycle twice
        assert mock_cycle.call_count == 2
        assert mock_sleep.call_count == 1
        assert task["status"] == "in_progress" # Verify it exited due to timeout, not task completion

def test_active_polling_loop_disabled(sample_tasks, tmp_path):
    worker = AutomationWorker(tasks_file=sample_tasks, memory_file=str(tmp_path / "memory.json"))
    worker.config["enable_active_polling"] = False

    task = worker.tasks[0]
    task["status"] = "in_progress"

    with patch.object(worker, "run_cycle") as mock_cycle:
        worker.run_polling_loop()
        assert mock_cycle.call_count == 1


def test_ci_failure_traceback_extraction_and_prompt_injection(sample_tasks, tmp_path):
    worker = AutomationWorker(tasks_file=sample_tasks, memory_file=str(tmp_path / "memory.json"))
    task = worker.tasks[0]
    task["status"] = "waiting_for_ci"
    task["pr_number"] = 100
    task["active_session_id"] = "test-session"

    mock_pr = {"head": {"sha": "abcdef"}, "merged": False, "state": "open"}
    mock_check_runs_response = {
        "check_runs": [
            {
                "status": "completed",
                "conclusion": "failure",
                "output": None
            },
            {
                "status": "completed",
                "conclusion": "failure",
                "output": "String output instead of dict"
            },
            {
                "status": "completed",
                "conclusion": "failure",
                "output": {
                    "title": "Pytest Failure",
                    "summary": "1 failed, 2 passed",
                    "text": "E       AssertionError: assert False\nE       traceback goes here"
                }
            }
        ]
    }

    with patch.object(worker.github_client, "get_pr", return_value=mock_pr), \
         patch("requests.get") as mock_get:

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_check_runs_response
        mock_get.return_value = mock_response

        # This will trigger get_pr_checks which fetches via requests.get
        worker.check_pr_and_merge_gates(task)

    # 1. Assert traceback was saved to task history
    assert len(task.get("failure_history", [])) == 1
    reason = task["failure_history"][0]["reason"]
    assert "CI checks failed on PR #100" in reason
    assert "Detailed Error Traceback:" in reason
    assert "Title: Pytest Failure" in reason
    assert "AssertionError: assert False" in reason

    # 2. Assert build_task_prompt injects this traceback
    prompt = worker.build_task_prompt(task)
    assert "AssertionError: assert False" in prompt
    assert "traceback goes here" in prompt

def test_write_github_step_summary(sample_tasks, tmp_path, monkeypatch):
    worker = AutomationWorker(tasks_file=sample_tasks, memory_file=str(tmp_path / "memory.json"))

    # Setup some test data
    worker.tasks[0]["status"] = "done"
    worker.tasks[0]["pr_url"] = "https://github.com/test/repo/pull/1"
    worker.tasks[0]["failure_history"] = [{"error": "retry1"}]
    worker.tasks[1]["status"] = "in_progress"

    # Mock the memory manager
    worker.memory_manager = MagicMock()
    worker.memory_manager.memories = [
        {"task_id": "TASK-1", "lesson": "Lesson 1"},
        {"task_id": "TASK-2", "lesson": "Lesson 2"}
    ]

    summary_file = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    worker.write_github_step_summary()

    assert summary_file.exists()
    content = summary_file.read_text(encoding="utf-8")

    # Check headers
    assert "## 🤖 Jules Automation Worker Summary" in content
    assert "### 📋 Task Backlog Status" in content
    assert "### 📊 System Health & Telemetry" in content
    assert "### 🧠 Recent Lessons Learned" in content

    # Check task status table contents
    assert "| TASK-1 | First task | ✅ done | [PR Link](https://github.com/test/repo/pull/1) | 1 |" in content
    assert "| TASK-2 | Second task | 🔄 in_progress | - | 0 |" in content

    # Check memories
    assert "- **TASK-1**: Lesson 1" in content
    assert "- **TASK-2**: Lesson 2" in content
