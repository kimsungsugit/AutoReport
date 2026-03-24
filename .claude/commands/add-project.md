---
description: Add a new project to AutoReport monitoring
---

AutoReport에 새 프로젝트를 추가합니다.

인자: $ARGUMENTS

아래 단계를 수행해 주세요:

1. 인자를 파싱합니다. 형식: `<프로젝트명> <경로> [프로파일]`
   - 예: `/add-project MyProject D:/Project/MyProject desktop_app`
2. `scripts/startup_projects.json`을 읽어 현재 프로젝트 목록을 확인합니다
3. 이미 같은 이름의 프로젝트가 있으면 중복 안내를 합니다
4. 지정된 경로가 실제로 존재하고 git 저장소인지 확인합니다
5. 확인 후 `startup_projects.json`에 새 프로젝트를 추가합니다
6. 추가된 결과를 보여줍니다
