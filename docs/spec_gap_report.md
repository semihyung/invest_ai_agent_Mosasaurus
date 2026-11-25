# 명세 v3.7.5 ↔ 코드 격차 보고서 (S0.1–S8 완료 후 v3.7.5 정렬 반영)

작성 기준: 자동 테스트 570개 통과(감사 7건 수정 후, 이전 422개)(`python -m unittest discover -s tests -t .`). **이 보고서는 "구현됨"과 "부품만 있고 아직 연결되지 않음"을 구분합니다.**
실계좌·실서버 호출, 운영 서버 배포, 쉘 스크립트의 실제 실행(`sh -n` 문법 검사만 수행)은 하지 않았습니다.

## 1. 단계별 변경 요약

| 단계 | 내용 | 주요 파일 |
| --- | --- | --- |
| S0.1 | 명세 v3.7.4 패치(승인 명령 3종, 예수금 필드, 취소 후 지연 체결, 슬롯 재정렬 등) | `mosasaurus_spec_v3.7.4.md` |
| S1 | 격차 감사 | 이 문서 |
| S2 | 순수 규칙: 진리표·전이표·미접수 확정·슬롯·낙폭 히스테리시스·HWM 동결/epoch/리베이스·변동성 하한·설정 정합성·예수금 대사 | `order_policy.py`, `order_manager.py`, `portfolio_manager.py`, `risk_manager.py`, `performance.py`, `quant_engine.py`, `config.py`, `cash_recon.py`, `config/rules.yaml` |
| S3 | 마이그레이션 003: 26개 테이블·뷰·트리거·partial index | `migrations/003_spec_v374_schema.sql`, `db.py` |
| S4 | `safety_flags`(SAFE_HALT 원인), 부팅 복구 Step 0–6, 호스트 상태 파일(UPS·NTP) | `safety_flags.py`, `boot_recovery.py`, `reconciliation.py`, `scheduler.py` |
| S5 | KIS 주문 어댑터(PAPER/REAL TR 표, 재생 테스트), ETF 호가표, 비상 재가격, 체결 증분, 지연 체결 보정, 의무 전이 | `_kis_order_adapter.py`, `order_manager.py` |
| S6 | Telegram 확인 코드 보안·사용자 ID·속도 제한, 승인·해제·분류·검증 작업 | `telegram_bot.py`, `operator_actions.py` |
| S7 | 배포 금지 시간·서명 검증 래퍼·10단계 배포·systemd·네트워크 감시·컨테이너 하드닝 | `deployment.py`, `net_watchdog.py`, `scripts/*`, `docker-compose.yml`, `Dockerfile`, `deploy.yml` |
| S8 | 운영 코드를 호출하는 백테스트 엔진·Walk-Forward·승인 산출물 | `backtest/engine.py`, `walk_forward.py`, `report.py` |

## 2. 사용자 확인이 필요한 결정(명세와 코드 사이에서 제가 정한 것)

1. `pending_actions.action_type` 이름을 기존 코드에 맞춰 `UNIVERSE_ADD`/`UNIVERSE_REMOVE` 로 확정하고 명세를 고쳤습니다.
2. `SAFE_PARKING` 의 `asset_class` 는 `MONEY_MARKET` 으로 허용했습니다(명세 0.5절에 반영). `RISK_ASSET` 은 `EQUITY` 만 DB `CHECK` 로 강제합니다.
3. `/resume`, `/training on|off` 를 명세 6.4절대로 2단계 확인으로 바꿨습니다. `run_telegram_polling` 은 새 명령 6종의 콜백을 요구하므로 **운영 연결 단계에서 콜백 주입이 필요**합니다.
4. 기존 `boot_recovery` 의 `SAFE_HALT_ALREADY_LATCHED`(영구 정지)를 원인별 자동/수동 해제로 대체했습니다. 기존 테스트 일부를 이 동작에 맞게 다시 썼습니다.
5. `CREATED → REJECTED`, `PARTIALLY_FILLED → CANCELED`, `CONFIRMED_NOT_ACCEPTED → SUBMITTING` 전이는 명세 7.3절 표에 없어 제거했습니다.
6. 배포 금지 시간은 `08:50–15:25` 에서 `08:20–15:45`(+토·일 구간, 완료 신호)로 넓혔고, 기존 `safe_deploy.sh` 는 10단계 절차로 교체했습니다.
7. (v3.7.5) 비상 재가격 함수(`next_emergency_price`·`calculate_emergency_reprice`·`tick_down`)는 **삭제**했습니다. 위험 축소 매도 가격은 `calculate_limit_price(..., reduce_only=True)`(Bid1 1회)입니다.

## 3. 아직 남은 부분 (현재 기준)

이전 판의 "미연결" 항목(15:00 파이프라인, 마감 대조, 기업행위 감지, 예수금 대사 실행, 티커별 매도 상호배제, `cycle_id` 헬퍼,
호출 우선순위·폴링 연장, Telegram 명령 연결 등)은 아래 4-2~4-9절에서 모두 구현됐습니다. 지금 남은 것은 다음뿐입니다.

| 항목 | 상태 |
| --- | --- |
| 신규 매수 실제 전송 | `KisBroker.buyable_cash`(`nrcvb_buy_amt` TR `[UNVERIFIED]`) 미확정 → 의도적으로 막혀 있음(fail-closed) |
| WebSocket(체결통보·iNAV·`ws_session_refresh`) | 미구현. iNAV 가 `None` 이라 전략 주문 검증이 거부됨 |
| REAL 3중 잠금 → `RealOrderGateContext` 연결, 스모크 세션 | REAL 전용. PAPER 단계에서는 거부 유지(`backtest_calibrated=False`·`operator_approval=False` 고정) |
| `[UNVERIFIED]` 표시(주문 응답 필드명, 휴장일 PAPER 지원, 호출 한도, ETF 호가표 등) | KIS 포털 확인 전까지 fail-closed 기본값 |
| systemd·워치독·CPU/온도 측정·UPS 안전 종료·배포/롤백·백업 암호화 실제 실행 | **Raspberry Pi 5 에서 검증 필요**(`sh -n` 문법 검사만) |
| 감사 Major 중 명세 개정이 필요한 항목 | 4-7절 표 참고(사용자 결정 대기) |
| Phase 2(LLM, 시나리오 63–75) | Stage 8 범위, 미착수 |

## 4. 11.2절 시나리오 커버리지 (1–62번, 마무리 8차 재작성)

`✅` 자동 테스트로 직접 검증, `🟡` 순수 로직·구성요소는 검증했고 나머지는 Raspberry Pi·WebSocket·실제 KIS 응답·REAL 개방이 필요.
**✅ 43개 / 🟡 19개 / ⬜ 0개** (Phase 2 63–75번은 이번 범위 밖).

| # | 상태 | 근거 · 남은 부분 |
| --- | --- | --- |
| 1 | ✅ | `test_quant_engine`(변동성 하한·초과수익 분모) |
| 2 | ✅ | `test_quant_engine`(복리 초과수익·`ER120`·거래대금·티커 타이브레이커) |
| 3 | ✅ | `test_market_data`(불완전·결측 일봉 거부), `test_morning_prep`(200일 Upsert) |
| 4 | ✅ | `phase1 #18`(PeakPrice 보정, 가짜 스탑 없음), `test_morning_prep`(소급 조정 감지), `test_operator_actions`(재기준화) |
| 5 | ✅ | `test_performance`(분배금은 수익·입금은 단위 조정), `test_v374_pure_rules`(분배금 일치 허용오차) |
| 6 | ✅ | `test_job_handlers`(예수금 대사 2회 재현 → 미분류), `test_operator_actions`(`/classify`), `test_boot_recovery` |
| 7 | ✅ | `test_performance`, `test_v374_pure_rules`(입금은 단위가격 불변) |
| 8 | 🟡 | `phase1 #09`(전액 파킹 폴백)·`test_daily_pipeline`(파킹 배정) ✅. 손절 대금 당일 파킹은 D+2 정산 필드 `[UNVERIFIED]` 라 실측 필요 |
| 9 | ✅ | `phase1 #10–12`, `test_daily_pipeline`(교체·생존 우선·승계), `test_review_fixes`(승계 1회) |
| 10 | ✅ | `phase1 #13–14`, `test_strategy_backtest`(지역 비중 상한), `test_v374_pure_rules`(US 쌍 슬롯) |
| 11 | ✅ | `test_scheduler_jobs`(미검증 제외·사유 기록), `test_universe`, `test_v374_schema`(분류 제약), `test_telegram_bot`(`/add` 거부) |
| 12 | ✅ | `phase1 #01`, `test_boot_recovery`(SUBMITTING→PENDING), `test_job_handlers`(재전송 없음·지연 접수 매칭 채택) |
| 13 | ✅ | `test_order_manager`(완전한 권위 증거 필요), `test_job_handlers`(장 마감 전 미확정 유지) |
| 14 | ✅ | `test_v374_pure_rules`/`test_order_manager`(미접수 확정 후에만 새 attempt) |
| 15 | ✅ | `phase1 #02`, `test_kis_order_adapter`(체결 증분), `test_job_handlers`(체결 → 주문·보유 반영) |
| 16 | 🟡 | `phase1 #03`(취소 경합 체결 우선)·취소 정리 ✅. 취소 요청 응답 유실 흐름은 직접 테스트 없음 |
| 17 | ✅ | `phase1 #05`, `test_order_manager`, `test_job_handlers`(같은 사유 1회), DB 유일 제약 |
| 18 | ✅ | `phase1 #06`, `test_daily_pipeline`(매도 선행·시한), `test_review_fixes`(스프레드 재평가) |
| 19 | ✅ | `phase1 #07`, `test_spec_gaps`(위험 축소 1%·호가 단위·동가 거부), `test_review_fixes`(GLOBAL) |
| 20 | ✅ | `test_v375_rules`, `test_job_handlers`(허용 원인만), `test_spec_gaps`(대조 신선도 예외) |
| 21 | ✅ | `test_job_handlers`(Bid1 1회·의무·15:10 이후), `test_order_manager`(iNAV 이상 시 미제출) |
| 22 | ✅ | `phase1 #08`, `test_job_handlers`(15:14:30 취소·09:10 검문소) |
| 23 | ✅ | `phase1 #17`, `test_spec_gaps`(마감 후·장중 -15% 자동 EMERGENCY) |
| 24 | ✅ | `test_v374_pure_rules`(SAFE_HALT 는 취소만), `test_daily_pipeline`(SAFE_HALT 생략) |
| 25 | ✅ | `phase1 #21`, `test_v374_pure_rules`, `test_telegram_actions` |
| 26 | 🟡 | `phase1 #19–20`·`test_spec_gaps`(가짜 달력으로 불일치·fail-closed·자동 해제) ✅. 실제 `exchange_calendars` 조회는 이 PC 보안 정책으로 미실행 |
| 27 | 🟡 | `test_order_manager`(스모크 게이트)·`test_operator_actions`(60분 세션)·`test_daily_pipeline`(최소 주문 금액) ✅. REAL 1주 스모크 연결은 REAL 미개방 |
| 28 | ✅ | `test_v374_schema`(제약·FK·트리거), `test_db` |
| 29 | 🟡 | `test_safeguards`(busy_timeout), `test_spec_gaps`(동시 writer), `phase1 #24`(WAL) ✅. 백업 복구 자동 검증(`restore_test.sh`)은 Pi |
| 30 | 🟡 | `test_v374_schema`(v2→v3 이관), `safe_deploy.sh` 구조 검사 ✅. 실제 롤백 실행은 Pi |
| 31 | 🟡 | `test_deployment`(금지 시간 판정) ✅. 지연 큐 실행은 Pi |
| 32 | 🟡 | `test_host_operations`(compose 최소 권한·비root Dockerfile) ✅. 실제 컨테이너 격리는 Pi |
| 33 | 🟡 | `phase1 #22`(65초 쿨다운·재사용), 타이머 구조 검사 ✅. 호스트 실행은 Pi |
| 34 | 🟡 | `test_order_manager`(REAL 게이트), `test_runtime`(REAL 거부), `test_telegram_actions`(승인 해시) ✅. REAL TR 선택 연결은 의도적으로 미개방 |
| 35 | 🟡 | `test_host_operations`(재부팅 규칙)·`test_health_monitor`(워치독 게이트) ✅. 실제 DNS 단절·재부팅은 Pi |
| 36 | ✅ | `test_telegram_bot`/`test_ops_jobs`(마스킹), `test_runtime`(비밀 미노출), `test_host_operations`(스크립트에 비밀 없음) |
| 37 | 🟡 | `test_host_operations`(UPS 상태 검증)·`test_safety_flags`(`UPS_LOW` 플래그·자동 해제) ✅. 안전 종료 절차는 Pi |
| 38 | 🟡 | `test_host_operations`(PR 에서 self-hosted 미실행) ✅. 실제 runner 격리는 Pi |
| 39 | 🟡 | `test_v374_pure_rules`(`rules.yaml` = 명세 블록)·`test_v374_schema` ✅. `docs/` 5종 ↔ 코드 자동 비교는 없음 |
| 40 | ✅ | `test_safety_flags`, `test_boot_recovery`(자동/수동 원인), `test_telegram_actions`(`/clear_halt`) |
| 41 | ✅ | `test_v374_pure_rules`(완화 설정·폐기 키 실패), `test_boot_recovery`(`CONFIG_INVALID`) |
| 42 | ✅ | `test_morning_prep`(감지·종목 격리·자동 검증), `test_operator_actions`(`/ca_confirm`), `test_review_fixes`(격리 중 스탑 보류) |
| 43 | ✅ | `test_v375_rules`(정지 상태·PROVISIONAL·오염 제거·학습 기간), `test_job_handlers`(대사 실행) |
| 44 | ✅ | `test_v374_pure_rules`(전액 출금 epoch·재입금), `test_performance`(초기화 지연) |
| 45 | ✅ | `test_boot_recovery`, `test_job_handlers`(마감 후 미접수 확정), `test_spec_gaps`(잔량 `EXPIRED`) |
| 46 | 🟡 | `test_v374_pure_rules`(BUY 장중 확정 불가·SELL 조건) ✅. WebSocket 연동이 없어 장중 확정은 실행하지 않음(보수적) |
| 47 | ✅ | `test_kis_order_adapter`(TR 표·시장가/빈 가격 거부·import 경계) |
| 48 | 🟡 | `test_kis_order_adapter`/`test_v374_schema`(증분·중복 거부), `test_job_handlers`(REST 반영) ✅. WS 체결통보 병합은 WebSocket 없음 |
| 49 | ✅ | `test_telegram_actions`(리베이스 1회 소비), `test_v374_pure_rules`(하한), `test_spec_gaps`(해제 직후 재발동 금지). 하한 5개 개장일은 달력 7일로 근사 |
| 50 | 🟡 | 타이머 구조·`CREDENTIALS_UNAVAILABLE` 등록/해제 ✅. 실제 주말 갱신은 Pi |
| 51 | ✅ | `test_v374_pure_rules`(변동성 하한·순위 버퍼), `test_v374_schema`(자산군 제약) |
| 52 | ✅ | `test_v374_pure_rules`(지수 패밀리·상관 차단·US 쌍), `test_daily_pipeline`(DB 상관계수) |
| 53 | ✅ | `phase1 #13`, `test_daily_pipeline`(보유 퇴출은 스프레드 무관, 신규 후보는 필터·게이트) |
| 54 | ✅ | `test_job_handlers`(15:10 이후 의무만), `test_spec_gaps`(주문 시각 창), 스케줄 15:40 |
| 55 | ✅ | `test_safeguards`(`SELL_ALREADY_ACTIVE`) |
| 56 | ✅ | `test_spec_gaps`(`cycle_id` 포맷), `test_v374_schema`(활성 attempt 1건·정정 체인 없음) |
| 57 | ✅ | `test_scheduler_jobs`(`RF_INVALID`), `test_order_manager`(호가 단위 5원) |
| 58 | ✅ | `test_review_fixes`(승계 1회·같은 날 재실행), `test_daily_pipeline`(부분 실행 시 소비) |
| 59 | 🟡 | `test_runtime`(REAL 거부)·`test_operator_actions`(승인 파일·DB 일치) ✅. 불일치 → `CONFIG_INVALID` 연결은 REAL 미개방 |
| 60 | ✅ | `test_telegram_confirmation_security`(1회용·사용자 결합·해시 저장·5분 만료·5회 실패 30분 잠금), `test_telegram_bot`(채팅 검증), `test_telegram_actions`(10분 유효) |
| 61 | ✅ | `test_spec_gaps`(Bid1=0 → `QUOTE_INVALID`), `test_daily_pipeline`(V ≤ 0 생략·수량식 0), `test_order_manager` |
| 62 | 🟡 | `test_safety_flags`(`CLOCK_UNSYNCED`·`UPS_LOW` 자동 해제) ✅. 실제 NTP·UPS 감시 데몬은 Pi |

## 4-1. v3.7.5 정렬 내역 (코드)

| 명세 변경 | 코드 반영 | 아직 연결 안 된 부분 |
| --- | --- | --- |
| D2 비상 재가격 삭제 | 재가격 함수·`replaces_order_id`·정정 인덱스·`execution.emergency` 삭제, `ux_orders_one_active_attempt` 1개 | — |
| V-B2 종결 트리거 | `trg_terminal_immutable`(체결 증가+잔량 0만), `LATE_FILL_CORRECTION` 잔량 0 고정 | — |
| V-B3 의무 수량 불변 | `trg_obligation_required_immutable`, `order_intents.obligation_id`, 체결 집계 동기화, 보유 0 → `VOIDED`, 활성 주문 있으면 대체 거부 | 09:10 검문소 잡이 의무 의도를 만드는 호출부 |
| D1 보호성 손절 | `PROTECTIVE_EXIT_REASONS`·`should_protective_stop`·`OrderKind/RiskAction.PROTECTIVE_EXIT_SELL`·게이트 `protective_exit`·미해결 재경보 | **스탑 감시 잡(위험 축소 매도 생성)과 재경보 잡 호출부**, 설정 `protective_exit.enabled` 를 `load_risk_flags` 에 전달하는 연결 |
| V-B5 대사 오탐 | 정지 상태 판정(`reconciliation_allowed`), `PROVISIONAL` 2단계, 조회 실패≠불일치, 허용 오차 1,000원, 고아 주문 처리 | 대사 실행 잡(체결 대금·잔고 조회 연결) |
| V-B6 회계 오염 | 오염 제거 단위가격, 늦은 분류 재계산 순수 함수(`restate_after_late_flow`), 악화 전이 비동결 | `daily_portfolio` 를 쓰는 마감 스냅샷 잡과 `/classify` 후 재계산 호출 |
| V-B7 파킹 결함 | 주간 프리뷰가 파킹 결함 시 `RF_INVALID` 프리뷰를 저장하고 교체 생략 | 15:00 Step B 가 `RF_INVALID` 를 읽어 교체를 건너뛰는 처리 |
| V-B8 현금 필드 | `accounting.valuation_cash_field` 설정·검사 | 평가액 교차 검증(`tot_evlu_amt`) 실행, PAPER 실측(runbook) |
| V-B9 내구성 | `synchronous=FULL`, 연결 시 WAL·FULL 검증 | 디렉터리 `fsync` 를 쓰는 호스트 스크립트(`refresh_kis_token.py` 등) 점검 |

마이그레이션 `004_spec_v375_schema.sql` 은 003 과 같은 테이블 재구성 방식으로 적용되며 `schema_migrations` 에 체크섬이 기록됩니다.
감사 보고서의 **Major 20건은 이번 정렬 범위 밖**입니다(`docs/audit_v3.7.4_review.md`).

## 4-2. Phase 1 마무리 1차 (Raspberry Pi 없이 가능한 배선)

| 항목 | 구현 | 파일 |
| --- | --- | --- |
| 위험 축소 매도 실행기(게이트→write-ahead→Bid1 1회→결과 기록, SAFE_HALT 보호성 손절 원인 판정) | ✅ | `src/order_executor.py` |
| `intraday_guard`(스탑 감시·15:10 이후 의무 기록만·조회 실패=`ACCOUNT_DATA_STALE`) | ✅ | `src/job_handlers.py` |
| `open_checkpoint`(의무 재매도·보유 0 → `VOIDED`) | ✅ | `src/job_handlers.py` |
| `order_cancel_sweep`(15:14:30 취소 요청 + 미체결 매도 의무화) | ✅ | `src/job_handlers.py` |
| `post_close_reconcile`(15:40: 정지 상태 대조·2회 재현 `ACCOUNT_MISMATCH`·예수금 대사·PeakPrice·일간 TWR/HWM 스냅샷) | ✅ | `src/job_handlers.py` |
| 안전 점검(UPS·시각 플래그 동기화, 24h 미해결 HALT 재경보) | ✅ (인트라데이 잡에 10분 주기로 합침) | `src/job_handlers.py`, `src/runtime.py` |
| KIS 응답 → 포트 변환(필드 누락 시 fail-closed) | ✅ (`prvs_rcdl_excc_amt`·`tot_evlu_amt` 는 `[UNVERIFIED]`) | `src/kis_broker.py` |
| 운영 조립(PAPER 전용, REAL 거부, 비밀은 환경변수만) | ✅ | `src/runtime.py`, `src/main.py` |
| PAPER 전용 주문 전송기 생성 경로(비공개 어댑터 경계 유지) | ✅ | `src/order_manager.py` |
| 스케줄 시각을 명세 9.1절에 정렬(`post_close_reconcile` 15:15:10 → 15:40, 가드 종료 15:25 → 15:30) | ✅ | `src/scheduler.py` |

버그 수정: 예수금 대사에서 첫 관측(`PROVISIONAL`) 직후 기준 예수금을 옮기면 다음 대사가 변동을 스스로 `DISMISSED` 시키는 문제를, 확정·해소 전에는 기준 예수금을 옮기지 않도록 처리했습니다.

## 4-3. Phase 1 마무리 2차 (Raspberry Pi 없이 가능한 나머지 잡)

| 항목 | 구현 | 파일 |
| --- | --- | --- |
| 주문·체결 동기화(체결 증분 기록·`positions` 반영·취소 확정·미접수 확정은 장 마감 후만·`PENDING_CONFIRMATION` 정지 원인 등록/해제) + 15초 `pending_confirmation_poller` | ✅ | `src/order_sync.py`, `src/job_handlers.py` |
| `morning_prep`: 200거래일 일봉 Upsert, 소급 조정(분배락) 감지 → 종목 격리 + `verified=0` 후보, ±30% 이상치 격리 | ✅ | `src/morning_prep.py` |
| `daily_pipeline`(15:00): 120MA·스탑·검증 상실 퇴출, 주간 첫 개장일 슬롯 확정·교체 매도, 매도 선행(시한 15:06)·매수(시한 15:10), 5.4절 수량식, 대표 파킹 배정, 슬롯·프리뷰 소비 | ✅ (제외 항목은 아래) | `src/daily_pipeline.py` |
| 전략 주문(교체·매수·파킹) 실행: 스프레드·iNAV 검증 후 매수 Ask1 / 매도 Bid1 | ✅ | `src/order_executor.py` |
| 18:05 일일 브리핑, 일요일 03:00 SQLite 백업+무결성 검사+8개 보관 | ✅ | `src/ops_jobs.py` |
| Telegram **알림 전송기**(마스킹·실패 무시) → 모든 잡의 CRITICAL/WARN 알림 연결 | ✅ | `src/ops_jobs.py`, `src/runtime.py` |

### 의도적으로 남겨 둔 것 (fail-closed 로 막혀 있음)

- **신규 매수는 실제로는 아직 나가지 않습니다:** `KisBroker.buyable_cash`(`nrcvb_buy_amt` 조회 TR 미확정 `[UNVERIFIED]`)가 예외를 올리고, iNAV 는 WebSocket 연동 전이라 `None` → 스프레드·iNAV 검증이 매수를 거부합니다. 매도(스탑·120MA·교체·의무 재주문)는 동작합니다.
- `morning_prep` 의 XKRX 달력 ↔ KIS 휴장일 이중 검증은 `exchange_calendars` 의존성 추가가 필요해 사용자 결정 대기 중입니다(현재는 KIS 휴장일 조회만 사용).
- 5.2절 필터 중 모멘텀 점수·절대수익 필터는 토요일 프리뷰에서만 평가하고, 15:00 에는 거래 이력·20일 거래대금·재진입 차단·120MA·검증·격리·데이터 상태를 재확인합니다.
- 아직 실행해 보지 않은 것: `main()` 의 Telegram 폴링(메인 스레드)+에이전트(작업 스레드) 구성은 실제 봇 토큰으로 검증하지 못했습니다(라즈베리파이·모의투자 단계에서 확인).

### 4-4. Telegram 명령 연결 (마무리 3차)

| 항목 | 구현 | 파일 |
| --- | --- | --- |
| 명령 19종 콜백(`/status`·`/preview`·`/pool`·`/trades`·`/health`·`/reconcile [adopt]`·`/pause`·`/stop`·`/resume`·`/training`·`/emergency [off]`·`/remove`·`/clear_halt`·`/classify`·`/ca_confirm`·`/approve_real`·`/approve_smoke`·`/approve_backtest`·`/add`) | ✅ | `src/telegram_actions.py` |
| 2단계 명령은 `pending_actions` 의 `APPROVED` 이력(명령 해시 일치)이 있어야만 실행 | ✅ | `src/telegram_actions.py` |
| 실패 답장은 고정 문구(예외 내용 비노출) + `COMMAND_FAILED` 기록 | ✅ | `src/telegram_actions.py` |
| `main()` 이 토큰·채팅 ID 가 있으면 폴링 시작(`allowed_user_ids` 주입 포함) | ✅(미실행 검증) | `src/main.py` |
| `hwm_rebase_pending` 소비 + 해제 후 `WARNING` 하한, 훈련 종료일 자동 해제, `/remove` 보유 종목 퇴출 | ✅ | `src/job_handlers.py`, `src/daily_pipeline.py` |
- 주간 교체 승계(misfire 후 다음 개장일 1회) 전용 로직: 현재는 "미소비 프리뷰가 있으면 다음 개장일에 같은 로직 재실행"으로 자연 승계됩니다(부분 실행되면 `CONSUMED`).
- 호스트(Raspberry Pi) 전용: systemd·워치독·CPU/온도 측정·UPS 안전 종료·실제 KIS 응답 필드 확정.

### 4-5. Phase 1 마무리 4차 (규칙 보강)

| 항목 | 구현 | 파일 |
| --- | --- | --- |
| 리밸런싱 초과분(Drift ≥ 5%p AND 최소 주문 금액) 매도 — 내림 수량 | ✅ | `src/daily_pipeline.py` |
| 파킹 현금화 매도 — 매수 재원 부족분 N 만큼 올림 수량 | ✅ | `src/daily_pipeline.py` |
| 슬롯 상관계수 검사(최근 60거래일 수익률 피어슨, 데이터 부족이면 건너뜀) | ✅ | `src/daily_pipeline.py` |
| 15:00 신규 진입 재평가: 거래 이력·20일 평균 거래대금·재진입 차단 | ✅ | `src/daily_pipeline.py` |
| 손절 체결 → 재진입 차단, 기준일 이후 교체가 소비되면 해제(5.2절 6번) | ✅ | `src/order_sync.py`, `src/daily_pipeline.py` |
| 15:00 시작 시 예수금 대사(기준 예수금은 옮기지 않음) | ✅ | `src/job_handlers.py`(`reconcile_cash`) |
| 허용 일정 기반 분배락 자동 검증(`ALLOWLIST_SCHEDULE`, 계수 0.95~1.0 + 등록 ex_date 일치) + PeakPrice 재기준화 | ✅ | `src/morning_prep.py`, `src/universe.py` |
| 승인 파일 선택 필드 `distribution_schedule`(ex_date + https 출처) 형식 검증 | ✅ | `src/universe.py` |
| 2단계 승인 1회용(성공 시 소진)·확인 후 10분 이내만 유효 | ✅ | `src/telegram_actions.py` |
| `/approve_backtest`: 보고서 정규화 해시 일치 + 현재 전략 해시 일치 + 기간 기록 | ✅ | `src/telegram_actions.py`, `backtest/report.py`(`data_range`) |
| 티커별 매도 상호배제(`SELL_ALREADY_ACTIVE`) | ✅ | `src/order_executor.py` |
| DB `busy_timeout=5000` 강제와 연결 시 검증 | ✅ | `src/db.py` |

남은 Phase 1 항목(파이 없이 가능하지만 이번에 하지 않음): 감사 보고서 Major 20건, 명세 11.2절 🟡 시나리오의 통합 테스트 보강,
호출 우선순위 큐·REST 폴링 주기 자동 연장, DB 단일 writer 스레드(현재는 `busy_timeout` 로 경합 대기).

### 4-6. Phase 1 마무리 5차 (호출 한도·우선순위)

| 항목 | 구현 | 파일 |
| --- | --- | --- |
| 프로세스 단일 우선순위 한도기(주문·취소·대조 > 스탑 감시 > 일봉·기타), 모든 브로커 호출·주문 전송에 적용 | ✅ | `src/rate_limit.py`, `src/runtime.py` |
| 스탑 감시 REST 폴링 주기 자동 연장(`ceil(보유 종목 수 / 초당 한도)`) + `/health` 에 연장·지연 표시 | ✅ | `src/job_handlers.py`, `src/telegram_actions.py` |

### 4-7. 감사 보고서 Major 20건 분류 (코드로 바로 고칠 수 없는 이유)

`docs/audit_v3.7.4_review.md` 2절의 Major 는 대부분 **명세 문구를 바꿔야 하는 결정**이거나 **호스트(라즈베리파이) 작업**입니다.
CLAUDE.md "명세와 코드가 충돌하면 임의로 판단하지 않는다" 에 따라 구현하지 않았습니다.

| 분류 | 항목 | 비고 |
| --- | --- | --- |
| ✅ 개정·구현 완료(D3) | `PENDING_CONFIRMATION` 을 BUY=계좌 신규 매수 / SELL=해당 티커로 한정, `SAFE_HALT` 미생성(고아 주문만 `BOOT_RECOVERY_FAILED`) | 명세 0.3·4.3·6.3·7.1·7.3절, `order_executor`·`boot_recovery`·`daily_pipeline` |
| 명세 개정 필요 | 취소 일괄 요청을 15:14:30~15:19:30 반복(`CANCEL_PENDING` 재요청), `PENDING_CONFIRMATION → EXPIRED` 전이 추가, 일시 거부(`EGW00201`) 재시도 허용 | 전이표 변경 |
| 명세 개정 필요 | 슬롯 단위 매수 판단(의존 매도 미체결 시 그 슬롯만 생략), 교체 매도 미제출분 `ROTATION_EXIT` 의무화 | 5.3절 Step B 변경 |
| 명세 개정 필요 | 소액 계좌 처리(`UNFILLABLE`, `min_trade_eff`), 파킹 현금화 N 정의(수수료·버퍼 포함), `459580` 전환 기준, 분배금 `distribution_receivables`·`/classify` 분할 | 5.1·5.4·2.2절 |
| 명세 개정 필요 | TickerBlock 보유 종목 사전 배정, 슬롯 재배정 후 지역 비중 재검사(3!=6 순열), `emergency_reasons` 집합·리베이스 횟수 제한(`epoch_max_drawdown`) | 5.3·6.2절 |
| 명세 개정 필요 | `reconciliation_ok`·`data_status`·`order_intents.status` 중복 출처 정리 | 스키마 변경 → 마이그레이션 005 |
| 호스트/배포(Pi 필요) | 토큰 갱신 여유(4h)·파일 권한 순서, 서명 검증·runner 신뢰 경계(수동 SSH 배포 권장), 롤백 데이터 손실·잠금 고착·백업 암호화, `run/` 마운트 분리, UPS RTC 재기동, 감시 데몬 오탐 기준 | 쉘·systemd·compose |
| 보안 정책 결정 | Telegram 탈취 대응(`away_mode`·TOTP·DB 기반 차단), `/emergency` 1단계화 | 정책 결정 |

### 4-8. 코드 리뷰 10건 수정 (마무리 7차)

| # | 결함 | 수정 |
| --- | --- | --- |
| 1 | `reconciliation_ok=false` 가 주문 잡 전체를 영구 잠금(잠금을 푸는 대조 잡도 잠김) | 건강 판정에서 대조 상태 제외, 대조·동기화·취소 정리 잡은 잠금 면제, 대조 성공 시 잠금 자동 해제 |
| 2 | 호가·잔고 조회 예외가 잡을 죽이고 의도를 `CREATED` 로 고착 | 의도 저장 전에 조회, 실패는 `BLOCKED:QUOTE_UNAVAILABLE`/`BROKER_UNAVAILABLE` 로 반환(다음 틱 재시도, 의무로 넘기지 않음), 기존 의도는 조회 없이 `DUPLICATE` |
| 3 | `GLOBAL` 지역 전략 주문이 `ValueError` | 스프레드·iNAV 검증이 GLOBAL(한도 1.2%) 지원 |
| 4 | 스탑 감시가 격리 종목을 무시 | 명세 4.2절대로 `TICKER_BLOCK` 종목은 가격 기반 자동 매도 보류(감사 2.2의 "스탑이 꺼진다" 우려는 명세 결정 사항으로 남음) |
| 5 | PeakPrice 가 하루 늦게 갱신 | 마감 후 현재가(당일 종가)까지 반영 |
| 6 | 스프레드에 막힌 교체 매도 미재평가 | 교체·Drift·파킹 매도를 계획으로 모아 30초 간격으로 15:06 까지 재평가, 미제출이면 위험자산 매수 생략 + 경고 |
| 7 | 주간 교체 승계 무제한 | 프리뷰당 첫 개장일 + `weekly_rotation_carryover_days`(1)일까지만, 초과 시 `EXPIRED` |
| 8 | 시세 조회 1회 실패가 당일 매수 차단으로 고착 | 일시 실패 표식(`TRANSIENT:`)이 붙은 STALE 은 다음 성공 조회(가드·폴러·15:00)에서 자동 해제 |
| 9 | 주간 프리뷰 미발송·상태 미갱신 | 저장 후 Telegram 발송 → `SENT`/`DELIVERY_FAILED`, 10분 간격 6회 재시도, 발송 실패여도 집행 가능 |
| 10 | 중첩 함수 Docstring 누락 | 한국어 Docstring 추가(AST 검사 0건 남음) |

### 4-9. 명세 잔여 항목 구현 (마무리 8차 — 명세는 고정, 파이 없이 가능한 것 전부)

| 명세 | 구현 | 파일 |
| --- | --- | --- |
| 4.3 #6·시나리오 26: XKRX(`exchange_calendars`) ↔ KIS 휴장일 이중 검증, 불일치·조회 불가 시 `CALENDAR_MISMATCH`(fail-closed, 일치하면 자동 해제), 개장 판정(`market_is_open`)과 `morning_prep` 에 적용 | ✅ (라이브러리는 지연 import + `requirements.txt` 추가) | `src/calendar_check.py`, `src/runtime.py` |
| 9.1 `morning_prep`: 비개장일 생략, 토큰 유효성(`CREDENTIALS_UNAVAILABLE`), 전 개장일 미확정 주문 장 마감 후 확정 재적용 | ✅ | `src/morning_prep.py` |
| 6.3 `CREDENTIALS_UNAVAILABLE` 등록·자동 해제(10분 점검) | ✅ | `src/job_handlers.py` |
| 시나리오 29·9.2: 프로세스 단일 writer(`with connection:` = 전역 락 + `BEGIN IMMEDIATE`), 1초 초과 쓰기 기록 | ✅ | `src/db.py` |
| 시나리오 61: 호가 0 이하·교차·호가 단위 비배수 → 예외 없이 `QUOTE_INVALID` 종목 격리, 정상 복귀 시 가드가 자동 해제 | ✅ | `src/order_executor.py`, `src/job_handlers.py` |
| 시나리오 45: 장 마감 후 취소·체결 없는 잔량 `EXPIRED` 확정(위험 축소 매도는 의무로) | ✅ | `src/order_sync.py` |
| 시나리오 23·49·6.2: 낙폭 -15% 자동 `EMERGENCY`(원인 `DRAWDOWN`, 마감 후 + 장중 60초 감시, 리베이스 대기 중 재발동 금지), EMERGENCY 중 미체결 매수 전량 취소 | ✅ | `src/job_handlers.py` |
| 7.1 공통 게이트: 신규 주문 09:00~15:10 창(게이트 9), 대조 TTL 300초 신선도(게이트 2, 장중 2분 주기 대조로 유지), 실패 게이트 번호를 `order_events.reason_code` 에 기록 | ✅ | `src/order_executor.py`, `src/job_handlers.py` |
| 7.2: 위험 축소 매도 스프레드 1.0% 한도, 전략 주문 스프레드·iNAV 보류는 의도를 만들지 않고 같은 cycle 로 재평가 | ✅ | `src/order_executor.py` |
| 7.1 `cycle_id` 포맷: `R{거래일}:{STOP\|MA120\|REMOVE\|VETO}`, `W{ISO 주}`, `D{거래일}`, `X{의무}:{거래일}` | ✅ | `src/order_executor.py`, `src/daily_pipeline.py` |
| 7.1 게이트 5 유동성 한도(주문 금액 ≤ 20일 평균 거래대금 × 1%) | ✅ | `src/daily_pipeline.py` |
| 9.1 18:05 브리핑: MDD·XIRR·보유 슬롯·토큰 만료·API 호출 수 추가 | ✅ | `src/ops_jobs.py`, `src/rate_limit.py` |
| 6.4 `/pool` 점수·순위, `/health`(CPU·온도·디스크·DB·NTP·UPS·토큰), `/reconcile` 읽기 전용 즉시 대조, `/clear_halt ALL`(수동 원인만), 확인 메시지 효과 표시(즉시 스탑 도달 종목·해제될 원인·기업행위 추정 분배금·승인 파일 요약), 훈련 7일 경과 일일 알림 | ✅ | `src/telegram_actions.py`, `src/telegram_bot.py`, `src/job_handlers.py` |

**명세와 다르게 둔 부분(의도)**
- `post_emergency_floor_open_days`(5개 개장일)는 달력 7일로 근사합니다(휴장일 달력을 `exchange_calendars` 로 바꾸면 정확해짐).
- 이 개발 PC 에서는 보안 정책(애플리케이션 제어)이 `exchange_calendars`→pandas/numpy DLL 로드를 막아 **실제 달력 조회는 실행해 보지 못했고**, 가짜 달력으로만 테스트했습니다(Docker/Python 3.12·Pi 에서 확인 필요).

**명세에 있으나 파이/외부 조건이 필요해 남김**: WebSocket(체결통보·iNAV·장중 미접수 확정 시나리오 46)·`ws_session_refresh`, REAL 3중 잠금·스모크 세션(REAL 전용, PAPER 단계에서는 거부 유지), 토큰 갱신기·UPS·NTP·systemd·배포·롤백·백업 암호화 스크립트(호스트), `nrcvb_buy_amt`·KIS 응답 필드 확정(`[UNVERIFIED]`), `/add` 공식 상품정보 조회 계약.

### 4-10. 시작 자금 400만 원 점검 (감사 Major "소액 계좌"에 해당하는지)

`V = 4,000,000원` 기준으로 규칙을 계산했습니다.

| 항목 | 값 | 판정 |
| --- | --- | --- |
| 슬롯 목표(S1 45% / S2 30% / S3 20%) | 1,800,000 / 1,200,000 / 800,000원 | 모두 최소 주문 금액 150,000원 이상 → 슬롯이 영구 미매수가 되지 않음 |
| 현금 버퍼 5% | 200,000원 | 매수 가능 재원 = 예수금 − 200,000원 |
| Drift 리밸런싱 기준 5%p | 200,000원 | 최소 주문 금액(150,000원)보다 커서 `rebalance_drift_ratio` 가 정상 작동(문제가 되는 구간은 V < 3,000,000원) |
| 유동성 한도(20일 거래대금 × 1%) | 5억 원 이상 | 수백만 원 주문과 무관 |
| 459580 파킹 전환(1,000만 원 이상) | 해당 없음 | 기본 파킹 `423160` 유지 |

결론: 400만 원에서는 감사 보고서의 소액 계좌 문제(V < 300만 원)가 발생하지 않아 별도 명세 개정이 필요 없습니다. 다만 위험자산 3슬롯 각각이 ETF 1주 가격보다 충분히 커야 하므로(예: 80만 원 슬롯 ÷ 주당 5~10만 원) 편입 대상 ETF 중 주당 가격이 80만 원을 넘는 것은 S3 에 들어가기 어렵습니다.

### 4-11. 저장소 감사 7건 수정

| # | 결함 | 수정 | 파일 |
| --- | --- | --- | --- |
| 1 | `ALLOWED_USER_ID` 가 비면 사용자 확인 없이 명령 수신(명세 6.4 위반) | 사용자 ID 가 없으면 명령 수신을 켜지 않음(fail-closed) | `src/main.py` |
| 2 | 교체 매도가 스프레드 외 사유(조회 실패·게이트·활성 매도)로 못 나가도 매수 진행 | `SUBMITTED`·`DUPLICATE` 외 결과는 모두 15:06 까지 재평가, 끝내 미제출이면 위험자산 매수 생략 | `src/daily_pipeline.py` |
| 3 | 대조 신선도(300초) 일시 만료로 손절이 게이트에서 막히면 의도가 닫혀 다음 날 09:10 으로 밀림 | 의도 생성 전 `BLOCKED:RECONCILIATION_STALE`(일시 차단)로 반환 → 다음 틱 재시도 | `src/order_executor.py` |
| 4 | 공통 게이트 규칙이 3곳(PAPER·REAL 판정, 실패 번호 기록)에 중복 | `order_manager.first_failed_gate` 하나로 통합 | `src/order_manager.py`, `src/order_executor.py` |
| 5 | 주문 동기화가 같은 일별 체결 조회를 두 번 호출 | `fill_prices()` 가 직전 `orders()` 응답을 1회 재사용 | `src/kis_broker.py` |
| 6 | 항상 `True` 인 `account_fresh` 인자, 쓰이지 않는 자리표시 핸들러 | 삭제 | `src/order_executor.py`, `src/runtime.py` |
| 7 | 이 보고서 3절(미연결 표)이 낡음, `Ask1 ≥ Bid1` 설명이 코드(엄격 `>`)와 다름 | 3절 갱신, 틀린 설명 삭제 | 이 문서 |

## 5. 권고 다음 순서

1. 이 보고서 3절의 **배선 항목**을 위험도순으로: `safe_halt`와 직접 관련된 것(주문 게이트 연결, `post_close_reconcile`, 기업행위 감지, 예수금 대사 실행) → `daily_pipeline` 오케스트레이션 → 장중 잡.
2. Raspberry Pi 5 에서 쉘·systemd·컨테이너 하드닝 실제 검증(`docs/operations_runbook.md` 5번).
3. `[UNVERIFIED]` 항목을 KIS 포털·KRX 규정으로 확인해 명세에서 제거.
4. Shadow Trading 2주 → 스모크 → REAL(11.3절 체크리스트).
