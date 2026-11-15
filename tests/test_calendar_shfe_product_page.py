"""Offline tests use synthetic HTML, never a network or account."""

import hashlib
import json

import pytest

from bt_api_ctp.calendar_shfe_product_page import MAX_BYTES, parse_shfe_product_page

FUTURE = "https://www.shfe.com.cn/products/futures/metal/nonferrousmetal/cu_f/"
OPTION = "https://www.shfe.com.cn/products/option/nonferrousmetal/cu_o/"
INDEX_FUTURE = "https://www.shfe.com.cn/products/futures/index_f/ec_f/"


def page(rows, kind="future"):
    name = "pageList" if kind == "future" else "pageList_o"
    content = json.dumps(rows, ensure_ascii=False).replace("}, {", "},\u00a0{")
    return f"<html><script>let {name} = {content};</script></html>".encode()


def record(code="CU", venue="上海期货交易所", **kwargs):
    return {
        "ContractSymbol": code,
        "ListingExchange": venue,
        "ListingExchange1": "",
        "data_standard": "2023-03-29",
        "TradingHours": "合成时间及其他时间",
        **kwargs,
    }


def test_future_retains_raw_fields_and_distinguishes_date_from_validity():
    html = page([record(note='原样保留 [ ] , 和 \\" 字符')])
    result = parse_shfe_product_page(source_url=FUTURE, html=html)
    assert result.status == "CANDIDATE" and result.sha256 == hashlib.sha256(html).hexdigest()
    assert result.verification == "UNVERIFIED" and result.source_authentication == "NOT_PROVEN"
    assert result.coverage == result.current_record == "UNKNOWN"
    row = result.records[0]
    assert (row.kind, row.venue, row.symbol, row.raw_date) == ("future", "SHFE", "CU", "2023-03-29")
    assert row.effective_date == "UNKNOWN"
    assert dict(row.fields)["note"] == '原样保留 [ ] , 和 \\" 字符'
    assert dict(row.fields)["TradingHours"] == "合成时间及其他时间"


def test_explicit_index_future_path_is_supported_without_venue_inference():
    html = page([record("EC", venue="", ListingExchange1="上海国际能源交易中心")])
    result = parse_shfe_product_page(source_url=INDEX_FUTURE, html=html)
    assert result.status == "CANDIDATE"
    assert result.records[0].symbol == "EC" and result.records[0].venue == "INE"
    assert result.source_authentication == "NOT_PROVEN"
    assert result.records[0].effective_date == "UNKNOWN"


def test_option_venue_is_read_from_record_and_history_is_preserved():
    option_code = "看涨期权：BC-合约月份-C-行权价格； 看跌期权：BC-合约月份-P-行权价格"
    rows = [
        record(option_code, venue="", ListingExchange1="上海国际能源交易中心"),
        record(
            option_code,
            venue="",
            ListingExchange1="上海国际能源交易中心",
            data_standard="2020-01-01",
        ),
    ]
    result = parse_shfe_product_page(source_url=OPTION, html=page(rows, "option"))
    assert result.status == "CANDIDATE"
    assert [(r.venue, r.symbol, r.raw_date) for r in result.records] == [
        ("INE", "BC", "2023-03-29"),
        ("INE", "BC", "2020-01-01"),
    ]
    assert all(row.effective_date == "UNKNOWN" for row in result.records)


def test_unknown_or_conflicting_fields_are_not_inferred_from_url_or_name():
    option_code = "看涨期权：CU-合约月份-C-行权价格；看跌期权：BC-合约月份-P-行权价格"
    rows = [record(option_code, ListingExchange1="上海国际能源交易中心", data_standard="")]
    result = parse_shfe_product_page(source_url=OPTION, html=page(rows, "option"))
    assert result.status == "NEEDS_REVIEW"
    assert result.records[0].symbol is result.records[0].venue is None
    assert result.records[0].unknown_reasons == (
        "SYMBOL_UNKNOWN",
        "VENUE_UNKNOWN",
        "SOURCE_DATE_UNKNOWN",
    )
    assert dict(result.records[0].fields)["ContractSymbol"] == option_code
    assert result.review_reasons == ("RECORD_UNRESOLVED",)


def test_conflicting_history_preserves_records_and_exposes_each_conflict():
    rows = [
        record("CU"),
        record("BC", venue="", ListingExchange1="上海国际能源交易中心"),
        record("unrecognized"),
    ]
    result = parse_shfe_product_page(source_url=FUTURE, html=page(rows))
    assert result.status == "NEEDS_REVIEW"
    assert result.review_reasons == ("RECORD_UNRESOLVED", "SYMBOL_CONFLICT", "VENUE_CONFLICT")
    assert [(r.symbol, r.venue) for r in result.records] == [
        ("CU", "SHFE"),
        ("BC", "INE"),
        (None, "SHFE"),
    ]
    assert [dict(row.fields) for row in result.records] == rows


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([record("CU"), record("BC")], "SYMBOL_CONFLICT"),
        (
            [record("CU"), record("CU", venue="", ListingExchange1="上海国际能源交易中心")],
            "VENUE_CONFLICT",
        ),
    ],
)
def test_one_cross_record_conflict_has_specific_reason(rows, reason):
    result = parse_shfe_product_page(source_url=FUTURE, html=page(rows))
    assert result.status == "NEEDS_REVIEW"
    assert result.review_reasons == (reason,)
    assert len(result.records) == 2


def test_trailing_commas_and_nbsp_are_accepted_outside_strings_only():
    html = '<script> let pageList = [\u00a0{"ContractSymbol":"CU", "ListingExchange":"上海期货交易所", "data_standard":"x,]\\"",},]; </script>'.encode()
    result = parse_shfe_product_page(source_url=FUTURE, html=html)
    assert result.status == "CANDIDATE"
    assert result.records[0].raw_date == 'x,]"'


@pytest.mark.parametrize(
    "script",
    [
        'let pageList = [{"ContractSymbol":"CU", "ContractSymbol":"BC"}];',
        'let pageList = [{"ContractSymbol": alert(1)}];',
        'let pageList = [{"ContractSymbol":"CU"}]; alert(1)',
        'const foo=1; let pageList = [{"ContractSymbol":"CU"}];',
        'let pageList = [{"ContractSymbol":"CU"}]; let pageList = [];',
        'let pageList = [{ContractSymbol:"CU"}];',
        'let pageList = [{"ContractSymbol":NaN}];',
        'let pageList = [{"ContractSymbol":"CU",/*comment*/}];',
        'let pageList_o = [{"ContractSymbol":"CU"}];',
        'let pageList = [{"ContractSymbol":"CU",},,];',
        'let pageList = [{"ContractSymbol":["CU"]}];',
        'let pageList = [{"ContractSymbol":"\\ud800"}];',
        "let pageList = [];",
    ],
)
def test_rejects_js_execution_duplicate_keys_or_changed_schema(script):
    result = parse_shfe_product_page(source_url=FUTURE, html=f"<script>{script}</script>".encode())
    assert result.status == "STRUCTURE_CHANGED" and result.records == ()


def test_duplicate_declarations_in_separate_scripts_or_external_script_are_rejected():
    source = page([record()])
    double = source.replace(b"</html>", b"<script>let pageList = [];</script></html>")
    assert parse_shfe_product_page(source_url=FUTURE, html=double).status == "STRUCTURE_CHANGED"
    external = source.replace(b"<script>", b'<script src="file.js">')
    assert parse_shfe_product_page(source_url=FUTURE, html=external).status == "STRUCTURE_CHANGED"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.shfe.com.cn/products/futures/metal/cu_f/",
        "https://www.shfe.com.cn.evil.example/products/futures/metal/cu_f/",
        "https://www.shfe.com.cn:443/products/futures/metal/cu_f/",
        "https://user@www.shfe.com.cn/products/futures/metal/cu_f/",
        FUTURE + "?x=1",
        FUTURE + "#x",
        "https://www.shfe.com.cn/products/futures/../cu_f/",
        "https://www.shfe.com.cn/products/futures/metal/cu_o/",
        "https://www.shfe.com.cn/products/option/index_f/ec_o/",
        "https://www.shfe.com.cn/products/futures/metal/index_f/ec_f/",
        "https://www.shfe.com.cn/products/futures/index_f/metal/ec_f/",
        "https://www.shfe.com.cn/products/futures/index_f/../ec_f/",
        "https://www.shfe.com.cn/products/futures/index_f/%65c_f/",
        "https://www.shfe.com.cn/products/futures/index_f//ec_f/",
        "https://www.shfe.com.cn/products/futures/index_f/ec_f/?query=1",
    ],
)
def test_source_url_is_exactly_bounded(url):
    assert parse_shfe_product_page(source_url=url, html=page([record()])).status == "INVALID_SOURCE"


def test_invalid_input_and_resource_limits():
    assert parse_shfe_product_page(source_url=FUTURE, html=None).status == "INVALID_INPUT"
    assert (
        parse_shfe_product_page(source_url=FUTURE, html=b"a" * (MAX_BYTES + 1)).status
        == "INPUT_TOO_LARGE"
    )
    assert parse_shfe_product_page(source_url=FUTURE, html=b"\xff").status == "INVALID_UTF8"
    assert (
        parse_shfe_product_page(source_url=FUTURE, html=page([record()] * 101)).status
        == "STRUCTURE_CHANGED"
    )
