"""Sealed, local-only CTP owner snapshots; these grant no execution authority."""

from __future__ import annotations

from dataclasses import dataclass, field

from bt_api_ctp.instrument_status_observation import InstrumentStatusView
from bt_api_ctp.registered_sim_session import CtpQuerySessionBinding

_SNAPSHOT_SEAL = object()


@dataclass(frozen=True, slots=True)
class _SnapshotCut:
    venue: str
    native_symbol: str
    view: InstrumentStatusView | None
    session: object
    binding: CtpQuerySessionBinding
    provider: object
    bridge: object
    listener: object
    lock: object
    revision: int
    seal: object


@dataclass(frozen=True, slots=True)
class CtpGatewayInstrumentStatusSnapshot:
    """One scoped observation at a local owner cut, not continuity evidence."""

    venue: str
    native_symbol: str
    view: InstrumentStatusView | None = field(repr=False)
    _cut: _SnapshotCut = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _snapshot_is_sealed(self):
            raise ValueError("instrument status snapshot is unsealed")

    @property
    def authorizes_trading(self) -> bool:
        return False

    @property
    def continuity_proven(self) -> bool:
        return False

    @property
    def session_generation(self) -> str:
        return str(self._cut.binding.connection_generation)

    @property
    def trading_day(self) -> str:
        return self._cut.binding.trading_day


def _make_snapshot(
    *, venue, native_symbol, view, session, binding, provider, bridge, listener, lock, revision
):
    cut = _SnapshotCut(
        venue,
        native_symbol,
        view,
        session,
        binding,
        provider,
        bridge,
        listener,
        lock,
        revision,
        _SNAPSHOT_SEAL,
    )
    return CtpGatewayInstrumentStatusSnapshot(venue, native_symbol, view, cut)


def _snapshot_is_sealed(snapshot: object) -> bool:
    try:
        if type(snapshot) is not CtpGatewayInstrumentStatusSnapshot:
            return False
        cut = snapshot._cut
        return bool(
            type(cut) is _SnapshotCut
            and cut.seal is _SNAPSHOT_SEAL
            and type(snapshot.venue) is str
            and type(snapshot.native_symbol) is str
            and snapshot.venue == cut.venue
            and snapshot.native_symbol == cut.native_symbol
            and snapshot.view is cut.view
        )
    except Exception:
        return False
