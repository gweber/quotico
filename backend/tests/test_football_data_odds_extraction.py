"""
backend/tests/test_football_data_odds_extraction.py

Purpose:
    Unit tests for football-data odds extraction helpers with bookmaker
    whitelist, aggregate exclusion, and market parsing rules.

Dependencies:
    - pytest
    - app.services.football_data_service
"""

from datetime import datetime, timezone
import sys

from bson import ObjectId

sys.path.insert(0, "backend")

from app.services import football_data_service as fds


def test_longest_prefix_matching():
    assert fds._match_bookmaker_prefix("PSC>2.5") == "PSC"
    assert fds._match_bookmaker_prefix("PSH") == "PS"
    assert fds._match_bookmaker_prefix("INTERWETTENH") == "INTERWETTEN"
    assert fds._match_bookmaker_prefix("FOOH") is None


def test_aggregate_blacklist_ignored_in_extraction():
    row = {
        "BbAvH": "2.10",
        "BbMx>2.5": "1.90",
        "AvgAHH": "2.00",
        "B365H": "2.10",
        "B365D": "3.30",
        "B365A": "3.80",
    }
    out = fds._extract_odds_snapshots_from_row(
        row=row,
        match_doc={"_id": ObjectId()},
        league_id=ObjectId(),
        sport_key="soccer_epl",
        match_date=datetime(2024, 8, 10, tzinfo=timezone.utc),
    )
    assert "bet365" in out
    assert len(out["bet365"]) == 1


def test_extract_all_markets_h2h_totals_spreads():
    row = {
        "B365H": "2.10",
        "B365D": "3.30",
        "B365A": "3.80",
        "B365>2.5": "1.85",
        "B365<2.5": "1.95",
        "B365>3.5": "2.55",
        "B365<3.5": "1.48",
        "B365AHH": "1.90",
        "B365AHA": "1.98",
        "AHh": "-0.5",
    }
    match_date = datetime(2024, 8, 10, tzinfo=timezone.utc)
    out = fds._extract_odds_snapshots_from_row(
        row=row,
        match_doc={"_id": ObjectId()},
        league_id=ObjectId(),
        sport_key="soccer_epl",
        match_date=match_date,
    )

    snapshots = out["bet365"]
    assert len(snapshots) == 4
    assert sum(1 for s in snapshots if "odds" in s) == 1
    assert sum(1 for s in snapshots if "totals" in s) == 2
    assert sum(1 for s in snapshots if "spreads" in s) == 1
    for snap in snapshots:
        ts = snap["snapshot_at"]
        assert ts.hour == 12
        assert ts.minute == 0
        assert ts.tzinfo == timezone.utc


def test_incomplete_h2h_skipped():
    row = {
        "B365H": "2.10",
        "B365A": "3.80",
    }
    out = fds._extract_odds_snapshots_from_row(
        row=row,
        match_doc={"_id": ObjectId()},
        league_id=ObjectId(),
        sport_key="soccer_epl",
        match_date=datetime(2024, 8, 10, tzinfo=timezone.utc),
    )
    assert "bet365" not in out or all("odds" not in s for s in out["bet365"])


def test_incomplete_totals_line_skipped():
    row = {
        "B365>2.5": "1.85",
        "B365AHH": "1.95",
        "B365AHA": "1.90",
        "AHh": "-0.5",
    }
    out = fds._extract_odds_snapshots_from_row(
        row=row,
        match_doc={"_id": ObjectId()},
        league_id=ObjectId(),
        sport_key="soccer_epl",
        match_date=datetime(2024, 8, 10, tzinfo=timezone.utc),
    )
    assert "bet365" in out
    assert all("totals" not in s for s in out["bet365"])
    assert any("spreads" in s for s in out["bet365"])


def test_spreads_without_line_skipped_with_warning(caplog):
    row = {
        "B365AHH": "1.90",
        "B365AHA": "1.98",
    }
    out = fds._extract_odds_snapshots_from_row(
        row=row,
        match_doc={"_id": ObjectId()},
        league_id=ObjectId(),
        sport_key="soccer_epl",
        match_date=datetime(2024, 8, 10, tzinfo=timezone.utc),
    )
    assert "bet365" not in out or all("spreads" not in s for s in out["bet365"])
    assert any("missing line" in message.lower() for message in caplog.messages)


def test_counting_matches_extraction_provider_counts():
    row = {
        "B365H": "2.10",
        "B365D": "3.30",
        "B365A": "3.80",
        "B365>2.5": "1.85",
        "B365<2.5": "1.95",
        "B365>3.5": "2.55",
        "B365<3.5": "1.48",
        "B365AHH": "1.90",
        "B365AHA": "1.98",
        "AHh": "-0.5",
        "WHH": "2.20",
        "WHD": "3.25",
        "WHA": "3.40",
        "BbAvH": "2.05",
        "AvgAHH": "1.99",
    }
    match_doc = {"_id": ObjectId()}
    league_id = ObjectId()
    match_date = datetime(2024, 8, 10, tzinfo=timezone.utc)

    extracted = fds._extract_odds_snapshots_from_row(
        row=row,
        match_doc=match_doc,
        league_id=league_id,
        sport_key="soccer_epl",
        match_date=match_date,
    )
    extracted_counts = {provider: len(snapshots) for provider, snapshots in extracted.items()}

    counted_counts = dict(fds._count_odds_snapshots_from_row(row=row, match_date=match_date))

    assert set(extracted_counts.keys()) == set(counted_counts.keys())
    assert extracted_counts == counted_counts
    assert sum(extracted_counts.values()) == sum(counted_counts.values())


def test_counting_matches_extraction_with_incomplete_markets():
    row = {
        "B365H": "2.10",
        "B365D": "3.30",
        "B365A": "3.80",
        "B365>2.5": "1.85",   # missing under -> should be skipped
        "B365AHH": "1.90",    # missing line -> spreads skipped
        "B365AHA": "1.98",
    }
    match_doc = {"_id": ObjectId()}
    league_id = ObjectId()
    match_date = datetime(2024, 8, 10, tzinfo=timezone.utc)

    extracted = fds._extract_odds_snapshots_from_row(
        row=row,
        match_doc=match_doc,
        league_id=league_id,
        sport_key="soccer_epl",
        match_date=match_date,
    )
    extracted_counts = {provider: len(snapshots) for provider, snapshots in extracted.items()}
    counted_counts = dict(fds._count_odds_snapshots_from_row(row=row, match_date=match_date))

    assert extracted_counts == {"bet365": 1}
    assert counted_counts == {"bet365": 1}
    assert extracted_counts == counted_counts


def test_counting_matches_extraction_empty_when_no_whitelisted_provider():
    row = {
        "BbAvH": "2.10",
        "BbMx>2.5": "1.95",
        "AvgAHH": "2.05",
        "MaxA": "4.10",
        "FOOH": "2.25",
        "FOOD": "3.20",
        "FOOA": "3.10",
    }
    match_doc = {"_id": ObjectId()}
    league_id = ObjectId()
    match_date = datetime(2024, 8, 10, tzinfo=timezone.utc)

    extracted = fds._extract_odds_snapshots_from_row(
        row=row,
        match_doc=match_doc,
        league_id=league_id,
        sport_key="soccer_epl",
        match_date=match_date,
    )
    counted_counts = dict(fds._count_odds_snapshots_from_row(row=row, match_date=match_date))

    assert extracted == {}
    assert counted_counts == {}
