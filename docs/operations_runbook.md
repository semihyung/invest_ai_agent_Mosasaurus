# Operations Runbook (명세 v3.7.5)

KIS·네트워크·전원·DB·Telegram 장애의 공통 원칙: 신규 주문을 멈추고, `PENDING_CONFIRMATION` 과 `exit_obligations` 를 보존하며,
KIS 와 SQLite 를 대조한 뒤, **원인별 해제 규칙**(`docs/risk_modes.md`)에 따라 복구합니다.

## 무인 복구 vs 수동 복구

- `/status` 로 활성 `SAFE_HALT` 원인 목록·발생 시각·해제 방식을 확인합니다.
- 자동 해제 원인(토큰·시각·캘린더·UPS·대기 주문·부팅 실패·설정)은 조건이 풀리면 스스로 해제됩니다. 10분 주기 `safety_flag_auto_clear` 와 부팅 복구가 평가합니다.
- 수동 해제 원인(`ACCOUNT_MISMATCH`·`DB_INTEGRITY`·`DEPLOY_ROLLBACK`)은 `/reconcile` 일치 확인 후 `/clear_halt <원인>` + `/confirm <코드>` 입니다.
  KIS 상태를 원본으로 채택해야 하면 `/reconcile adopt`(차이를 확인 메시지로 보고 2단계 확인).
- 휴대폰을 분실하면 SSH 로 접속해 `ALLOWED_USER_ID` 를 무효화합니다(같은 채널의 2단계 확인은 분실 시 보호가 되지 않음).

## 구현 착수 전 실측 (v3.7.5 Stage 2 완료 조건)

PAPER 계좌에서 매도·매수 체결 후 D+0/D+1/D+2 시점의 `dnca_tot_amt`·`prvs_rcdl_excc_amt`·`nrcvb_buy_amt`·`tot_evlu_amt` 가
어떻게 변하는지 기록하고 이 문서에 남깁니다. 평가액·매수가능금액·예수금 대사는 모두 `prvs_rcdl_excc_amt`(D+2 정산 예수금)
하나로 계산한다는 명세 2.2절 정의가 실측과 다르면 Stage 3 착수 전에 명세를 개정합니다.

| 항목 | 확인할 것 | 결과 |
| --- | --- | --- |
| `prvs_rcdl_excc_amt` | 체결 즉시 D+2 값에 반영되는가, 결제일에 반영되는가 | (미실측) |
| `nrcvb_buy_amt` | 당일 매도대금 재사용분을 포함하는가 | (미실측) |
| `tot_evlu_amt` | D+2 정산분을 포함하는가(교차 검증 필드) | (미실측) |
| 수수료·유관기관비 | 체결 건별 절사 방식(대사 허용 오차 1,000원이 충분한지) | (미실측) |

## 토큰

호스트 `systemd` 타이머가 **매시 정각·부팅 직후**(`--trigger recovery`, 만료 1시간 전 하드 가드)와 **평일 08:25**(`--trigger morning_prep`, 6시간 경과 시 선제 갱신)에
`scripts/refresh_kis_token.py` 를 실행합니다. 65초 쿨다운과 원자적 교체는 그대로입니다. 연속 3회 실패는 `run/token_refresh_status.json` 의 `alert=true` 로 표시되고
앱이 Telegram `CRITICAL` 경보를 보냅니다. 앱은 KIS 호출 전마다 `token.json` 의 mtime 을 확인해 재로드하며, 만료·파싱 실패는 `CREDENTIALS_UNAVAILABLE`(자동 해제)입니다.

## 호스트 상태 파일 (UPS·NTP)

`scripts/ups_monitor.py` 가 10초마다 `run/ups.json`, `run/clock.json` 을 원자적으로 기록합니다(UPS 읽기 명령은 `UPS_STATUS_COMMAND` 로 주입).
파일이 120초보다 오래되면 **상태 불명 = fail-closed** 로 `UPS_LOW`·`CLOCK_UNSYNCED` 가 활성화됩니다. 배터리 ≤ 20% → `UPS_LOW`, ≤ 10% → 안전 종료(호스트 스크립트 책임).

## 네트워크

`scripts/net_watchdog.sh` 가 1분마다 DNS(1.1.1.1·8.8.8.8)와 KIS HTTPS 를 분리 점검합니다. 연속 5회 미만 실패는 지수 백오프로 인터페이스만 재시작하고,
재부팅은 **장외·1시간 1회**일 때만 합니다(평일 08:50–15:45 에는 재부팅 금지, 판단은 `src/net_watchdog.decide_action`).

## 배포

1. GitHub Actions(`push` + `main` 만)가 클라우드 테스트 후 self-hosted Runner 에서 `sudo -n /usr/local/sbin/mosasaurus-deploy <SHA>` 하나만 실행합니다.
   Runner 계정은 docker 그룹이 아니며 금고를 읽을 수 없습니다.
2. 래퍼가 `origin/main` 조상 여부·운영자 서명(`git verify-commit`)을 확인하고 root 소유 승인본 `mosasaurus-safe-deploy` 를 실행합니다.
3. 금지 시간(평일 08:20–15:45, 토 09:50–10:30, 일 02:50–03:30 + 마감 대조·백업 완료 신호 전)에는 SHA 를 `run/deploy_queue` 에 기록하고 정상 종료합니다.
   `mosasaurus-deploy-queue.timer` 가 30분마다 최신 SHA 1건을 재시도합니다.
4. 10단계 절차는 `scripts/safe_deploy.sh` 주석을 따릅니다. 롤백 시 `DEPLOY_ROLLBACK` 이 활성화되고 `/clear_halt DEPLOY_ROLLBACK` 전까지 신규 주문이 금지됩니다.
5. 설치·권한: `scripts/systemd/README.md`. 이 환경(Windows)에서는 쉘·systemd·UPS 를 실행할 수 없으므로 **Raspberry Pi 5 에서 별도 검증이 필요**합니다.

## REAL 활성화와 스모크 테스트

실계좌는 세 가지가 모두 일치해야 열립니다: (1) `deployment.environment: REAL`, (2) 호스트 `approvals/real_activation.json`(`scripts/approve_real.sh`, 최대 30일),
(3) DB `operator_approvals`(Telegram `/approve_real <id>` 2단계 확인, 승인 파일 SHA-256 대조). 스모크 세션은 `scripts/approve_smoke.sh` + `/approve_smoke <id>`(최대 60분)이며
주문 1건이 종결되면 `CONSUMED` 로 닫힙니다. 전략 주문은 현재 전략 설정 해시와 일치하는 `/approve_backtest` 승인도 필요합니다. 승인 파일에는 계좌번호를 쓰지 마십시오(별칭만).

## Scheduler Fail-Closed

주문 관련 Job 은 `scheduler_order_lock` 이 없거나 true 일 때 실행되지 않습니다. misfire·실패는 `system_events` 에 기록하고 잠급니다.
잠금은 대조 성공과 활성 `SAFE_HALT` 원인 0건일 때만 풀립니다(`set_order_scheduler_lock`). 놓친 주문 Job 은 따라잡지 않습니다.
부팅 시 `boot_recovery`(Step 0–6)가 시각·DB·설정·토큰·크래시 주문·계좌 대조를 확인합니다.

## Universe Classification Gate

`config/universe_allowlist.yaml` 은 운영자가 검토한 시드 목록입니다(현재 비어 있음). 항목에는 기존 필드 외에 `asset_class`, `underlying_index_family`,
`distribution_schedule`, 출처 URL·확인일을 기록합니다. 공식 상품정보 조회 계약이 명세에 추가되기 전까지 `/add` 는 확인 코드를 발급하지 않고 거부합니다.
스키마 v1·v2 에서 올라온 자산은 모두 미검증으로 남아 프리뷰·신규 후보에서 제외됩니다.

## 백업

DB 백업은 SHA-256 파일과 함께 만들고 `restore_test.sh` 가 임시 복사본에서 검증합니다(운영 DB 를 덮어쓰지 않음). 백업 암호화 키(`backup.key`)는 금고 밖으로 복제하지 않으며 오프라인 보관 절차를 별도로 둡니다.
