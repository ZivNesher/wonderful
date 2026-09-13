"""Unit tests for the deterministic scoring formulas -- pure functions, no LLM,
no network. Given known inputs, assert known outputs (skill §17, "unit tests for
the scoring formulas themselves")."""
import scoring


def test_avg_passengers_per_departure_basic():
    assert scoring.avg_passengers_per_departure(1000, 10) == 100.0


def test_avg_passengers_per_departure_zero_departures_is_none():
    assert scoring.avg_passengers_per_departure(1000, 0) is None


def test_capacity_pressure_basic():
    assert scoring.capacity_pressure(1000, 2) == 500.0


def test_capacity_pressure_no_runways_is_none():
    assert scoring.capacity_pressure(1000, None) is None
    assert scoring.capacity_pressure(1000, 0) is None


def test_percentile_rank_middle_of_population():
    population = [10, 20, 30, 40, 50]
    assert scoring.percentile_rank(30, population) == 60.0  # 3 of 5 <= 30


def test_percentile_rank_empty_population_is_zero():
    assert scoring.percentile_rank(30, []) == 0.0


def test_percentile_rank_top_of_population_is_100():
    population = [10, 20, 30]
    assert scoring.percentile_rank(30, population) == 100.0


def test_long_haul_share_classifies_by_threshold():
    # Origin at (0, 0). One destination ~1000mi away (short), one ~6000mi (long).
    destinations = [
        ("NEAR", 0.0, 14.4),   # roughly 1000 mi at the equator
        ("FAR", 0.0, 86.0),    # roughly 5950 mi at the equator
    ]
    result = scoring.long_haul_share(0.0, 0.0, destinations, threshold_miles=2500)
    assert result["status"] == "ok"
    assert result["total_routes"] == 2
    assert result["long_haul_routes"] == 1
    assert result["share"] == 0.5
    assert "FAR" in result["long_haul_destinations"]
    assert "NEAR" not in result["long_haul_destinations"]


def test_long_haul_share_no_routes_is_insufficient_data():
    result = scoring.long_haul_share(0.0, 0.0, [])
    assert result["status"] == "insufficient_data"


def test_composite_expansion_score_weights_sum_correctly():
    # All three inputs at 100 -> composite must be 100 regardless of weights.
    assert scoring.composite_expansion_score(100, 100, 100) == 100.0
    # All three inputs at 0 -> composite must be 0.
    assert scoring.composite_expansion_score(0, 0, 0) == 0.0


def test_composite_expansion_score_known_weighted_value():
    # 0.40*80 + 0.35*40 + 0.25*20 = 32 + 14 + 5 = 51
    assert scoring.composite_expansion_score(80, 40, 20) == 51.0
