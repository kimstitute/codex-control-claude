# 역할별 모델과 노력 설정

Codex Control Claude는 각 역할에 사용할 Claude 모델과 노력 수준을 호스트별로
설정할 수 있습니다. 설정은 새 task, workflow, workspace, composition에 적용됩니다.
이미 만들어진 task와 세션은 생성 당시 해석된 모델과 effort를 계속 사용합니다.

## 지원하는 모델 표기

`model`에는 다음 형식 중 하나를 사용합니다.

- 이동 별칭: `sonnet`, `opus`, `haiku`, `fable`
- 정확한 모델 ID: `claude-fable-5-1`, `claude-opus-5`,
  `claude-sonnet-5`, `claude-haiku-4-5-20251001` 같은 `claude-...` 값
- Claude Code가 catalog에 표시하는 명시적 1M context selector: `opus[1m]`,
  `claude-fable-5-1[1m]` 같은 값

별칭은 실행 결과가 같은 모델 계열인지 확인합니다. 정확한 모델 ID는 Claude Code가
보고한 실제 ID와 바이트 단위로 같아야 성공합니다. 버전을 고정하려면 Claude Code가
표시하거나 실행 기록에 남긴 정확한 ID를 사용하세요. 앱의 `Opus 5` 같은 표시 이름은
설정 값이 아닙니다.

모델 목록은 계정과 Claude Code 버전에 따라 달라지므로 컨트롤러는 고정된 버전
목록을 유지하거나 모델을 자동 대체하지 않습니다. 사용할 수 없는 모델을 선택하면
첫 실행이 명시적으로 실패합니다.

## 계정 model catalog 조회

```bash
claude_control models catalog
```

이 명령은 Claude Agent SDK의 `initialize` 제어 요청을 사용해 로그인 계정에 현재
제공되는 selector, 해석된 모델 ID, 표시 이름과 지원 effort를 읽습니다. 모델 prompt,
도구 또는 MCP 호출은 발생하지 않습니다. Claude Code가 내부적으로 최신 상태를
조회하거나 cache를 사용할 수 있으므로, 결과는 설치된 Claude Code가 그 시점에
제공하는 유효 catalog입니다.

초기화 응답에 포함될 수 있는 이메일, 조직, 구독과 PID는 모두 버리고 모델 필드만
반환합니다. 결과는 저장하지 않으며 역할 설정도 바꾸지 않습니다.

- `selector`: Claude Code에 전달할 선택자
- `resolved_model`: 현재 해석된 실제 모델
- `supported_effort_levels`: Claude Code가 광고한 전체 목록
- `configurable_effort_levels`: 이 controller가 설정할 수 있는 교집합
- `role_settings_compatible`: selector를 역할 설정에 그대로 쓸 수 있는지 여부

`default`처럼 실행 때마다 해석될 수 있는 선택자는 catalog에 나타나더라도 역할 설정에
사용할 수 없습니다. 버전 고정과 실행 결과 검증을 유지하기 위한 제한입니다.

## 노력 수준

`effort`는 `low`, `medium`, `high`, `xhigh`, `max` 중 하나입니다. 화면의
`엑스트라`는 `xhigh`에 해당합니다. 모델이나 계정이 특정 단계를 지원하지 않으면
Claude Code의 오류가 그대로 기록되고 다른 단계로 낮추지 않습니다.

provider 기본 동작을 사용하거나 effort를 지원하지 않는 모델에는 `effort` 키를
생략하세요. JSON `null`은 허용하지 않습니다.

## 현재 설정 확인

```bash
claude_control models show
claude_control models catalog
```

`source`가 `built_in`이면 기존 호환 기본값을 사용합니다. executor와 researcher는
`sonnet`, 나머지 역할은 `fable`입니다. `configured`이면 호스트의 비공개
`role-models.json` 설정을 사용합니다.

## 전체 설정 교체

설정 파일은 여섯 역할을 모두 정확히 한 번씩 정의해야 합니다.

```json
{
  "contract": "claude-control.role-models.v1",
  "roles": {
    "executor": {"model": "claude-sonnet-5", "effort": "medium"},
    "researcher": {"model": "claude-haiku-4-5-20251001"},
    "planner": {"model": "claude-opus-5", "effort": "high"},
    "architect": {"model": "claude-fable-5-1", "effort": "xhigh"},
    "critic": {"model": "claude-fable-5-1", "effort": "high"},
    "verifier": {"model": "claude-opus-5", "effort": "high"}
  }
}
```

```bash
claude_control models configure --file /absolute/path/to/role-models.json
```

문서 전체를 검증한 뒤 호스트 전용 상태 디렉터리에 원자적으로 교체합니다. 필드가
빠졌거나 알 수 없는 역할·키·모델·effort가 있으면 이전 설정은 바뀌지 않습니다.

기본값으로 돌아가려면 다음을 실행합니다.

```bash
claude_control models reset
```

## 개별 작업에서 덮어쓰기

assignment의 `model`과 `effort`는 해당 작업에만 역할 기본값을 덮어씁니다.

```json
{
  "role": "critic",
  "model": "claude-opus-4-8",
  "effort": "max"
}
```

나머지 필드는 완전한 assignment 계약에 맞게 작성해야 합니다. 내부 workflow
reviewer는 critic 역할 기본값을 사용하며, 필요하면 `workflow create`의
`--reviewer-model`과 `--reviewer-effort`로 그 workflow만 덮어쓸 수 있습니다.
composition의 계획 검토 reviewer도 같은 규칙을 사용합니다.

모델과 effort는 task/session 생성 시점에 해석되어 프롬프트와 세션 원장에
동결됩니다. 이후 역할 설정을 바꿔도 기존 재시도, 개정, resume의 모델은 바뀌지
않습니다. 실행 결과에는 provider가 보고한 실제 모델 ID가 별도로 저장되며,
지정한 계열 또는 정확한 ID와 다르면 `model_mismatch`로 실패합니다.
