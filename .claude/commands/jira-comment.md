Prepare a reviewed comment for a Jira issue.

Arguments: $ARGUMENTS (format: "ISSUE_KEY comment text")

## Steps

1. Parse the issue key and comment.
2. Do not call `workflow.task_provider` or Jira REST directly.
3. Use the matching AutoReport dashboard suggestion and let the user review and
   approve the final text. If no suggestion exists, report that no write occurred.

This command must never bypass the proposal/outbox/idempotency path.
