from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class PerformanceState:
    unit_nav: float
    units: float
    hwm: float
    twr_cumulative: float
    drawdown: float


def initialize_flow_adjusted_performance(
    *,
    starting_equity: float,
) -> PerformanceState:
    if starting_equity <= 0:
        raise ValueError("Starting equity must be positive.")
    unit_nav = 100.0
    units = starting_equity / unit_nav
    return PerformanceState(
        unit_nav=unit_nav,
        units=units,
        hwm=unit_nav,
        twr_cumulative=0.0,
        drawdown=0.0,
    )


def apply_external_cash_flow(
    state: PerformanceState,
    *,
    cash_flow: float,
) -> PerformanceState:
    if state.unit_nav <= 0 or state.units <= 0:
        raise ValueError("Performance state is invalid.")
    new_units = state.units + cash_flow / state.unit_nav
    if new_units <= 0:
        raise ValueError("Cash flow would make unit count non-positive.")
    return PerformanceState(
        unit_nav=state.unit_nav,
        units=new_units,
        hwm=state.hwm,
        twr_cumulative=state.twr_cumulative,
        drawdown=state.drawdown,
    )


def update_flow_adjusted_performance(
    state: PerformanceState,
    *,
    total_equity: float,
    benchmark_values: Iterable[float] | None = None,
) -> PerformanceState:
    del benchmark_values
    if total_equity <= 0:
        raise ValueError("Total equity must be positive.")
    if state.units <= 0:
        raise ValueError("Performance state is invalid.")

    unit_nav = total_equity / state.units
    hwm = max(state.hwm, unit_nav)
    twr_cumulative = unit_nav / 100.0 - 1.0
    drawdown = unit_nav / hwm - 1.0
    return PerformanceState(
        unit_nav=unit_nav,
        units=state.units,
        hwm=hwm,
        twr_cumulative=twr_cumulative,
        drawdown=drawdown,
    )
