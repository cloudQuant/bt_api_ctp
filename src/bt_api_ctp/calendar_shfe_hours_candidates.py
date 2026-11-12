# ruff: noqa: UP045
"""Unverified SHFE/INE day-session candidates from an already parsed page.

Only caller-specified identities and explicit page day phases are projected.
Night phases and notes remain separate review material; empty night_mappings
never means no night session. The caller-supplied source hash, product names,
contracts, dates and scope are not authenticated, and this is not a coverage,
listing, live status or publication decision. No network, filesystem or account
access occurs. Inputs must not be concurrently modified during conversion.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

from bt_api_ctp.calendar_shfe_page import (
    MAX_ROWS,
    PageCandidate,
    ProductCandidate,
    StageCandidate,
    parse_shfe_page,
)
from bt_api_ctp.calendar_supplement import MAX_LIST_ITEMS, SupplementCandidate, _validate

_SOURCE = "https://www.shfe.com.cn/services/calenderandholidays/tradinghours/"
_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
_MAX_SELECTIONS = 128
_MAX_CONTRACTS = 4096
_MAX_RAW = 1024 * 1024


@dataclass(frozen=True)
class HoursSelection:
    venue: str
    kind: str
    page_product_name: str
    product: str
    contracts: tuple[str, ...]
    valid_from: date
    valid_to: date


@dataclass(frozen=True)
class NightReview:
    venue: str
    kind: str
    page_product_name: str
    product: str
    contracts: tuple[str, ...]
    stages: tuple[StageCandidate, ...]
    availability: str = "UNKNOWN"
    trading_day_mapping: str = "UNKNOWN"


@dataclass(frozen=True)
class HoursCandidateResult:
    status: str
    supplement: Optional[SupplementCandidate] = None
    unresolved_nights: tuple[NightReview, ...] = ()
    page_notes: tuple[str, ...] = ()
    page_review_reasons: tuple[str, ...] = ()
    reason: str = ""
    verification: str = "UNVERIFIED"
    coverage: str = "NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"


def _valid_id(value):
    return type(value) is str and _ID.fullmatch(value) is not None


def _cell(value):
    return "<br>".join(html.escape(line) for line in value.split("\n"))


def _recheck_page(page, *, source_url=_SOURCE):
    if (
        type(page) is not PageCandidate
        or type(page.status) is not str
        or page.status not in {"CANDIDATE", "NEEDS_REVIEW"}
        or page.verification != "UNVERIFIED"
        or type(page.verification) is not str
        or type(page.source_id) is not str
        or page.source_id != source_url
        or type(page.sha256) is not str
        or _HASH.fullmatch(page.sha256) is None
        or type(page.products) is not tuple
        or not 1 <= len(page.products) <= 1024
        or type(page.raw_notes) is not tuple
        or len(page.raw_notes) > 256
        or type(page.review_reasons) is not tuple
        or any(type(reason) is not str for reason in page.review_reasons)
    ):
        raise ValueError("invalid page")
    rows = []
    row_set = set()
    products = {}
    for product in page.products:
        if (
            type(product) is not ProductCandidate
            or type(product.kind) is not str
            or product.kind not in {"future", "option"}
            or type(product.name) is not str
            or type(product.raw_product_cell) is not str
            or type(product.raw_time_cell) is not str
            or len(product.raw_product_cell) > 8192
            or len(product.raw_time_cell) > 8192
            or type(product.stages) is not tuple
            or not 1 <= len(product.stages) <= 256
            or any(type(stage) is not StageCandidate for stage in product.stages)
            or any(
                type(getattr(stage, field)) is not str
                for stage in product.stages
                for field in ("session", "phase", "start", "end", "day_transition", "raw_line")
            )
        ):
            raise ValueError("invalid product")
        key = (product.kind, product.name)
        if key in products:
            raise ValueError("duplicate page product identity")
        products[key] = product
        raw = (product.raw_product_cell, product.raw_time_cell)
        if raw not in row_set:
            row_set.add(raw)
            rows.append(f"<tr><td>{_cell(raw[0])}</td><td>{_cell(raw[1])}</td></tr>")
    for note in page.raw_notes:
        if type(note) is not str or len(note) > 8192:
            raise ValueError("invalid note")
        rows.append(f'<tr><td colspan="2">{_cell(note)}</td></tr>')
    document = "".join(
        "<table>" + "".join(rows[index : index + MAX_ROWS]) + "</table>"
        for index in range(0, len(rows), MAX_ROWS)
    ).encode("utf-8", "strict")
    if len(document) > _MAX_RAW:
        raise ValueError("page budget exceeded")
    reparsed = parse_shfe_page(source_id=source_url, html=document)
    replay = {(p.kind, p.name): p for p in reparsed.products}
    if (
        reparsed.status != page.status
        or reparsed.review_reasons != page.review_reasons
        or reparsed.raw_notes != page.raw_notes
        or len(replay) != len(reparsed.products)
        or replay != products
    ):
        raise ValueError("page candidate inconsistent with cells")
    return products


def convert_shfe_hours(
    page: PageCandidate, *, selections: tuple[HoursSelection, ...]
) -> HoursCandidateResult:
    """Project explicitly selected day phases, never infer night or identity."""
    return _convert_hours(
        page, selections=selections, source_url=_SOURCE, prefix="shfe", venues={"SHFE", "INE"}
    )


def _convert_hours(page, *, selections, source_url, prefix, venues):
    try:
        products = _recheck_page(page, source_url=source_url)
        if type(selections) is not tuple or not 1 <= len(selections) <= _MAX_SELECTIONS:
            raise ValueError("invalid selections")
        page_keys = set()
        product_keys = set()
        contract_keys = set()
        total_contracts = 0
        for selected in selections:
            if (
                type(selected) is not HoursSelection
                or type(selected.venue) is not str
                or selected.venue not in venues
                or type(selected.kind) is not str
                or selected.kind not in {"future", "option"}
                or type(selected.page_product_name) is not str
                or not _valid_id(selected.product)
                or type(selected.contracts) is not tuple
                or not 1 <= len(selected.contracts) <= MAX_LIST_ITEMS
                or any(not _valid_id(contract) for contract in selected.contracts)
                or len(set(selected.contracts)) != len(selected.contracts)
                or type(selected.valid_from) is not date
                or type(selected.valid_to) is not date
                or selected.valid_from > selected.valid_to
            ):
                raise ValueError("invalid selection")
            page_key = (selected.kind, selected.page_product_name)
            product_key = (selected.venue, selected.kind, selected.product)
            if page_key not in products or page_key in page_keys or product_key in product_keys:
                raise ValueError("unknown or duplicate product")
            page_keys.add(page_key)
            product_keys.add(product_key)
            for contract in selected.contracts:
                key = (selected.venue, contract)
                if key in contract_keys:
                    raise ValueError("ambiguous contract identity")
                contract_keys.add(key)
            total_contracts += len(selected.contracts)
            if total_contracts > _MAX_CONTRACTS:
                raise ValueError("selection budget exceeded")
        source_id = f"{prefix}.hours.{page.sha256}"
        hours = []
        unresolved = []
        for selected in sorted(selections, key=lambda s: (s.venue, s.kind, s.product)):
            product = products[(selected.kind, selected.page_product_name)]
            windows = []
            night = []
            for stage in product.stages:
                if stage.session == "NIGHT":
                    night.append(stage)
                    continue
                if stage.session != "DAY" or stage.day_transition != "SAME_CLOCK_DAY":
                    raise ValueError("unsupported day phase")
                if stage.phase == "AUCTION_ORDER":
                    window_id, phase = "day.auction_order", "auction"
                elif stage.phase == "AUCTION_MATCH":
                    window_id, phase = "day.auction_match", "auction"
                elif stage.phase == "CONTINUOUS":
                    window_id, phase = (
                        f"day.continuous.{stage.start.replace(':', '')}.{stage.end.replace(':', '')}",
                        "continuous",
                    )
                else:
                    raise ValueError("unknown day phase")
                windows.append(
                    {
                        "id": window_id,
                        "phase": phase,
                        "start": stage.start,
                        "end": stage.end,
                        "end_day_offset": 0,
                    }
                )
            contracts = tuple(sorted(selected.contracts))
            hours.append(
                {
                    "venue": selected.venue,
                    "kind": selected.kind,
                    "product": selected.product,
                    "contracts": list(contracts),
                    "valid_from": selected.valid_from.isoformat(),
                    "valid_to": selected.valid_to.isoformat(),
                    "day_windows": sorted(
                        windows, key=lambda row: (row["start"], row["end"], row["id"])
                    ),
                    "night_windows": [],
                    "night_mappings": [],
                    "source_ids": [source_id],
                    "windows_complete": False,
                    "exceptions_complete": False,
                }
            )
            unresolved.append(
                NightReview(
                    selected.venue,
                    selected.kind,
                    selected.page_product_name,
                    selected.product,
                    contracts,
                    tuple(night),
                )
            )
        content = {
            "sources": [{"id": source_id, "description": f"{source_url} sha256:{page.sha256}"}],
            "trading_days": [],
            "trading_hours": hours,
            "exceptions": [],
        }
        return HoursCandidateResult(
            "CANDIDATE", _validate(content), tuple(unresolved), page.raw_notes, page.review_reasons
        )
    except Exception:
        return HoursCandidateResult("INVALID_INPUT", reason="INVALID_OR_AMBIGUOUS_INPUT")
