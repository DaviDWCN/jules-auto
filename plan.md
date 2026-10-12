1. **Fix `audit_and_expand_backlog` dependencies chaining**
   - Update `automation/worker.py` in `ArchitectAuditor.audit_and_expand_backlog`.
   - Before adding tasks, determine the initial dependency. If `done_task_ids` is empty, anchor to the very last task in `existing_tasks` if it exists.
   - For each newly generated task, its dependency should be `dep_id`.
   - Update `dep_id` to be the newly generated task's ID so that the next generated task depends on this new one, enforcing a serial dependency chain.

2. **Fix `save_tasks` and `load_tasks` UTF-8 encoding**
   - Update `automation/worker.py` in `AutomationWorker.load_tasks` to use `encoding="utf-8"`.
   - Update `AutomationWorker.save_tasks` to use `encoding="utf-8"` and `ensure_ascii=False` when calling `json.dump`.

3. **Add Unit Tests in `tests/test_worker.py`**
   - Add `test_architect_auditor_dependencies_append_to_backlog_tail`: Create an auditor, pass existing tasks (none done), generate new tasks, and assert they append dependency correctly and chain sequentially.
   - Add `test_save_tasks_preserves_utf8_chinese`: Create a worker, add a task with Chinese characters, save it, read the file content as text, and assert it contains the actual Chinese characters (no `\u` escapes).

4. **Verify tests pass**
   - Run `pytest` and ensure all tests pass with zero warnings (or at least check that the new tests pass and no regression occurs).

5. **Run Pre-Commit Step**
   - Execute `pre_commit_instructions` to ensure proper testing, verification, review, and reflection are done.

6. **Submit Code**
   - Commit and submit using appropriate tool.
