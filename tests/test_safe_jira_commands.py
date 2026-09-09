from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_slash_commands_do_not_bypass_jira_review_path():
    for name in ("jira-add.md", "jira-comment.md", "jira-complete.md"):
        text = (ROOT / ".claude" / "commands" / name).read_text(encoding="utf-8")
        assert "provider._request" not in text
        assert "provider.add_comment" not in text
        assert "provider.complete_issue" not in text
        assert "must never bypass" in text
