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

