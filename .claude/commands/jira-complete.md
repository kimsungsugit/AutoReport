Complete a Jira issue (transition to 종료 요청) with a completion comment.

Arguments: $ARGUMENTS (format: "ISSUE_KEY completion comment text")

## Steps

1. Parse $ARGUMENTS — first token is the issue key (e.g. APPL-389), the rest is the comment
2. Run the following Python script to complete the issue:

```python
import sys, os
sys.path.insert(0, "d:/Project/Program/AutoReport")
from dotenv import load_dotenv
load_dotenv("d:/Project/Program/AutoReport/.env")
from workflow.task_provider import get_task_provider

provider = get_task_provider({"jira": {"project_key": "APPL", "sprint_id": 152}})
result = provider.complete_issue("<ISSUE_KEY>", "<COMMENT>")
```

3. Report the result:
   - Success: "ISSUE_KEY → 종료 요청 완료. 댓글: comment"
   - Failure: "ISSUE_KEY 완료 처리 실패 — 현재 상태가 '진행 중'인지 확인하세요"

Keep the response to 1-2 lines.
