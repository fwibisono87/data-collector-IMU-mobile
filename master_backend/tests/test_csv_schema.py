from master_backend.app.csv_schema import (
    CSV_HEADER_V1,
    CSV_HEADER_V2,
    is_header_line,
    parse_row,
    schema_version_of,
    metadata_line,
    parse_metadata_line,
)


def test_is_header_line_true_for_both_versions():
    assert is_header_line(CSV_HEADER_V1)
    assert is_header_line(CSV_HEADER_V2)


def test_is_header_line_false_for_data_row_and_comment():
    assert not is_header_line("1,2,3,4,5,6,7,8,9,10,11")
    assert not is_header_line("# session_id=123")


def test_parse_row_v1_pads_to_14():
    fields = parse_row("1,2,3,4,5,6,7,8,9,10,11")
    assert len(fields) == 14
    assert fields[11] == ""
    assert fields[12] == ""
    assert fields[13] == ""


def test_parse_row_v2_unchanged():
    fields = parse_row("1,2,3,4,5,6,7,8,9,10,11,12,13,14")
    assert len(fields) == 14
    assert fields[13] == "14"


def test_parse_row_header_v1_regression_is_none():
    assert parse_row(CSV_HEADER_V1.rstrip("\n")) is None
    assert parse_row(CSV_HEADER_V2.rstrip("\n")) is None


def test_parse_row_none_cases():
    assert parse_row("") is None
    assert parse_row("   ") is None
    assert parse_row("# session_id=123") is None
    assert parse_row("1,2,3,4,5") is None


def test_schema_version():
    assert schema_version_of([""] * 14) == 1
    row14 = [""] * 14
    row14[13] = "1"
    assert schema_version_of(row14) == 2
    assert schema_version_of([""] * 11) == 1


def test_metadata_line():
    line = metadata_line(session_id="1785921100261", subject="rabil",
                         role="chest", nominal_hz=100.0)
    assert line == "# session_id=1785921100261,schema_version=2,subject=rabil,role=chest,nominal_hz=100.0"


def test_metadata_line_sanitizes():
    line = metadata_line(session_id="a,b", subject="x\ny")
    assert "a_b" in line
    assert "x_y" in line
    assert "\n" not in line


def test_parse_metadata_line():
    line = "# session_id=1785921100261,schema_version=2,subject=rabil,role=chest,nominal_hz=100.0"
    parsed = parse_metadata_line(line)
    assert parsed["session_id"] == "1785921100261"
    assert parsed["subject"] == "rabil"
    assert parsed["nominal_hz"] == "100.0"
    assert parse_metadata_line("not a comment") == {}
