"""Unit test for the trailing-12-month date arithmetic used to window the live
BTS query -- pure function, no network, easy to get subtly wrong on year rollover."""
import retrieval


def test_months_before_same_year():
    assert retrieval._months_before("2026-04-01", 11) == "2025-05-01"


def test_months_before_crosses_year_boundary():
    assert retrieval._months_before("2026-01-15", 11) == "2025-02-01"


def test_months_before_zero_is_same_month():
    assert retrieval._months_before("2026-04-30", 0) == "2026-04-01"
