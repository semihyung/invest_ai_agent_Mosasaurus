"""Telegram 명령 콜백(Phase 1): `run_telegram_polling` 의 `command_actions` 를 DB·운영 동작에 연결합니다(명세 6.4절).

콜백 규격은 `(chat_id, 명령 전체 문자열) -> 답장 문자열` 입니다. 인증·화이트리스트·속도 제한·2단계 확인(`/confirm`)은
`telegram_bot.TelegramCommandHandler` 가 이미 통과시킨 뒤에만 호출되므로, 여기서는 **동작 자체**만 수행합니다.
2단계 명령은 `pending_actions` 의 `APPROVED` 이력(명령 해시 일치, 가장 최근 확인)을 찾아 `operator_actions` 에 넘깁니다.
답장에는 계좌·토큰을 넣지 않으며, 예외 내용은 노출하지 않고 고정 문구만 돌려줍니다(전송 시 한 번 더 마스킹됨).
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

from src import operator_actions as ops
from src.config import load_config, strategy_params_hash
from src.ops_jobs import compose_briefing
from src.reconciliation import Position
from src.safety_flags import active_halt_reasons, is_safe_halt

KST = timezone(timedelta(hours=9), name="KST")


class _Refused(Exception):
    """실행하지 않고 사유만 안내할 때 쓴다(승인은 소진하지 않는다)."""


FAIL = "처리하지 못했습니다. 상태와 인자를 확인하세요(자세한 원인은 로그에 기록됨)."


def _set_state(connection: sqlite3.Connection, key: str, value: str, now: datetime) -> None:
    """system_state 한 값을 UPSERT 한다."""
    with connection:
        connection.execute(
            'INSERT INTO system_state ("key", value, updated_at) VALUES (?, ?, ?) '
            'ON CONFLICT("key") DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at',
            (key, value, now.isoformat()))


def _state(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute('SELECT value FROM system_state WHERE "key" = ?', (key,)).fetchone()
    return row[0] if row else None


APPROVAL_VALID_FOR = timedelta(minutes=10)  # 확인 직후 실행되는 것이 정상이므로 오래된 승인은 쓰지 않는다


def _approved_action_id(connection: sqlite3.Connection, command: str, now: datetime | None = None) -> str:
    """이 명령 문자열로 가장 최근에 `APPROVED` 된(그리고 아직 쓰이지 않은) 2단계 확인의 ID(없으면 예외)."""
    row = connection.execute(
        "SELECT id, confirmed_at FROM pending_actions WHERE approval_status = 'APPROVED' AND args_hash = ? "
        "ORDER BY confirmed_at DESC LIMIT 1", (hashlib.sha256(command.encode("utf-8")).hexdigest(),)).fetchone()
    if row is None:
        raise ops.ApprovalError("No approved confirmation for this command.")
    if now is not None and row["confirmed_at"]:
        confirmed = datetime.fromisoformat(row["confirmed_at"])
        confirmed = confirmed if confirmed.tzinfo else confirmed.replace(tzinfo=timezone.utc)
        if now - confirmed > APPROVAL_VALID_FOR:
            raise ops.ApprovalError("The approved confirmation is too old.")
    return row["id"]


def _consume_approval(connection: sqlite3.Connection, command: str) -> None:
    """성공한 명령의 승인을 소진한다(`EXPIRED`): 같은 확인으로 명령을 다시 실행하지 못하게 한다(감사 행은 유지)."""
    with connection:
        connection.execute(
            "UPDATE pending_actions SET approval_status = 'EXPIRED' WHERE approval_status = 'APPROVED' "
            "AND args_hash = ?", (hashlib.sha256(command.encode("utf-8")).hexdigest(),))


def build_command_actions(
    connection_factory: Callable[[], sqlite3.Connection],
    *,
    approvals_dir: str | Path,
    backtest_dir: str | Path = "/run/backtests",
    broker_positions: Callable[[], list[Position]] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Mapping[str, Callable[[str, str], str]]:
    """`/confirm` 을 제외한 Phase 1 명령 19종의 콜백 표를 만든다(`/add` 는 봇이 직접 처리)."""

    def guarded(fn: Callable[[sqlite3.Connection, list[str], datetime], str]) -> Callable[[str, str], str]:
        """연결 수명 관리와 예외 → 고정 문구 변환을 한 곳에서 한다."""

        def action(_chat_id: str, text: str) -> str:
            connection = connection_factory()
            try:
                reply = fn(connection, text.split(), clock())
                _consume_approval(connection, " ".join(text.split()))  # 승인이 없던 명령이면 아무 일도 없다
                return reply
            except _Refused as refusal:
                return str(refusal)
            except (ops.ApprovalError, ValueError, KeyError, sqlite3.Error, OSError) as error:
                with connection:
                    connection.execute(
                        "INSERT INTO system_events (component, event_type, severity, message, created_at) "
                        "VALUES ('telegram_actions', 'COMMAND_FAILED', 'WARN', ?, ?)",
                        (f"{text.split()[0]}: {type(error).__name__}", clock().isoformat()))
                return FAIL
            finally:
                connection.close()

        return action

    # ───── 조회 ─────
    def status(c, args, now):
        mode = ("SAFE_HALT" if is_safe_halt(c) else "EMERGENCY" if _state(c, "emergency_mode") == "true"
                else "PAUSED" if _state(c, "is_paused") == "true"
                else "TRAINING" if _state(c, "training_mode") == "true" else "NORMAL")
        return f"운영 모드: {mode}\n" + compose_briefing(c, now)

    def preview(c, args, now):
        row = c.execute("SELECT * FROM weekly_previews ORDER BY created_at DESC, preview_revision DESC LIMIT 1").fetchone()
        if row is None:
            return "저장된 주간 프리뷰가 없습니다."
        import json
        ranked = json.loads(row["ranked_candidates_json"])
        names = ("RF_INVALID: 교체 생략(" + ranked.get("reason", "") + ")") if isinstance(ranked, dict) else \
            ", ".join(item["ticker"] for item in ranked[:10]) or "후보 없음"
        return f"프리뷰 기준일 {row['base_candle_date']} / 상태 {row['status']}\n순위: {names}"

    def pool(c, args, now):
        lines = []
        for r in c.execute("SELECT ticker, category, data_status FROM universe_assets WHERE status = 'ACTIVE' "
                           "ORDER BY category, ticker"):
            candles = c.execute("SELECT adjusted_close FROM daily_candles WHERE ticker = ? "
                                "ORDER BY trade_date DESC LIMIT 120", (r["ticker"],)).fetchall()
            sma = f"{sum(x[0] for x in candles) / 120:,.0f}" if len(candles) == 120 else "-"
            close = f"{candles[0][0]:,.0f}" if candles else "-"
            lines.append(f"{r['ticker']} {r['category']} {r['data_status']} 종가 {close} / 120MA {sma}")
        return "\n".join(lines) or "활성 종목이 없습니다."

    def trades(c, args, now):
        limit = int(args[1]) if len(args) == 2 else 10
        rows = c.execute("SELECT ticker, side, requested_qty, filled_qty, status, order_price, submitted_at "
                         "FROM orders ORDER BY submitted_at DESC LIMIT ?", (limit,)).fetchall()
        return "\n".join(f"{r['submitted_at'][:16]} {r['ticker']} {r['side']} {r['filled_qty']}/{r['requested_qty']}주 "
                         f"@{r['order_price']:,} {r['status']}" for r in rows) or "주문 내역이 없습니다."

    def health(c, args, now):
        last = _state(c, "last_reconciliation_at") or "없음"
        return (f"대조 상태: {_state(c, 'reconciliation_ok') or '미확인'} / 마지막 대조: {last}\n"
                f"주문 잠금: {_state(c, 'scheduler_order_lock') or '미설정'} / "
                f"SAFE_HALT 원인: {', '.join(active_halt_reasons(c)) or '없음'}\n"
                f"스탑 감시 주기: {_state(c, 'guard_poll_interval_seconds') or '10'}초"
                + (" (한도로 연장됨: 스탑 판정이 그만큼 지연될 수 있음)"
                   if int(_state(c, 'guard_poll_interval_seconds') or 10) > 10 else ""))

    def reconcile(c, args, now):
        if args[1:] == ["adopt"]:
            if broker_positions is None:
                raise _Refused("증권사 조회가 연결되지 않아 채택할 수 없습니다.")
            diff = ops.adopt_broker_positions(
                c, {p.ticker: p.quantity for p in broker_positions()},
                action_id=_approved_action_id(c, " ".join(args), now), now=now)
            return f"KIS 보유 수량을 채택했습니다(차이 {len(diff)}건). 대조는 다음 마감 후 잡에서 재확인됩니다."
        return (f"대조 상태: {_state(c, 'reconciliation_ok') or '미확인'} / 마지막 대조: "
                f"{_state(c, 'last_reconciliation_at') or '없음'}\n수동 채택은 /reconcile adopt (2단계 확인)")

    # ───── 운영 모드 ─────
    def pause(c, args, now):
        _set_state(c, "is_paused", "true", now)
        return "일시정지: 신규 매수를 막고 보호 매도·취소만 허용합니다. 해제는 /resume (2단계 확인)."

    def resume(c, args, now):
        if is_safe_halt(c):
            raise _Refused("SAFE_HALT 중에는 /resume 으로 풀 수 없습니다. /clear_halt 로 원인을 해제하세요.")
        _approved_action_id(c, " ".join(args), now)
        _set_state(c, "is_paused", "false", now)
        return "일시정지를 해제했습니다."

    def training(c, args, now):
        _approved_action_id(c, " ".join(args), now)
        if args[1] == "off":
            _set_state(c, "training_mode", "false", now)
            _set_state(c, "training_until", "", now)
            return "훈련 모드를 해제했습니다(다음 평가부터 정상 규칙)."
        until = (now.astimezone(KST).date() + timedelta(days=int(args[2]))).isoformat() if len(args) == 3 else ""
        _set_state(c, "training_mode", "true", now)
        _set_state(c, "training_until", until, now)
        return f"훈련 모드 활성화(스탑 -7%, 위험자산 매수 차단){' / 종료일 ' + until if until else ' / 무기한'}"

    def emergency(c, args, now):
        _approved_action_id(c, " ".join(args), now)
        if len(args) == 2:  # off
            rebase = ops.release_emergency(
                c, action_id=_approved_action_id(c, " ".join(args), now),
                floor_until_date=(now.astimezone(KST).date() + timedelta(days=7)).isoformat(), now=now)
            return "EMERGENCY 를 해제했습니다." + (" HWM 리베이스가 예약되었습니다." if rebase else "")
        _set_state(c, "emergency_mode", "true", now)
        _set_state(c, "emergency_reason", "MANUAL", now)
        return "EMERGENCY 활성화: 위험 축소 매도만 허용합니다."

    # ───── 운영자 승인·정정 ─────
    def clear_halt(c, args, now):
        ok = ops.clear_halt(c, code=args[1].upper(), action_id=_approved_action_id(c, " ".join(args), now),
                            reconciliation_ok=_state(c, "reconciliation_ok") == "true", now=now)
        if not ok:
            raise _Refused(f"{args[1].upper()} 는 해제 조건(대조 성공·수동 해제 원인)을 만족하지 않아 유지됩니다.")
        return f"{args[1].upper()} 해제했습니다."

    def classify(c, args, now):
        ops.classify_cash_flow(c, flow_id=int(args[1]), flow_type=args[2].upper(),
                               action_id=_approved_action_id(c, " ".join(args), now), now=now)
        return f"현금흐름 {args[1]} 을(를) {args[2].upper()} 로 처리했습니다."

    def ca_confirm(c, args, now):
        factor = ops.confirm_corporate_action(c, corporate_action_id=int(args[1]),
                                              action_id=_approved_action_id(c, " ".join(args), now), now=now)
        return f"기업행위 {args[1]} 검증 완료(조정계수 {factor:.4f}), PeakPrice 재기준화·격리 해제."

    def remove(c, args, now):
        _approved_action_id(c, " ".join(args), now)
        with c:
            cur = c.execute("UPDATE universe_assets SET status = 'REMOVED', removed_at = ? WHERE ticker = ? "
                            "AND status = 'ACTIVE'", (now.isoformat(), args[1]))
        if cur.rowcount != 1:
            raise ValueError("Ticker is not an active universe asset.")
        return f"{args[1]} 을(를) 유니버스에서 제거했습니다. 보유 중이면 다음 15:00 판정에서 퇴출 매도됩니다."

    def approve_real(c, args, now):
        ops.approve_real_activation(c, approval_id=args[1], action_id=_approved_action_id(c, " ".join(args), now),
                                    approvals_dir=approvals_dir, now=now)
        return "REAL 활성화 승인 레코드를 만들었습니다(호스트 승인 파일과 일치 확인). 실계좌 연결은 별도 단계입니다."

    def approve_smoke(c, args, now):
        ops.approve_smoke_session(c, approval_id=args[1], action_id=_approved_action_id(c, " ".join(args), now),
                                  approvals_dir=approvals_dir, now=now)
        return "스모크 세션(최대 60분)을 승인했습니다."

    def approve_backtest(c, args, now):
        """`/approve_backtest [run_id] [report_sha256]`: 보고서 파일의 정규화 해시가 인자와 같고, 보고서의 전략 해시가
        현재 설정과 같을 때만 승인한다(다른 전략의 백테스트로 승인되는 일을 막는다)."""
        action_id = _approved_action_id(c, " ".join(args), now)
        run_id, digest = args[1], args[2]
        report_path = Path(backtest_dir) / f"{run_id}.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != digest:
            raise ValueError("Report hash does not match.")
        if report["strategy_params_hash"] != strategy_params_hash(load_config()):
            raise ValueError("Report was produced for a different strategy configuration.")
        start, end = report["data_range"]
        ops.approve_backtest(c, run_id=run_id, report_sha256=digest, code_commit_sha=report["code_commit_sha"],
                             strategy_params_hash=report["strategy_params_hash"], data_range=(start, end),
                             action_id=action_id, now=now)
        return f"백테스트 {run_id} 승인 완료(전략 해시 일치, 기간 {start}~{end})."

    return {
        "/status": guarded(status), "/preview": guarded(preview), "/pool": guarded(pool),
        "/trades": guarded(trades), "/health": guarded(health), "/reconcile": guarded(reconcile),
        "/pause": guarded(pause), "/stop": guarded(pause), "/resume": guarded(resume),
        "/training": guarded(training), "/emergency": guarded(emergency),
        "/remove": guarded(remove), "/clear_halt": guarded(clear_halt), "/classify": guarded(classify),
        "/ca_confirm": guarded(ca_confirm), "/approve_real": guarded(approve_real),
        "/approve_smoke": guarded(approve_smoke), "/approve_backtest": guarded(approve_backtest),
        "/add": lambda _chat, _text: "종목 추가는 공식 메타데이터 확인 후 봇이 직접 처리합니다.",
    }
