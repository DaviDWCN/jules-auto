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


def test_dependency_checking(sample_tasks):
    worker = AutomationWorker(tasks_file=sample_tasks)

    # TASK-1 has no dependencies
    assert worker.is_dependency_satisfied(worker.tasks[0]) is True
    # TASK-2 depends on TASK-1, which is pending
    assert worker.is_dependency_satisfied(worker.tasks[1]) is False

    # Mark TASK-1 as done
    worker.tasks[0]["status"] = "done"
    assert worker.is_dependency_satisfied(worker.tasks[1]) is True


def test_session_reconciliation_rule1(sample_tasks):
    mock_jules = MagicMock()
    mock_jules.list_sessions.return_value = [
        {"id": "session-123", "title": "[TASK-1] First task", "state": "IN_PROGRESS"}
    ]

    worker = AutomationWorker(tasks_file=sample_tasks, jules_client=mock_jules)
    worker.reconcile_sessions()

    # TASK-1 should now be re-linked to session-123 and status set to in_progress
    assert worker.tasks[0]["active_session_id"] == "session-123"
    assert worker.tasks[0]["status"] == "in_progress"


def test_session_reconciliation_ignores_failed_history(sample_tasks):
    mock_jules = MagicMock()
    mock_jules.list_sessions.return_value = [
        {"id": "session-failed-1", "title": "[TASK-1] First task", "state": "FAILED"}
    ]

    worker = AutomationWorker(tasks_file=sample_tasks, jules_client=mock_jules)
    task = worker.tasks[0]
    task["failure_history"] = [{"session_id": "session-failed-1", "reason": "Failed run"}]
    task["status"] = "pending"

    worker.reconcile_sessions()

    # TASK-1 should NOT re-link to the failed session from failure_history
    assert task["active_session_id"] is None
    assert task["status"] == "pending"


def test_session_reconciliation_ambiguous_conflict(sample_tasks):
    mock_jules = MagicMock()
    mock_jules.list_sessions.return_value = [
        {"id": "session-123", "title": "[TASK-1] First task", "state": "IN_PROGRESS"},
        {"id": "session-456", "title": "[TASK-1] First task (retry)", "state": "IN_PROGRESS"}
    ]

    worker = AutomationWorker(tasks_file=sample_tasks, jules_client=mock_jules)
    worker.reconcile_sessions()

    # Ambiguous active sessions set task status to blocked
    assert worker.tasks[0]["status"] == "blocked"
    assert "Ambiguous" in worker.tasks[0]["blocked_reason"]


def test_retry_prompt_history_rule2(sample_tasks):
    worker = AutomationWorker(tasks_file=sample_tasks)
    task = worker.tasks[0]
    task["failure_history"] = [
        {"session_id": "sess-old", "reason": "Syntax error in src/app.py", "timestamp": "2025-01-01T00:00:00Z"}
    ]

    prompt = worker.build_task_prompt(task)
    assert "Previous Attempt Failures:" in prompt
    assert "Syntax error in src/app.py" in prompt
    assert "sess-old" in prompt


def test_record_task_failure_rule2(sample_tasks):
    worker = AutomationWorker(tasks_file=sample_tasks)
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


def test_check_pr_merge_gates_pending_ci_rule3(sample_tasks):
    mock_github = MagicMock()
    mock_github.get_pr.return_value = {"merged": False, "state": "open", "head": {"sha": "abc"}}
    mock_github.get_pr_checks.return_value = {"status": "pending"}

    worker = AutomationWorker(tasks_file=sample_tasks, github_client=mock_github)
    task = worker.tasks[0]
    task["pr_number"] = 42
    task["status"] = "waiting_for_ci"

    worker.check_pr_and_merge_gates(task)

    # Pending checks should NOT fail or block, status stays waiting_for_ci
    assert task["status"] == "waiting_for_ci"


def test_check_pr_merge_gates_ci_failed_rule3(sample_tasks):
    mock_github = MagicMock()
    mock_github.get_pr.return_value = {"merged": False, "state": "open", "head": {"sha": "abc"}}
    mock_github.get_pr_checks.return_value = {"status": "failure"}

    worker = AutomationWorker(tasks_file=sample_tasks, github_client=mock_github)
    task = worker.tasks[0]
    task["pr_number"] = 42

    worker.check_pr_and_merge_gates(task)

    assert task["active_session_id"] is None
    assert len(task["failure_history"]) == 1
    assert "CI checks failed" in task["failure_history"][0]["reason"]


def test_check_pr_merge_gates_merged_done(sample_tasks):
    mock_github = MagicMock()
    mock_github.get_pr.return_value = {"merged": True, "state": "closed"}

    worker = AutomationWorker(tasks_file=sample_tasks, github_client=mock_github)
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
    assert all(t["dependencies"] == ["TASK-1"] for t in new_tasks)


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
    )
    worker.config["enable_backlog_expansion"] = True
    worker.config["enable_guideline_optimization"] = True

    worker.run_cycle()

    mock_optimizer.optimize_guidelines_if_needed.assert_called_once()
    mock_auditor.audit_and_expand_backlog.assert_called_once()
    assert any(t["id"] == "TASK-AUTO-999" for t in worker.tasks)


def test_session_timeout_watchdog(sample_tasks):
    from datetime import datetime, timezone, timedelta

    worker = AutomationWorker(tasks_file=sample_tasks)
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


def test_worker_run_cycle_exception_isolation(sample_tasks):
    worker = AutomationWorker(tasks_file=sample_tasks)

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

