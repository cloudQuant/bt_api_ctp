"""Fake-only contracts for the source-only CTP callback ingress module."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "src" / "bt_api_ctp" / "ctp" / "callback_ingress.py"
TRADER_API_PATH = REPO_ROOT / "src" / "bt_api_ctp" / "ctp" / "ctp_trader_api.py"
TRADE_STRUCTS_PATH = REPO_ROOT / "src" / "bt_api_ctp" / "ctp" / "ctp_structs_trade.py"
WRAPPER_CPP_PATH = REPO_ROOT / "src" / "bt_api_ctp" / "ctp" / "ctp_wrap.cpp"
STRUCTS_PATHS = tuple((REPO_ROOT / "src" / "bt_api_ctp" / "ctp").glob("ctp_structs_*.py"))
INGRESS_MODULE_NAME = "callback_ingress_source_only_test_module"

_spec = importlib.util.spec_from_file_location(INGRESS_MODULE_NAME, MODULE_PATH)
assert _spec is not None and _spec.loader is not None
ingress = importlib.util.module_from_spec(_spec)
sys.modules[INGRESS_MODULE_NAME] = ingress
_spec.loader.exec_module(ingress)


def _source_tags(*, source_connection_generation: int = 0):
    return ingress.CtpTraderCallbackSourceTagsV2(
        source_instance_id="source-1",
        native_client_epoch="epoch-1",
        native_api_source_id="api-1",
        native_spi_source_id="spi-1",
        native_api_generation=1,
        connection_generation=source_connection_generation,
    )


def _build(
    callback_name: str,
    args: tuple[object, ...],
    *,
    phase=None,
    sequence: int = 1,
    connection_generation: int = 0,
    kwargs=None,
):
    return ingress.build_callback_ingress_record_v2(
        owner_intent_id="intent-1",
        callback_name=callback_name,
        source_phase=phase or ingress.CtpTraderCallbackPhase.ACTIVE,
        source_tags=_source_tags(),
        connection_generation=connection_generation,
        source_sequence=sequence,
        callback_monotonic_ns=123456,
        args=args,
        kwargs=kwargs,
    )


def _rsp_info(error_id: int = 0):
    return SimpleNamespace(ErrorID=error_id, ErrorMsg="private raw provider error text")


def _login_args(*, broker: str = "broker-1", user: str = "user-1"):
    return (
        SimpleNamespace(
            BrokerID=broker,
            UserID=user,
            TradingDay="20260926",
            FrontID=4,
            SessionID=7,
        ),
        _rsp_info(),
        41,
        True,
    )


def test_generated_spi_inventory_and_every_signature_are_frozen_by_ast():
    raw = TRADER_API_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest().upper() == (
        "26A1CAB3D387C5AFB79FC7C041A7F0E333CAFCA708283B05CE65C2B2EFB08610"
    )
    tree = ast.parse(raw.decode("utf-8"))
    spi = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CThostFtdcTraderSpi"
    )
    signatures = {
        node.name: tuple(argument.arg for argument in node.args.args[1:])
        for node in spi.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("On")
    }
    assert len(signatures) == 155
    assert tuple(signatures) == ingress.EXPECTED_TRADER_SPI_CALLBACK_NAMES
    assert set(ingress.CALLBACK_SPECS) == set(signatures)
    assert {
        name: spec.argument_names for name, spec in ingress.CALLBACK_SPECS.items()
    } == signatures
    assert all(
        type(spec) is ingress.CtpTraderCallbackSpecV2 for spec in ingress.CALLBACK_SPECS.values()
    )
    with pytest.raises(TypeError):
        ingress.CALLBACK_SPECS["OnVendorCallback"] = object()


def test_trade_field_allowlist_is_the_complete_safe_generated_field_set():
    tree = ast.parse(TRADE_STRUCTS_PATH.read_text(encoding="utf-8"))
    field_class = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "CThostFtdcTradeField"
    )
    declared = {
        target.id
        for node in field_class.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id[:1].isupper()
    }
    omitted = {"reserve1", "reserve2"}
    assert set(ingress.SAFE_TRADE_FIELD_NAMES) == declared - omitted
    assert len(ingress.SAFE_TRADE_FIELD_NAMES) == 31


def test_safe_field_types_and_getter_presence_match_generated_swig_sources():
    callback_field_classes = {
        "pRspAuthenticateField": "CThostFtdcRspAuthenticateField",
        "pRspUserLogin": "CThostFtdcRspUserLoginField",
        "pRspInfo": "CThostFtdcRspInfoField",
        "pOrder": "CThostFtdcOrderField",
        "pInputOrder": "CThostFtdcInputOrderField",
        "pInputOrderAction": "CThostFtdcInputOrderActionField",
        "pOrderAction": "CThostFtdcOrderActionField",
        "pTrade": "CThostFtdcTradeField",
        "pInvestorPosition": "CThostFtdcInvestorPositionField",
        "pInstrument": "CThostFtdcInstrumentField",
        "pDepthMarketData": "CThostFtdcDepthMarketDataField",
        "pInstrumentStatus": "CThostFtdcInstrumentStatusField",
    }
    declared_properties = {}
    for source_path in STRUCTS_PATHS:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            declared_properties[node.name] = {
                target.id
                for statement in node.body
                if isinstance(statement, ast.Assign)
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Name)
                and statement.value.func.id == "property"
                for target in statement.targets
                if isinstance(target, ast.Name)
            }

    cpp_lines = WRAPPER_CPP_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    getter_kinds = {}
    getter_pattern = re.compile(
        r"SWIGINTERN PyObject \*_wrap_CThostFtdc([A-Za-z0-9]+Field)_([A-Za-z0-9_]+)_get\("
    )
    conversion_pattern = re.compile(r"resultobj\s*=\s*([A-Za-z0-9_]+)\s*\(")
    conversion_kinds = {
        "SWIG_FromCharPtr": "string",
        "SWIG_From_char": "char",
        "SWIG_From_int": "integer",
        "SWIG_From_short": "integer",
        "SWIG_From_double": "float",
    }
    for index, line in enumerate(cpp_lines):
        match = getter_pattern.match(line)
        if match is None:
            continue
        for body_line in cpp_lines[index + 1 : index + 80]:
            if body_line.strip() == "fail:":
                break
            conversion = conversion_pattern.search(body_line)
            if conversion:
                getter_kinds[(match.group(1), match.group(2))] = conversion_kinds.get(
                    conversion.group(1), "unknown"
                )
                break

    observed_contexts = set()
    for spec in ingress.CALLBACK_SPECS.values():
        for slot, field_names in spec.safe_fields_by_slot:
            argument_name = spec.argument_names[slot]
            class_name = callback_field_classes.get(argument_name)
            assert class_name is not None, (argument_name, field_names)
            struct_suffix = class_name.removeprefix("CThostFtdc")
            for field_name in field_names:
                observed_contexts.add((class_name, field_name))
                # This set is populated only from class-level property(...)
                # declarations in the generated SWIG sources. In particular,
                # OrderSysID is statically declared and a getter
                # AttributeError must not be mistaken for absence.
                assert field_name in declared_properties[class_name], (class_name, field_name)
                assert (
                    getter_kinds[(struct_suffix, field_name)]
                    == ingress.SAFE_FIELD_KINDS[field_name]
                )

    assert observed_contexts
    assert {name for _struct, name in observed_contexts} == set(ingress.SAFE_FIELD_KINDS)

    wrapper_source = WRAPPER_CPP_PATH.read_text(encoding="utf-8", errors="replace")
    for callback_name in ("OnRspOrderInsert", "OnRspError"):
        marker = f"SwigDirector_CThostFtdcTraderSpi::{callback_name}("
        start = wrapper_source.index(marker)
        next_method = wrapper_source.find(
            "\nvoid SwigDirector_CThostFtdcTraderSpi::", start + len(marker)
        )
        body = wrapper_source[start : next_method if next_method >= 0 else start + 5000]
        assert "SWIG_From_int(static_cast< int >(nRequestID))" in body
        assert "SWIG_From_bool(static_cast< bool >(bIsLast))" in body


def test_record_payload_digest_and_registration_vs_callback_generation():
    tags = _source_tags(source_connection_generation=3)
    record = ingress.build_callback_ingress_record_v2(
        owner_intent_id="intent-1",
        callback_name="OnFrontDisconnected",
        source_phase=ingress.CtpTraderCallbackPhase.ACTIVE,
        source_tags=tags,
        connection_generation=4,
        source_sequence=8,
        callback_monotonic_ns=123456,
        args=(17,),
    )
    payload = record.to_payload()
    assert set(payload) == {
        "schema",
        "owner_intent_id",
        "callback_name",
        "callback_class",
        "phase",
        "source_instance_id",
        "native_client_epoch",
        "native_api_source_id",
        "native_spi_source_id",
        "native_api_generation",
        "source_connection_generation",
        "connection_generation",
        "sequence",
        "monotonic_ns",
        "named_args",
        "flattened_fields",
        "capture_complete",
        "missing_getters",
        "digest",
    }
    assert payload["source_connection_generation"] == 3
    assert payload["connection_generation"] == 4
    assert payload["named_args"] == [
        {
            "argument_slot": 0,
            "name": "nReason",
            "present": True,
            "scalar_captured": True,
            "value": 17,
        }
    ]
    payload_without_digest = dict(payload)
    del payload_without_digest["digest"]
    expected = hashlib.sha256(
        json.dumps(
            payload_without_digest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert record.digest == payload["digest"] == expected
    assert record.canonical_bytes().decode("utf-8") == json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def test_flattened_fields_preserve_missing_zero_and_present_none():
    login = SimpleNamespace(
        BrokerID="broker-1",
        UserID="user-1",
        TradingDay="20260926",
        FrontID=0,
        SessionID=7,
        MaxOrderRef=None,
    )
    record = _build(
        "OnRspUserLogin",
        (login, _rsp_info(), 4, True),
        phase=ingress.CtpTraderCallbackPhase.PRE_LOGIN,
    )
    assert record.capture_complete
    zero = record.field_value(0, "FrontID")
    assert zero is not None and zero.present and zero.scalar_captured and zero.value == 0
    explicit_none = record.field_value(0, "MaxOrderRef")
    assert explicit_none is not None and explicit_none.present and explicit_none.scalar_captured
    assert explicit_none.value is None
    absent = record.field_value(0, "InvestUnitID")
    assert absent is None  # the login callback does not allowlist this field

    missing_required = _build(
        "OnRspUserLogin",
        (
            SimpleNamespace(BrokerID="b", UserID="u", TradingDay="20260926", SessionID=7),
            _rsp_info(),
            4,
            True,
        ),
        phase=ingress.CtpTraderCallbackPhase.PRE_LOGIN,
    )
    assert not missing_required.capture_complete
    assert "arg:0.FrontID:missing" in missing_required.missing_field_names


def test_ack_binds_exact_complete_record_and_committed_high_watermark():
    record = _build("OnFrontConnected", ())
    ack = ingress.CtpTraderCallbackIngressAckV2(
        owner_id=record.owner_intent_id,
        sequence=record.sequence,
        digest=record.digest,
        commit_state="COMMITTED",
        high_watermark=record.sequence,
    )
    ack.validate_for(record)
    assert (ack.owner_id, ack.sequence, ack.digest, ack.commit_state, ack.high_watermark) == (
        record.owner_intent_id,
        record.sequence,
        record.digest,
        "COMMITTED",
        record.sequence,
    )
    with pytest.raises(ValueError, match="does not match"):
        ingress.CtpTraderCallbackIngressAckV2(
            "another-intent", record.sequence, record.digest, "COMMITTED", record.sequence
        ).validate_for(record)
    with pytest.raises(ValueError, match="high-watermark"):
        ingress.CtpTraderCallbackIngressAckV2(
            record.owner_intent_id, record.sequence, record.digest, "COMMITTED", record.sequence + 1
        )
    with pytest.raises(ValueError, match="not committed"):
        ingress.CtpTraderCallbackIngressAckV2(
            record.owner_intent_id, record.sequence, record.digest, "PREPARED", record.sequence
        )
    incomplete = _build("OnFrontDisconnected", (b"invalid",))
    with pytest.raises(ValueError, match="incomplete"):
        ingress.CtpTraderCallbackIngressAckV2(
            incomplete.owner_intent_id,
            incomplete.sequence,
            incomplete.digest,
            "COMMITTED",
            incomplete.sequence,
        ).validate_for(incomplete)


def test_sensitive_and_free_text_provider_values_are_never_snapshotted():
    secret = "raw-password-authcode-captcha-CFMMC-token-bank-password"
    password_fields = SimpleNamespace(OldPassword=secret, NewPassword=secret)
    response = SimpleNamespace(ErrorID=0, ErrorMsg=secret)
    record = _build("OnRspUserPasswordUpdate", (password_fields, response, 3, True))
    assert record.capture_complete
    payload = record.canonical_bytes().decode("utf-8")
    assert secret not in payload
    assert "OldPassword" not in payload
    assert "NewPassword" not in payload
    assert "ErrorMsg" not in payload
    assert "ErrorID" in payload
    assert "pUserPasswordUpdate" in payload  # fixed parameter label only, no value

    kwargs_record = _build("OnFrontConnected", (), kwargs={"password": secret})
    assert not kwargs_record.capture_complete
    assert secret not in kwargs_record.canonical_bytes().decode("utf-8")
    assert kwargs_record.missing_field_names == ("kwargs_unsupported",)


def test_bad_scalar_getter_argument_and_size_conditions_are_incomplete():
    bad_scalar = _build("OnFrontDisconnected", (True,))
    assert not bad_scalar.capture_complete
    assert "arg:0:invalid_scalar" in bad_scalar.missing_field_names

    nan_order = SimpleNamespace(
        InstrumentID="i",
        ExchangeID="x",
        OrderRef="r",
        RequestID=1,
        TradingDay="20260926",
        LimitPrice=float("nan"),
    )
    nonfinite = _build("OnRtnOrder", (nan_order,))
    assert not nonfinite.capture_complete
    assert "arg:0.LimitPrice:non_finite" in nonfinite.missing_field_names

    trade_values = {}
    for field_name in ingress.SAFE_TRADE_FIELD_NAMES:
        kind = ingress.SAFE_FIELD_KINDS[field_name]
        trade_values[field_name] = {
            "string": "x" * 1900,
            "char": "x",
            "integer": 1,
            "float": 1.0,
        }[kind]
    oversize = _build("OnRtnTrade", (SimpleNamespace(**trade_values),))
    assert not oversize.capture_complete
    assert "record_size_limit_exceeded" in oversize.missing_field_names
    assert oversize.flattened_fields == ()
    assert len(oversize.canonical_bytes()) <= 32 * 1024


def test_generated_integer_double_and_char_fields_reject_bool_or_wrong_python_type():
    input_order = SimpleNamespace(
        BrokerID="broker-1",
        InvestorID="investor-1",
        UserID="user-1",
        InstrumentID="IF2609",
        ExchangeID="CFFEX",
        OrderRef="order-1",
        RequestID=41,
    )
    for error_id in (False, True):
        record = _build(
            "OnRspOrderInsert",
            (input_order, _rsp_info(error_id), 41, True),
        )
        assert not record.capture_complete
        assert "arg:1.ErrorID:unsafe_type" in record.missing_field_names
        assert (
            ingress.callback_ingress_disposition(record)
            is ingress.CtpTraderCallbackDisposition.POISON
        )

    valid_error_id = _build(
        "OnRspOrderInsert",
        (input_order, _rsp_info(0), 41, True),
    )
    assert valid_error_id.capture_complete
    assert valid_error_id.field_value(1, "ErrorID").value == 0
    assert (
        ingress.callback_ingress_disposition(valid_error_id)
        is ingress.CtpTraderCallbackDisposition.ROUTEABLE
    )
    integer_terminal_flag = _build(
        "OnRspOrderInsert",
        (input_order, _rsp_info(0), 41, 1),
    )
    assert not integer_terminal_flag.capture_complete
    assert "arg:3:invalid_scalar" in integer_terminal_flag.missing_field_names

    order = SimpleNamespace(
        BrokerID="broker-1",
        InvestorID="investor-1",
        UserID="user-1",
        InstrumentID="IF2609",
        ExchangeID="CFFEX",
        OrderRef="order-1",
        RequestID=41,
        TradingDay="20260926",
        LimitPrice=1,
    )
    wrong_double = _build("OnRtnOrder", (order,))
    assert not wrong_double.capture_complete
    assert "arg:0.LimitPrice:unsafe_type" in wrong_double.missing_field_names

    trade = SimpleNamespace(
        BrokerID="broker-1",
        InvestorID="investor-1",
        UserID="user-1",
        InstrumentID="IF2609",
        ExchangeID="CFFEX",
        TradeID="trade-1",
        OrderRef="order-1",
        TradeDate="20260926",
        TradingDay="20260926",
        SequenceNo=1,
        Price=1.0,
        Volume=True,
    )
    wrong_integer = _build("OnRtnTrade", (trade,))
    assert not wrong_integer.capture_complete
    assert "arg:0.Volume:unsafe_type" in wrong_integer.missing_field_names

    wrong_char = _build(
        "OnRtnTrade",
        (
            SimpleNamespace(
                BrokerID="broker-1",
                InvestorID="investor-1",
                UserID="user-1",
                InstrumentID="IF2609",
                ExchangeID="CFFEX",
                TradeID="trade-1",
                OrderRef="order-1",
                TradeDate="20260926",
                TradingDay="20260926",
                SequenceNo=1,
                Price=1.0,
                Volume=1,
                Direction="LONG",
            ),
        ),
    )
    assert not wrong_char.capture_complete
    assert "arg:0.Direction:unsafe_type" in wrong_char.missing_field_names


def test_order_ingress_captures_integer_volume_traded_and_rejects_bool():
    order_values = {
        "BrokerID": "broker-1",
        "InvestorID": "investor-1",
        "UserID": "user-1",
        "InstrumentID": "IF2609",
        "ExchangeID": "CFFEX",
        "OrderRef": "order-1",
        "RequestID": 41,
        "TradingDay": "20260926",
        "VolumeTraded": 0,
    }
    complete = _build("OnRtnOrder", (SimpleNamespace(**order_values),))
    assert complete.capture_complete
    assert complete.field_value(0, "VolumeTraded").value == 0

    order_values["VolumeTraded"] = True
    invalid = _build("OnRtnOrder", (SimpleNamespace(**order_values),))
    assert not invalid.capture_complete
    assert "arg:0.VolumeTraded:unsafe_type" in invalid.missing_field_names


def test_declared_getter_attribute_error_is_incomplete_but_true_absence_is_not():
    class OrderWithBrokenOptionalGetter:
        BrokerID = "broker-1"
        InvestorID = "investor-1"
        UserID = "user-1"
        InstrumentID = "IF2609"
        ExchangeID = "CFFEX"
        OrderRef = "order-1"
        RequestID = 41
        TradingDay = "20260926"

        @property
        def OrderSysID(self):
            raise AttributeError("private provider diagnostic must not escape")

    broken = _build("OnRtnOrder", (OrderWithBrokenOptionalGetter(),))
    assert not broken.capture_complete
    assert "arg:0.OrderSysID:getter_error" in broken.missing_field_names
    state = broken.field_value(0, "OrderSysID")
    assert state is not None and state.present and not state.scalar_captured
    assert "private provider diagnostic" not in broken.canonical_bytes().decode("utf-8")

    truly_absent = _build(
        "OnRtnOrder",
        (
            SimpleNamespace(
                BrokerID="broker-1",
                InvestorID="investor-1",
                UserID="user-1",
                InstrumentID="IF2609",
                ExchangeID="CFFEX",
                OrderRef="order-1",
                RequestID=41,
                TradingDay="20260926",
            ),
        ),
    )
    assert truly_absent.capture_complete
    state = truly_absent.field_value(0, "OrderSysID")
    assert state is not None and not state.present and not state.scalar_captured
    assert state.value is None


def test_null_required_fields_and_scalars_return_incomplete_records_without_raising():
    base_input_order = {
        "BrokerID": "broker-1",
        "InvestorID": "investor-1",
        "UserID": "user-1",
        "InstrumentID": "IF2609",
        "ExchangeID": "CFFEX",
        "OrderRef": "order-1",
        "RequestID": 41,
    }
    for field_name, reason in (
        ("BrokerID", "null_required"),
        ("InvestorID", "null_required"),
        ("UserID", "null_required"),
        ("InstrumentID", "null_required"),
        ("ExchangeID", "null_required"),
        ("OrderRef", "null_required"),
        ("RequestID", "unsafe_type"),
    ):
        order_fields = dict(base_input_order)
        order_fields[field_name] = None
        record = _build(
            "OnRspOrderInsert",
            (SimpleNamespace(**order_fields), _rsp_info(0), 41, True),
        )
        assert not record.capture_complete
        assert f"arg:0.{field_name}:{reason}" in record.missing_field_names
        state = record.field_value(0, field_name)
        assert state is not None and state.present
        if reason == "null_required":
            assert state.scalar_captured and state.value is None
        else:
            assert not state.scalar_captured and state.value is None

    null_error_id = _build(
        "OnRspOrderInsert",
        (SimpleNamespace(**base_input_order), _rsp_info(None), 41, True),
    )
    assert not null_error_id.capture_complete
    assert "arg:1.ErrorID:unsafe_type" in null_error_id.missing_field_names

    null_request_id = _build(
        "OnRspOrderInsert",
        (SimpleNamespace(**base_input_order), _rsp_info(0), None, True),
    )
    assert not null_request_id.capture_complete
    assert "arg:2:invalid_scalar" in null_request_id.missing_field_names

    null_price = _build(
        "OnRtnTrade",
        (
            SimpleNamespace(
                BrokerID="broker-1",
                InvestorID="investor-1",
                UserID="user-1",
                InstrumentID="IF2609",
                ExchangeID="CFFEX",
                TradeID="trade-1",
                OrderRef="order-1",
                TradeDate="20260926",
                TradingDay="20260926",
                SequenceNo=1,
                Price=None,
                Volume=1,
            ),
        ),
    )
    assert not null_price.capture_complete
    assert "arg:0.Price:unsafe_type" in null_price.missing_field_names


def test_disposition_uses_query_request_id_slots_and_lifecycle_policy():
    auth = _build(
        "OnRspAuthenticate",
        (SimpleNamespace(BrokerID="broker-1", UserID="user-1"), _rsp_info(), 41, True),
        phase=ingress.CtpTraderCallbackPhase.PRE_LOGIN,
    )
    assert (
        ingress.callback_ingress_disposition(auth)
        is ingress.CtpTraderCallbackDisposition.PRE_LOGIN_CANDIDATE
    )
    assert ingress.validate_expected_prelogin_step(
        auth,
        expected_callback_name="OnRspAuthenticate",
        expected_request_id=41,
        bound_broker_id="broker-1",
        bound_user_id="user-1",
    )
    assert not ingress.validate_expected_prelogin_step(
        auth,
        expected_callback_name="OnRspAuthenticate",
        expected_request_id=41,
    )
    assert not ingress.validate_expected_prelogin_step(
        auth,
        expected_callback_name="OnRspAuthenticate",
        expected_request_id=42,
    )

    login = _build(
        "OnRspUserLogin",
        _login_args(),
        phase=ingress.CtpTraderCallbackPhase.PRE_LOGIN,
    )
    assert ingress.validate_expected_prelogin_step(
        login,
        expected_callback_name="OnRspUserLogin",
        expected_request_id=41,
        bound_broker_id="broker-1",
        bound_user_id="user-1",
    )
    assert not ingress.validate_expected_prelogin_step(
        login,
        expected_callback_name="OnRspUserLogin",
        expected_request_id=41,
    )

    failed_login = _build(
        "OnRspUserLogin",
        (_login_args()[0], _rsp_info(7), 41, True),
        phase=ingress.CtpTraderCallbackPhase.PRE_LOGIN,
    )
    assert (
        ingress.callback_ingress_disposition(failed_login)
        is ingress.CtpTraderCallbackDisposition.POISON
    )
    assert not ingress.validate_expected_prelogin_step(
        failed_login,
        expected_callback_name="OnRspUserLogin",
        expected_request_id=41,
    )

    query = _build("OnRspQryMaxOrderVolume", (None, _rsp_info(), 77, True))
    assert ingress.CALLBACK_SPECS["OnRspQryOrder"].argument_names[2] == "nRequestID"
    assert (
        ingress.callback_ingress_disposition(
            query, active_query_request_id=77, active_query_kind="OnRspQryMaxOrderVolume"
        )
        is ingress.CtpTraderCallbackDisposition.AUDIT_QUERY
    )
    assert (
        ingress.callback_ingress_disposition(
            query, active_query_request_id=78, active_query_kind="OnRspQryMaxOrderVolume"
        )
        is ingress.CtpTraderCallbackDisposition.POISON
    )

    query_error = _build("OnRspError", (_rsp_info(), 77, True))
    assert ingress.CALLBACK_SPECS["OnRspError"].argument_names[1] == "nRequestID"
    assert (
        ingress.callback_ingress_disposition(
            query_error, active_query_request_id=77, active_query_kind="OnRspQryOrder"
        )
        is ingress.CtpTraderCallbackDisposition.AUDIT_QUERY
    )
    assert (
        ingress.callback_ingress_disposition(
            query_error, active_query_request_id=77, active_query_kind="unpersisted-query"
        )
        is ingress.CtpTraderCallbackDisposition.POISON
    )

    lifecycle = _build("OnFrontDisconnected", (1,))
    assert (
        ingress.callback_ingress_disposition(lifecycle)
        is ingress.CtpTraderCallbackDisposition.POISON
    )
    unsupported = _build("OnRtnQuote", (SimpleNamespace(QuoteID="q", BidPrice=5.0),))
    assert (
        ingress.callback_ingress_disposition(unsupported)
        is ingress.CtpTraderCallbackDisposition.POISON
    )


def test_dynamic_wrapper_overrides_inherited_methods_and_dispatches_all_155():
    original_calls = []
    dispatcher_calls = []

    def make_original(name):
        def original(self, *args, **kwargs):
            original_calls.append(name)
            return name

        original.__name__ = name
        return original

    base = type(
        "FakeInheritedTraderSpi",
        (),
        {name: make_original(name) for name in ingress.EXPECTED_TRADER_SPI_CALLBACK_NAMES},
    )
    child = type("FakeTraderSpi", (base,), {})

    def dispatcher(self, name, args, kwargs, original):
        dispatcher_calls.append((name, args, kwargs, original.__name__))
        return None

    result = ingress.install_trader_spi_callback_dispatch(child, dispatcher)
    assert result is child
    assert child is not base
    assert set(ingress.EXPECTED_TRADER_SPI_CALLBACK_NAMES) <= set(child.__dict__)
    assert ingress.install_trader_spi_callback_dispatch(child, dispatcher) is child
    instance = child()
    for name in ingress.EXPECTED_TRADER_SPI_CALLBACK_NAMES:
        assert getattr(instance, name)() == name
    assert [name for name, *_ in dispatcher_calls] == list(
        ingress.EXPECTED_TRADER_SPI_CALLBACK_NAMES
    )
    assert original_calls == list(ingress.EXPECTED_TRADER_SPI_CALLBACK_NAMES)
    assert all(name == original_name for name, _args, _kwargs, original_name in dispatcher_calls)
    assert "OnFrontConnected" in child.__dict__
    with pytest.raises(RuntimeError, match="already installed"):
        ingress.install_trader_spi_callback_dispatch(child, lambda *args: None)


def test_dynamic_wrapper_exception_suppresses_original_and_sentinel_can_skip():
    original_calls = []

    def original(self, *args, **kwargs):
        original_calls.append("called")

    fake = type(
        "FakeSpi", (), {name: original for name in ingress.EXPECTED_TRADER_SPI_CALLBACK_NAMES}
    )

    def failing_dispatcher(*args):
        raise RuntimeError("commit acknowledgement unknown")

    ingress.install_trader_spi_callback_dispatch(fake, failing_dispatcher)
    with pytest.raises(RuntimeError, match="acknowledgement unknown"):
        fake().OnFrontConnected()
    assert original_calls == []

    fake_skip = type(
        "FakeSkipSpi", (), {name: original for name in ingress.EXPECTED_TRADER_SPI_CALLBACK_NAMES}
    )
    ingress.install_trader_spi_callback_dispatch(
        fake_skip, lambda *args: ingress.SKIP_ORIGINAL_CALLBACK
    )
    assert fake_skip().OnFrontConnected() is None
    assert original_calls == []


def test_wrapper_rejects_missing_or_unreviewed_inventory_methods():
    names = ingress.EXPECTED_TRADER_SPI_CALLBACK_NAMES
    missing = type("MissingFakeSpi", (), {name: lambda self: None for name in names[:-1]})
    with pytest.raises(RuntimeError, match="inventory mismatch"):
        ingress.install_trader_spi_callback_dispatch(missing, lambda *args: None)

    unknown = type(
        "UnknownFakeSpi",
        (),
        {**{name: lambda self: None for name in names}, "OnVendorAdded": lambda self: None},
    )
    with pytest.raises(RuntimeError, match="inventory mismatch"):
        ingress.install_trader_spi_callback_dispatch(unknown, lambda *args: None)
