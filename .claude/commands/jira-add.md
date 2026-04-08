Create a new subtask (부작업) under a parent Jira issue.

Arguments: $ARGUMENTS (format: "PARENT_KEY subtask title")

## Steps

1. Parse $ARGUMENTS — first token is the parent issue key (e.g. APPL-376), the rest is the subtask summary
2. Run the following Python script:

```python
import sys, os
sys.path.insert(0, "d:/Project/Program/AutoReport")
from dotenv import load_dotenv
load_dotenv("d:/Project/Program/AutoReport/.env")
from workflow.task_provider import get_task_provider

provider = get_task_provider({"jira": {"project_key": "APPL", "sprint_id": 152}})
result = provider._request("POST", "/rest/api/2/issue", {
    "fields": {
        "project": {"key": "<PARENT_KEY>".split("-")[0]},
        "parent": {"key": "<PARENT_KEY>"},
        "summary": "<SUBTASK_TITLE>",
        "issuetype": {"name": "부작업"},
    }
})
print(result.get("key", ""))
```

3. Report the result:
   - Success: "PARENT_KEY 하위에 NEW_KEY 생성 완료: subtask title"
   - Failure: "부작업 생성 실패 — 상위 이슈 키를 확인하세요"

Keep the response to 1 line.
