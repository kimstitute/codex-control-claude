# 계획, 편집 및 고정 검토 구성(compositions)

0.9 버전은 기존 P4 워크플로와 P5 워크스페이스 엔진 위에 하나의 유한한(finite)
구성(composition)을 추가합니다:

```text
경계가 정해진 계획 + 독립적인 Fable 비평
  -> 계획에 대한 Codex의 정확한 수락
  -> 통제된 편집자(editor) 워크스페이스와 지정된 검사
  -> 고정된(frozen) 편집자 익스포트
  -> 그 익스포트로부터 만들어진 독립적인 읽기 전용 Fable 워크스페이스
  -> 편집자 결과에 대한 Codex의 정확한 수락
```

조정자(coordinator)는 결과를 수락하거나, 실패했거나 알 수 없는 실행을
재시도하거나, 세션을 교체하거나, 브랜치를 병합하거나, 푸시하거나, Codex를
깨우지 않습니다. 각 `composition run` 호출은 명시적이며 범위가 한정되어 있습니다.
기존 `workflow`와 `workspace` 명령은 독립적인 동작을 그대로 유지합니다.

## 생성과 실행

계획(planning) 할당, 편집자 할당, 편집자 워크스페이스 정책, 검토자 할당 이렇게
네 개의 JSON 파일을 준비하세요. 검토자 정책은 편집자 정책에서 파생되며, 동일한
읽기 가능 경로(readable paths)를 가지되 쓰기 가능 경로와 검사(checks)는 없고
verifier 역할을 사용합니다. 검토자 할당은 반드시 Fable을 사용해야 하며 역할은
`critic` 또는 `verifier`여야 합니다.

```bash
claude_control composition create \
  --planning-assignment-file /absolute/path/plan.json \
  --editor-assignment-file /absolute/path/editor.json \
  --editor-policy-file /absolute/path/editor-policy.json \
  --reviewer-assignment-file /absolute/path/reviewer.json \
  --scout-workspace <선택적인-finished-scout-workspace-uuid> \
  --repo /absolute/path/repository --ref HEAD \
  --operation-id feature-composition-001 \
  --max-revisions 2 --max-calls 6 \
  --dispatch-window-seconds 900 --reviewer-effort high

claude_control composition run --composition <uuid> --once
claude_control composition run --composition <uuid> \
  --until-idle --max-seconds 60
claude_control composition status --composition <uuid>
```

생성 과정은 요청된 Git ref를 하나의 불변(immutable) 커밋으로 해석하고, 구성과
P4 워크플로를 기록하며, 모델을 호출하지 않습니다. 이후 저장소가 이동하더라도
편집자 스냅샷을 다시 타겟팅할 수 없습니다. 선택적 scout는 동일 커밋과 동일
읽기 경로를 사용해 완료된 read-only Sonnet researcher workspace여야 합니다.
검증된 scout 보고서와 정확한 provenance가 계획자 context에 고정됩니다. 계획 워크플로는
`approve_recommended`에서 멈춥니다. 정확한 워커 작업, 리비전, 실행, 결과
digest를 확인한 뒤 `task accept`를 기록하세요. 이후의 경계 지정 구성 실행(bounded
composition run)은 그 검증된 계획 보고서와 출처(provenance)를 편집자 할당에
복사한 다음 편집자 워크스페이스를 생성합니다.

### 리더 작성 명세

0.15부터는 명세 작성자를 Codex 리더로 고정할 수 있습니다. planning assignment
대신 엄격한 leader-spec 문서와 Fable critic assignment를 전달합니다.

```json
{
  "version": 1,
  "id": "feature-spec",
  "name": "기능 명세",
  "objective": "필요한 동작을 정의한다.",
  "context": "관련 제약과 근거.",
  "scope": ["허용한 구현 범위."],
  "acceptance_criteria": ["관측 가능한 완료 기준."],
  "implementation_plan": ["순서가 있는 구현 단계."]
}
```

```bash
claude_control composition create \
  --leader-spec-file /absolute/path/spec.json \
  --critic-assignment-file /absolute/path/fable-critic.json \
  --editor-assignment-file /absolute/path/editor.json \
  --editor-policy-file /absolute/path/editor-policy.json \
  --reviewer-assignment-file /absolute/path/reviewer.json \
  --repo /absolute/path/repository --ref HEAD \
  --operation-id feature-composition-001 --reviewer-effort high
```

critic은 Fable과 `critic` 역할이어야 합니다. 불변 명세를 대신 작성하거나 수정하지
않으며 critic revision도 허용하지 않습니다. 명세 수정이 필요하면 리더가 새 문서와
composition을 만듭니다. 편집은 독립 비평의 승인 권고와 정확한 Codex accept가 모두
기록된 뒤에만 열립니다. editor는 리더 명세, digest, 승인한 비평을 서로 구분된
provenance로 받습니다.

### 동결 테스트 계약

`--test-contract-file`을 추가하면 editor binding 전에 컨트롤러가 baseline 검사를
실행하고, 최종 트리와 일치하는 검사 영수증이 있어야 frozen Fable reviewer를 만듭니다.

```json
{
  "version": 1,
  "frozen_paths": ["tests/test_feature.py", "tests/fixtures/"],
  "checks": {
    "unit": {"baseline": "fail", "post": "pass"}
  }
}
```

검사 이름은 editor policy에 이미 있어야 합니다. 신규 동작의 red test는 baseline을
`fail`, 회귀·리팩터링은 `pass`로 둡니다. post는 항상 `pass`입니다. 동결 경로는
읽기 범위 안에 있어야 하고 editor 쓰기 경로와 겹칠 수 없습니다. baseline 영수증,
동결 파일 해시, 최종 트리 검사 영수증은 기존 멱등 operation 원장에 저장됩니다.
최종 트리와 일치하는 통과 영수증이 없으면 reviewer 생성 전에 composition이 멈춥니다.

### 기록된 composition 평가

```bash
claude_control composition evaluate --all
claude_control composition evaluate \
  --composition <uuid> --composition <uuid>
```

평가는 SELECT만 사용합니다. terminal readiness, acceptance, unassisted-success 비율과
95% Wilson 구간을 반환합니다. 비용과 네 가지 provider token 필드는 해당 composition의
모든 run에 완전한 telemetry가 있을 때만 합산하고 partial/missing을 분리합니다.
complete-case 성공당 비용의 분자에는 측정된 실패 시도의 비용도 포함합니다.

편집자가 최종 익스포트를 고정(freeze)하면, 다음 구성 단계는 그 고정된 트리로부터
별도의 검토자 워크스페이스를 자동으로 생성합니다. 이는 라이브 저장소나 변경 가능한
편집자 트리를 절대 읽지 않습니다. 검토자는 편집자의 각 기준에 대한 구조화
verdict와 `approve`, `revise`, `blocked` 권고를 반환합니다. 편집자 결과를 직접
승인하거나 변경할 수는 없지만, `revise`와 `blocked`는 편집자 승인을 거부합니다.

두 익스포트가 모두 존재하고 권고가 approve이면 상태는
`awaiting_codex/final_review_ready`를 보고합니다. 다른 권고는
`final_review_revise` 또는 `final_review_blocked`로 표시되며 승인 gate가 닫힙니다.
계획, 편집자 익스포트, 검토자 익스포트를 확인한 뒤, 정확한 편집자
작업 리비전, 실행, 결과 digest에 대해 `task accept`를 사용하세요. 구성 상태는
그 정확한 원장(ledger) 기록과 온전한 고정 증거로부터만 `accepted`를 도출합니다.

## 실패, 중지, 복구

형식이 잘못되었거나 차단된 보고서, 소진된 예산, 변경된 컨텍스트, 모델 불일치,
알 수 없는 실행, 중단된 워크스페이스 생성은 구성을 주의(attention) 상태에서
멈추게 합니다. 다시 실행한다고 해서 그 작업이 복구, 재시도, 대체되지 않습니다.
기존 run 또는 workspace 진단 도구를 사용하여 식별된 불확실한 하위 항목만
정리하세요.

```bash
claude_control composition stop --composition <uuid> --operation-id stop-001
```

중지는 현재 활성화된 워크플로 또는 워크스페이스에 위임되며, 모든 계획, 영수증,
스냅샷, 익스포트를 보존합니다. 동일한 중지 작업을 반복해도
멱등적(idempotent)으로 동작합니다.

## 출처(provenance)와 호환성

스키마 10은 구성(composition), 멤버(member), 정확한 결과(exact-result) 테이블만
추가합니다. 이는 기존 워크플로, 워크스페이스, 작업, 결정(decision), 세션, 실행
행(row)을 다시 작성하지 않습니다. 각 단계는 작업, 리비전, 실행, 결과 digest를
고정합니다. 워크스페이스 단계는 고정된 매니페스트와 트리 digest도 함께
고정합니다. 유휴 상태의 스키마 9 저장소는 일반적인 검증된 `migrate --offline`
절차로 업그레이드하세요.
