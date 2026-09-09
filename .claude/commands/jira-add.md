Prepare a reviewed subtask (부작업) action under a parent Jira issue.

Arguments: $ARGUMENTS (format: "PARENT_KEY subtask title")

## Steps

1. Parse $ARGUMENTS — first token is the parent issue key, the rest is the summary.
2. Do not call `workflow.task_provider` or Jira REST directly.
3. Open/refresh the AutoReport Jira dashboard and use its reviewed add-subtask
   card or inline form. Confirm the exact parent, dates, description and summary
   before the user approves it.
4. If the review service is unavailable, report that the action was not applied.

This command must never bypass the proposal/outbox/idempotency path.
