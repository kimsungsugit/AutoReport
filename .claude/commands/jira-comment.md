Add a comment to a Jira issue.

Arguments: $ARGUMENTS (format: "ISSUE_KEY comment text")

## Steps

1. Parse $ARGUMENTS — first token is the issue key (e.g. APPL-392), the rest is the comment
2. Run the following Python script:

```python
import sys, os
sys.path.insert(0, "d:/Project/Program/AutoReport")
from dotenv import load_dotenv
load_dotenv("d:/Project/Program/AutoReport/.env")
from workflow.task_provider import get_task_provider

provider = get_task_provider({"jira": {"project_key": "APPL", "sprint_id": 152}})
result = provider.add_comment("<ISSUE_KEY>", "<COMMENT>")
```

3. Report the result:
   - Success: "ISSUE_KEY에 댓글 작성 완료"
   - Failure: "ISSUE_KEY 댓글 작성 실패"

Keep the response to 1 line.
