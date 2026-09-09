# Commit-to-Jira workflow

## 목표

개발자는 평소처럼 커밋합니다. AutoReport는 커밋의 전체 SHA, 본문, 변경 파일,
검증 명령과 Jira 키를 근거로 다음 작업 계획을 만들고, 검토 가능한 Jira Task
제안으로 저장합니다. 운영 모드를 명시적으로 켠 경우에만 같은 제안을 Jira에
한 번 적용합니다.

```text
git commit
  -> commit evidence
  -> structured plan
  -> local Jira proposal
  -> review or auto-apply gate
  -> durable outbox
  -> Jira summary-marker reconciliation
  -> Task created once
```

## 현재 적용 상태 (2026-08-11)

- AutoReport, GreencoreMaster, Release_claude, CyberSecurity에 공용
  `post-commit` hook이 설치되어 있습니다.
- TLS 검증은 활성화돼 있으며, 품질 롤아웃 기본 단계는 `B(섀도)`입니다.
  `.env`에 `JIRA_AUTO_APPLY=1`이 있어도 단계 B에서는 Jira 쓰기를 수행하지 않습니다.
- APPL 프로젝트의 Task 생성 권한과 필수 필드, summary marker 조회를 실데이터로
  사전 점검했습니다.
- 자동 경로는 새 Task만 생성합니다. 기존 이슈의 댓글, 상태 전환, 완료 처리는
  자동 적용 범위가 아닙니다.
- 야간 스냅샷·WIP·merge처럼 계획 가치가 낮은 커밋은 품질 gate에서 차단하고,
  의미 있는 일반 개발 커밋은 구조화 본문이 없어도 근거 기반 계획을 만듭니다.

## 실행 계획

1. 커밋 증거를 전체 SHA, 본문, 변경 파일, 검증 결과 단위로 보존합니다.
2. 문제, 목표 결과, 범위, 완료 조건, 검증 계획, 남은 작업, 리스크를 분리한
   Jira Task 계획을 만듭니다.
3. Jira key가 명시된 커밋은 새 Task를 만들지 않고 검토 대상으로 보냅니다.
4. Jira key가 없는 제안은 품질 gate와 중복 marker 조회를 통과한 경우에만
   자동 생성합니다.
5. 응답이 불확실하면 재생성하지 않고 outbox `uncertain` 상태에서 조정합니다.
6. 오늘자 프로젝트 보고서와 대시보드에서 계획, Jira 실행 결과, 남은 위험을
   함께 추적합니다.

## 계획 품질 계약

각 제안은 다음 항목을 분리해 보존합니다.

- 문제: 왜 다음 작업이 필요한지
- 목표 결과: 완료 후 달라져야 하는 상태
- 범위: 관련 코드나 구성 영역
- 완료 조건: 검토자가 판정할 수 있는 조건
- 실행 검증: 명령, 환경, 실제 결과, 출처가 모두 확인된 결과
- 향후 검증 계획: 명령, 환경, 기대 결과, 판정 기준, `not_run` 상태
- 남은 작업: 커밋 이후 이어서 할 일
- 위험: 외부 의존성, 운영 영향, 불확실성
- 근거: 전체 커밋 SHA, 제목, 링크, 변경 파일

명시적인 Jira 키가 커밋이나 계획에 있으면 기존 이슈에 연결하는 후보로
취급합니다. Jira 키가 없으면 프로젝트 기본 설정 아래에 새 Task 제안을
만듭니다. 제목 키워드만으로 기존 이슈를 확정 연결하지 않습니다.

Jira 키는 오탐을 막기 위해 명시 문법만 인정합니다. 예: `[APPL-123] ...`,
`APPL-123: ...`, `fix(APPL-123): ...`, 또는 본문의 `Jira: APPL-123` / 
`Refs: APPL-123`. 코드 예시나 설명 문장 속 `APPL-123`은 연결로 보지 않습니다.

구조화 본문이 없는 일반 개발 커밋도 자동 계획할 수 있습니다. 이때 전체 SHA와
변경 파일을 근거로 완료 조건과 후속 작업을 만들고, 검증 결과가 없으면 관련
테스트 실행이 필요하다는 사실을 Jira 계획에 명시합니다. 반대로 merge, WIP,
임시 저장, 야간 snapshot 커밋은 새 Jira Task를 만들지 않습니다.

제안마다 결정론적 100점 품질 평가를 수행합니다. `commit_backed` 제안만 자동
후보가 될 수 있으며, 전체 SHA·변경 파일·1~3개 응집 커밋·한국어 측정 완료조건
2개 이상·작업별 리스크·일정/Epic 근거를 요구합니다. 실행 결과가 없다면 완전한
`not_run` 검증 계획을 요구하고, 실행하지 않은 검증을 PASS로 표시하지 않습니다.
85점 이상이면서 필수 차단 사유가 0개인 카드만 `high`와 자동 적용 후보가 됩니다.

## 적용 안전성

- `JIRA_AUTO_APPLY=0`이 기본값입니다. 이 모드는 로컬 proposal만 생성합니다.
- `JIRA_QUALITY_ROLLOUT_STAGE=B`도 기본값입니다. 단계 A/B는 Jira 생성을 차단하고,
  C는 서버가 확인한 수동 리뷰 요청만, D는 공용 품질 validator를 통과한 자동
  요청만 허용합니다.
- UI, hook, CLI 또는 직접 서비스 호출이 제출한 점수는 신뢰하지 않습니다.
  `JiraApplyService`가 저장된 원본 근거로 품질을 다시 계산하고 Jira POST 직전에도
  운영 단계와 품질을 재검사합니다.
- 적용 시 proposal ID에서 만든 고정 `ARID...` summary marker를 먼저 조회합니다.
  이 marker는 Task 생성 POST에 함께 기록되어 create-then-tag 충돌 구간이 없습니다.
- 쓰기 직전 작업을 outbox의 `in_flight`로 기록합니다.
- 성공은 `applied`, 명확한 HTTP 거절은 `failed`, 타임아웃이나 연결 단절은
  `uncertain`으로 기록합니다.
- `uncertain`은 자동 재시도하지 않습니다. Jira summary marker를 조회해 적용 여부를
  조정한 뒤에만 다시 진행합니다.
- Jira 자동 적용 writer는 하나의 AutoReport 설치와 중앙 outbox를 사용해야 합니다.
  서로 다른 PC가 같은 Jira 프로젝트에 동시에 자동 생성하는 구성은 Jira가 marker
  고유 제약을 제공하지 않으므로 지원하지 않습니다.
- 커밋이 있다는 이유만으로 기존 Jira 이슈를 완료하거나 상태 전환하지
  않습니다. 완료 제안에는 명시적인 완료 문구와 커밋 증거가 모두 필요합니다.

## 프로젝트 설정

`scripts/startup_projects.json`의 각 프로젝트 `jira` 블록에서 다음 값을
사용합니다.

```json
{
  "project_key": "APPL",
  "epic_key": "APPL-123",
  "auto_plan": true,
  "suggest_existing": true,
  "plan_horizon_days": 7,
  "report_required": "yes"
}
```

`epic_key`는 선택입니다. `suggest_existing: false`는 스프린트 전체 이슈를
불러오지 않고 새 작업 계획만 만드는 프로젝트에 사용합니다.

## 운영 점검

1. `.env`의 Jira URL, PAT, 프로젝트 키와 TLS 검증을 확인합니다.
2. 커밋 hook을 설치하고 `JIRA_AUTO_APPLY=0`에서 proposal JSON을 검토합니다.
3. 테스트 Jira에서 동일 proposal을 두 번 적용해 같은 `ARID...` Task가 하나만 생기는지
   확인합니다.
4. 타임아웃을 모의해 outbox가 `uncertain`을 자동 재시도하지 않는지
   확인합니다.
5. `python scripts/evaluate_jira_suggestion_quality.py <suggestions.json...> --enforce-plan-targets`로
   코호트 KPI를 확인합니다.
6. 최소 5영업일·20커밋 섀도 평가 후 단계 C에서 수동 승인 표본을 확인합니다.
7. 대상 프로젝트와 권한, 단일 writer, kill switch를 검증한 뒤에만 단계 D로
   승격합니다.

자동 생성은 Task 작성과 계획 추적을 돕는 기능입니다. 사람의 승인 없이 기존
이슈의 완료 상태를 바꾸는 기능으로 사용하지 않습니다.
