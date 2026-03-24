---
description: Run auto commit/push for all configured repositories (dry-run by default)
---

등록된 프로젝트들의 자동 커밋/푸시를 실행합니다.

인자: $ARGUMENTS

아래 단계를 수행해 주세요:

1. 기본은 **dry-run** 모드입니다 (변경사항 확인만)
2. 인자에 "push" 또는 "force"가 포함되면 실제 커밋/푸시를 합니다
3. 아래 명령을 실행합니다:
   - dry-run: `python scripts/auto_commit_push.py --dry-run`
   - 실제 실행: `python scripts/auto_commit_push.py`
4. 각 프로젝트별 상태(변경 파일 수, 커밋 해시, 성공/실패)를 표로 정리합니다

**주의**: 실제 커밋/푸시 전에 반드시 사용자에게 확인을 받으세요.
