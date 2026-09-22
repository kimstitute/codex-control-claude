# 통제된 워크스페이스

P5 워크스페이스 API를 통해 컨트롤러는 불변의 Git 스냅샷을 대상으로 제한된
개수의 파일 읽기, 전체 파일 쓰기, 명명된 검사(named check)를 중재할 수
있습니다. Claude 자체는 여전히 공급된 텍스트만 처리하며, 이 워크플로우에서는
파일, 셸, 내장 도구, MCP에 접근할 수 없습니다.

명령, 정책, scout, 스냅샷 검토, 격리, apply, 복구, 스키마 8–13 계약의 전체
내용은 설치된 스킬 레퍼런스를 참고하세요:
[통제된 워크스페이스](../plugins/claude-control/skills/claude-control/references/workspaces.md).

P4 워크플로우는 이 API를 자동화하지 않습니다. 워크스페이스 생성, 단일
태스크 바인딩, 호출 승인, 고정된(frozen) 결과 내보내기, 그리고 그 결과의
승인은 여전히 명시적인 컨트롤러/Codex의 조치로 이루어집니다.

Schema 11은 run별 usage, 모델별 usage, 공급자 보고 비용과 지연을 저장합니다.
Schema 12의 `workspace apply`는 동결 manifest, 정확한 원본 HEAD, clean worktree를
검사한 뒤에만 동작하며 commit, merge, push를 수행하지 않습니다. Read-only Sonnet
`scout` workspace는 `composition create --scout-workspace`로 계획 입력에 고정할 수
있습니다.

Windows 10/11에서는 아키텍처가 일치하고 SHA-256으로 고정된 native supervisor가
`wsl.exe`를 관리합니다. 선택된 파일은 bounded deterministic tar로 WSL2의
private ext4 상태에 전달되고, 검증 뒤 기존 networkless Bubblewrap backend에서
실행됩니다. `/mnt` 아래 상태 경로, reparse point, 불완전한 mode sidecar 또는
실패한 live probe는 모두 workspace 실행 전에 거절됩니다. 자세한 절차는
[Windows 설치와 운영](windows.ko.md)을 참고하세요.

[실제 프로젝트 파일럿](real-project-pilot.ko.md)에는 실제 타임아웃과 보고서
형식 실패, 독립적인 검증, 그리고 다음 신뢰성 게이트(gate)가 기록되어
있습니다.
