# Risk Modes (명세 v3.7.4 0.5·0.6·6.2·6.3절)

운영 모드 우선순위는 `SAFE_HALT` > `EMERGENCY` > `PAUSED` > `TRAINING` > `NORMAL` 입니다.
낙폭 단계(`NORMAL`/`CAUTION`/`WARNING`/`CRITICAL`)는 운영 모드가 아니라 **두 번째 입력**이며,
허용 여부는 `src/order_policy.is_allowed(mode, stage, kind)` 하나가 판정합니다.

## SAFE_HALT 는 원인 목록입니다

`SAFE_HALT` 는 단일 스위치가 아니라 `safety_flags` 의 활성 `HALT` 행(뷰 `halt_reasons`)이
1건 이상 존재하는지로 유도합니다. 같은 원인이 이미 활성이면 새 행을 만들지 않고 `last_seen_at` 만 갱신합니다.

| 원인 | 해제 방식 | 해제 조건 |
| --- | --- | --- |
| `PENDING_CONFIRMATION` | 자동 | 해당 주문이 모두 종결로 확정됨(장 마감 후 확정 포함) |
| `CREDENTIALS_UNAVAILABLE` | 자동 | 유효 토큰 재로드 성공 |
| `CALENDAR_MISMATCH` | 자동 | 다음 검증에서 두 캘린더 일치 |
| `CLOCK_UNSYNCED` | 자동 | NTP 오차 허용치 이내 확인 |
| `UPS_LOW` | 자동 | 외부 전원 복귀 + 배터리 ≥ 30% (기준 20% + 마진 10%p) |
| `BOOT_RECOVERY_FAILED` | 자동 | 다음 부팅 복구 성공 |
| `CONFIG_INVALID` | 자동 | 설정 수정 후 정합성 검사 통과 |
| `ACCOUNT_MISMATCH` | **수동** | `/reconcile` 일치 후 `/clear_halt` 또는 `/reconcile adopt` (2단계 확인) |
| `DB_INTEGRITY` | **수동** | 백업 복원·무결성 확인 후 `/clear_halt` |
| `DEPLOY_ROLLBACK` | **수동** | 원인 확인 후 `/clear_halt` |

자동 해제 원인은 `/clear_halt` 로 강제 해제할 수 없습니다(조건이 풀리지 않았는데 사람이 덮어쓰는 것을 막음).
`UNCLASSIFIED_CASH_FLOW`·`ACCOUNTING_INVALID`·`ACCOUNT_DATA_STALE` 은 `SAFE_HALT` 가 아닌 **신규 매수 차단**(`BUY_BLOCK`),
기업행위 미검증·호가 무효·가격 시계열 불량·거래정지는 해당 **종목만 격리**(`TICKER_BLOCK`)합니다.

## 주문 허용 진리표 (O = 허용, X = 차단)

| 모드 \ 종류 | 위험자산 매수 | 교체·리밸런싱 매도 | 파킹 매수 | 파킹 현금화 매도 | 위험 축소 매도 | 매도 의무 재주문 | 취소 | 스모크 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SAFE_HALT | X | X | X | X | X | X | O | X |
| EMERGENCY | X | X | X | X | O | O | O | X |
| PAUSED | X | X | X | X | O | O | O | X |
| TRAINING | X | X | O | X | O(−7%) | O | O | X |
| NORMAL | O* | O | O | O | O(−10%) | O | O | O(단계 NORMAL) |

\* 낙폭 `CAUTION`: 신규 슬롯 진입은 허용, 기존 보유 종목 Drift 보충 매수는 금지. `WARNING`: 위험자산 매수 전면 금지(파킹만).
`CRITICAL`: 운영 모드가 `EMERGENCY` 로 자동 전환됩니다. 상태가 모순(`CRITICAL` 인데 `EMERGENCY` 가 아님)이면 fail-closed 로 위험 축소 매도만 허용합니다.
조회·대조·취소는 모든 모드에서 허용합니다.

## 낙폭 단계와 히스테리시스

악화 방향 전이는 즉시, 회복 방향은 경계보다 2%p 이상 회복된 마감 스냅샷에서만 한 단계씩 풀립니다
(예: `WARNING` → `CAUTION` 은 낙폭 > −8%). 미분류 현금흐름이 있으면 악화 전이를 동결합니다
(`next_drawdown_stage(..., frozen=True)`).

## EMERGENCY 해제

낙폭 원인(`emergency_reason = DRAWDOWN`)의 `EMERGENCY` 를 `/emergency off` 로 해제하면 HWM 을 현재 단위가격으로
리베이스(`hwm_rebase_pending`)하고, 이후 5개 개장일 동안 최소 `WARNING` 단계를 유지합니다(재발동 루프 방지).
HWM 이 미분류 현금흐름으로 동결된 상태에서는 해제를 거부합니다.

## 유량 보정 성과(TWR/HWM)

최초 평가액 > 0 이면 단위가격 100 으로 시작합니다. 입금·출금은 **직전 마감 스냅샷 단위가격**으로 단위 수만 조정하며
`DISTRIBUTION` 은 성과에 남깁니다. HWM 은 마감 스냅샷에서, 미분류 현금흐름이 없을 때만 올립니다(장중 갱신 금지).
전액 출금(단위 수 ≤ 1e-6)이면 epoch 를 닫고 0 으로 나누지 않으며, 재입금 시 직전 단위가격에서 새 epoch 로 이어갑니다.
