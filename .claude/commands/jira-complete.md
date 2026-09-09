Prepare a reviewed completion request for a Jira issue.

Arguments: $ARGUMENTS (format: "ISSUE_KEY completion comment text")

## Steps

1. Parse the issue key and completion evidence.
2. Require explicit completion wording plus commit/test evidence; status alone is
   not completion evidence.
3. Do not call `workflow.task_provider` or Jira REST directly.
4. Use the matching high-confidence AutoReport dashboard card and require an
   individual user approval. Never include completion in batch approval.

If those conditions are not met, leave the Jira status unchanged and report why.
This command must never bypass the proposal/outbox/idempotency path.
