from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROFILE_SCHEMA = "polymarket_paper_risk_profile_1"
ACTIVE_PROFILE_STATUS = "FROZEN_PAPER_ONLY"

REQUIRED_LIMITS = (
    "max_order_shares",
    "max_order_cash",
    "max_open_positions",
    "max_open_cash",
    "max_session_loss",
    "max_drawdown",
    "max_consecutive_losses",
)

REQUIRED_DISABLED_CONTROLS = (
    "orders_enabled",
    "wallet_required",
    "private_api_required",
    "kelly_enabled",
    "leverage_enabled",
    "compounding_enabled",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite_positive(name: str, value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} debe ser numerico") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} debe ser finito y mayor que cero")
    return parsed


def _positive_integer(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} debe ser entero positivo")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} debe ser entero positivo") from exc
    if parsed <= 0 or float(parsed) != float(value):
        raise ValueError(f"{name} debe ser entero positivo")
    return parsed


def load_profile(path: str | Path) -> dict[str, Any]:
    profile_path = Path(path).expanduser().resolve()
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("El perfil de riesgo debe ser un objeto JSON")
    if payload.get("schema") != PROFILE_SCHEMA:
        raise ValueError("Schema de perfil de riesgo incompatible")
    return payload


def paper_risk_status(
    profile_path: str | Path,
    *,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    path = Path(profile_path).expanduser().resolve()
    profile = load_profile(path)
    root = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else path.parent.parent
    )

    limits = profile.get("limits")
    derived = profile.get("derived_limits", {})
    pending = profile.get("pending_limits", {})
    effective = limits if isinstance(limits, dict) else {**derived, **pending}
    missing_limits = [
        name
        for name in REQUIRED_LIMITS
        if not isinstance(effective, dict) or effective.get(name) is None
    ]

    controls = profile.get("controls", {})
    unsafe_controls = [
        name
        for name in REQUIRED_DISABLED_CONTROLS
        if not isinstance(controls, dict) or controls.get(name) is not False
    ]

    source_checks: list[dict[str, Any]] = []
    sources = profile.get("sources", {})
    if not isinstance(sources, dict):
        sources = {}
    for relative, expected in sorted(sources.items()):
        source = (root / relative).resolve()
        exists = source.is_file()
        actual = _sha256(source) if exists else None
        source_checks.append(
            {
                "path": relative,
                "exists": exists,
                "expected_sha256": expected,
                "actual_sha256": actual,
                "matches": exists and actual == expected,
            }
        )

    sources_declared = bool(sources)
    source_mismatches = [
        check["path"] for check in source_checks if not check["matches"]
    ]
    active = profile.get("status") == ACTIVE_PROFILE_STATUS
    limit_validation_error = None
    if active and not missing_limits and not unsafe_controls:
        try:
            RiskLimits.from_profile(profile)
        except ValueError as exc:
            limit_validation_error = str(exc)
    ready = (
        active
        and not missing_limits
        and not unsafe_controls
        and limit_validation_error is None
        and sources_declared
        and not source_mismatches
    )
    blockers = []
    if not active:
        blockers.append("PROFILE_NOT_FROZEN_PAPER_ONLY")
    if missing_limits:
        blockers.append("MISSING_LIMITS")
    if unsafe_controls:
        blockers.append("UNSAFE_CONTROLS")
    if limit_validation_error is not None:
        blockers.append("INVALID_LIMITS")
    if not sources_declared:
        blockers.append("NO_SOURCE_CONTRACT")
    if source_mismatches:
        blockers.append("SOURCE_HASH_MISMATCH")

    return {
        "schema": PROFILE_SCHEMA,
        "profile": str(path),
        "profile_status": profile.get("status"),
        "ready_for_paper_risk_engine": ready,
        "missing_limits": missing_limits,
        "limit_validation_error": limit_validation_error,
        "unsafe_controls": unsafe_controls,
        "source_checks": source_checks,
        "sources_declared": sources_declared,
        "source_mismatches": source_mismatches,
        "blockers": blockers,
        "orders_enabled": False,
        "wallet_required": False,
        "real_money": "BLOQUEADO",
    }


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_shares: float
    max_order_cash: float
    max_open_positions: int
    max_open_cash: float
    max_session_loss: float
    max_drawdown: float
    max_consecutive_losses: int

    def __post_init__(self) -> None:
        for name in (
            "max_order_shares",
            "max_order_cash",
            "max_open_cash",
            "max_session_loss",
            "max_drawdown",
        ):
            object.__setattr__(
                self,
                name,
                _finite_positive(name, getattr(self, name)),
            )
        object.__setattr__(
            self,
            "max_open_positions",
            _positive_integer("max_open_positions", self.max_open_positions),
        )
        object.__setattr__(
            self,
            "max_consecutive_losses",
            _positive_integer(
                "max_consecutive_losses", self.max_consecutive_losses
            ),
        )
        if self.max_open_cash + 1e-12 < self.max_order_cash:
            raise ValueError(
                "max_open_cash no puede ser menor que max_order_cash"
            )

    @classmethod
    def from_profile(cls, profile: dict[str, Any]) -> "RiskLimits":
        if profile.get("schema") != PROFILE_SCHEMA:
            raise ValueError("Schema de perfil de riesgo incompatible")
        if profile.get("status") != ACTIVE_PROFILE_STATUS:
            raise ValueError("El perfil de riesgo no esta congelado para paper")

        controls = profile.get("controls")
        if not isinstance(controls, dict):
            raise ValueError("Faltan controles de seguridad")
        unsafe = [
            name
            for name in REQUIRED_DISABLED_CONTROLS
            if controls.get(name) is not False
        ]
        if unsafe:
            raise ValueError(
                "Controles inseguros en perfil: " + ",".join(unsafe)
            )

        limits = profile.get("limits")
        if not isinstance(limits, dict):
            raise ValueError("Falta bloque limits")
        missing = [name for name in REQUIRED_LIMITS if limits.get(name) is None]
        if missing:
            raise ValueError("Faltan limites: " + ",".join(missing))

        return cls(
            max_order_shares=_finite_positive(
                "max_order_shares", limits["max_order_shares"]
            ),
            max_order_cash=_finite_positive(
                "max_order_cash", limits["max_order_cash"]
            ),
            max_open_positions=_positive_integer(
                "max_open_positions", limits["max_open_positions"]
            ),
            max_open_cash=_finite_positive(
                "max_open_cash", limits["max_open_cash"]
            ),
            max_session_loss=_finite_positive(
                "max_session_loss", limits["max_session_loss"]
            ),
            max_drawdown=_finite_positive(
                "max_drawdown", limits["max_drawdown"]
            ),
            max_consecutive_losses=_positive_integer(
                "max_consecutive_losses",
                limits["max_consecutive_losses"],
            ),
        )


@dataclass(frozen=True, slots=True)
class PaperOrderProposal:
    condition_id: str
    strategy_id: str
    side: str
    entry_cost_per_share: float
    shares: float
    decision_timestamp_ms: int
    executable: bool
    data_fresh: bool
    real_money: int = 0

    def __post_init__(self) -> None:
        if not self.condition_id.strip():
            raise ValueError("condition_id no puede estar vacio")
        if not self.strategy_id.strip():
            raise ValueError("strategy_id no puede estar vacio")
        if self.side not in ("UP", "DOWN"):
            raise ValueError("side debe ser UP o DOWN")
        object.__setattr__(
            self,
            "entry_cost_per_share",
            _finite_positive(
                "entry_cost_per_share", self.entry_cost_per_share
            ),
        )
        object.__setattr__(
            self,
            "shares",
            _finite_positive("shares", self.shares),
        )
        if self.entry_cost_per_share > 1.0:
            raise ValueError("entry_cost_per_share no puede superar 1")
        if (
            isinstance(self.decision_timestamp_ms, bool)
            or not isinstance(self.decision_timestamp_ms, int)
            or self.decision_timestamp_ms <= 0
        ):
            raise ValueError("decision_timestamp_ms debe ser positivo")
        if not isinstance(self.executable, bool):
            raise ValueError("executable debe ser booleano")
        if not isinstance(self.data_fresh, bool):
            raise ValueError("data_fresh debe ser booleano")
        if type(self.real_money) is not int or self.real_money not in (0, 1):
            raise ValueError("real_money debe ser 0 o 1")

    @property
    def cash_outlay(self) -> float:
        return self.entry_cost_per_share * self.shares


@dataclass(frozen=True, slots=True)
class PaperPosition:
    condition_id: str
    strategy_id: str
    side: str
    entry_cost_per_share: float
    shares: float
    cash_outlay: float
    decision_timestamp_ms: int
    real_money: int = 0


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reasons: tuple[str, ...]
    cash_outlay: float
    open_positions_before: int
    open_cash_before: float
    real_money: int = 0


@dataclass(slots=True)
class PaperRiskState:
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    realized_pnl: float = 0.0
    peak_realized_pnl: float = 0.0
    consecutive_losses: int = 0
    kill_switch: bool = False

    @property
    def open_cash(self) -> float:
        return sum(position.cash_outlay for position in self.positions.values())

    @property
    def drawdown(self) -> float:
        return self.peak_realized_pnl - self.realized_pnl


class PaperRiskEngine:
    """Motor determinista paper-only; no contiene red, wallet ni ordenes."""

    def __init__(
        self,
        limits: RiskLimits,
        state: PaperRiskState | None = None,
    ) -> None:
        self.limits = limits
        self.state = state if state is not None else PaperRiskState()

    def activate_kill_switch(self) -> None:
        self.state.kill_switch = True

    def evaluate(self, proposal: PaperOrderProposal) -> RiskDecision:
        reasons: list[str] = []
        open_positions = len(self.state.positions)
        open_cash = self.state.open_cash
        outlay = proposal.cash_outlay

        if proposal.real_money != 0:
            reasons.append("REAL_MONEY_FORBIDDEN")
        if self.state.kill_switch:
            reasons.append("KILL_SWITCH")
        if not proposal.executable:
            reasons.append("NOT_EXECUTABLE")
        if not proposal.data_fresh:
            reasons.append("STALE_DATA")
        if proposal.condition_id in self.state.positions:
            reasons.append("DUPLICATE_POSITION")
        if proposal.shares > self.limits.max_order_shares + 1e-12:
            reasons.append("MAX_ORDER_SHARES")
        if outlay > self.limits.max_order_cash + 1e-12:
            reasons.append("MAX_ORDER_CASH")
        if open_positions >= self.limits.max_open_positions:
            reasons.append("MAX_OPEN_POSITIONS")
        if open_cash + outlay > self.limits.max_open_cash + 1e-12:
            reasons.append("MAX_OPEN_CASH")
        worst_case_pnl = self.state.realized_pnl - open_cash - outlay
        if (
            self.state.realized_pnl <= -self.limits.max_session_loss
            or worst_case_pnl < -self.limits.max_session_loss - 1e-12
        ):
            reasons.append("MAX_SESSION_LOSS")
        worst_case_drawdown = self.state.peak_realized_pnl - worst_case_pnl
        if (
            self.state.drawdown >= self.limits.max_drawdown
            or worst_case_drawdown > self.limits.max_drawdown + 1e-12
        ):
            reasons.append("MAX_DRAWDOWN")
        if (
            self.state.consecutive_losses
            >= self.limits.max_consecutive_losses
        ):
            reasons.append("MAX_CONSECUTIVE_LOSSES")

        unique_reasons = tuple(dict.fromkeys(reasons))
        return RiskDecision(
            approved=not unique_reasons,
            reasons=unique_reasons,
            cash_outlay=outlay,
            open_positions_before=open_positions,
            open_cash_before=open_cash,
            real_money=0,
        )

    def record_paper_fill(
        self,
        proposal: PaperOrderProposal,
    ) -> RiskDecision:
        decision = self.evaluate(proposal)
        if not decision.approved:
            return decision
        self.state.positions[proposal.condition_id] = PaperPosition(
            condition_id=proposal.condition_id,
            strategy_id=proposal.strategy_id,
            side=proposal.side,
            entry_cost_per_share=proposal.entry_cost_per_share,
            shares=proposal.shares,
            cash_outlay=proposal.cash_outlay,
            decision_timestamp_ms=proposal.decision_timestamp_ms,
            real_money=0,
        )
        return decision

    def settle_paper_position(
        self,
        condition_id: str,
        outcome: str,
    ) -> float:
        if outcome not in ("UP", "DOWN"):
            raise ValueError("outcome debe ser UP o DOWN")
        try:
            position = self.state.positions.pop(condition_id)
        except KeyError as exc:
            raise ValueError("No existe posicion paper abierta") from exc

        won = position.side == outcome
        pnl = (
            position.shares - position.cash_outlay
            if won
            else -position.cash_outlay
        )
        self.state.realized_pnl += pnl
        self.state.peak_realized_pnl = max(
            self.state.peak_realized_pnl,
            self.state.realized_pnl,
        )
        self.state.consecutive_losses = (
            0 if pnl > 0 else self.state.consecutive_losses + 1
        )
        return pnl


def load_active_limits(
    profile_path: str | Path,
    *,
    project_root: str | Path | None = None,
) -> RiskLimits:
    status = paper_risk_status(profile_path, project_root=project_root)
    if not status["ready_for_paper_risk_engine"]:
        raise ValueError(
            "Perfil paper no activable: " + ",".join(status["blockers"])
        )
    return RiskLimits.from_profile(load_profile(profile_path))


__all__ = [
    "ACTIVE_PROFILE_STATUS",
    "PROFILE_SCHEMA",
    "PaperOrderProposal",
    "PaperPosition",
    "PaperRiskEngine",
    "PaperRiskState",
    "RiskDecision",
    "RiskLimits",
    "load_active_limits",
    "load_profile",
    "paper_risk_status",
]
