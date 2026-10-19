"""
The data-quality checks in qa/data_quality.py, run against planted defects.

The seed carries a known number of each defect (see the header of seed.sql). A check
that reports the wrong count here would report the wrong count on the real archive too,
so this is what makes qa/data_quality_report.md trustworthy.
"""

import pytest

from conftest import SEED
from qa.data_quality import CHECKS, HANDLING, gap_list, run_checks

EXPECTED = {"bus": SEED.DEFECTS_BUS, "aircraft": SEED.DEFECTS_AIRCRAFT}


@pytest.fixture(scope="module")
def results(seeded_db):
    import psycopg2
    conn = psycopg2.connect(seeded_db)
    conn.set_session(readonly=True)
    try:
        yield run_checks(conn)
    finally:
        conn.close()


@pytest.mark.parametrize("layer", ["bus", "aircraft"])
@pytest.mark.parametrize("check", list(CHECKS))
def test_check_finds_exactly_the_planted_defects(results, layer, check):
    assert results[layer][check] == EXPECTED[layer][check]


def test_every_check_has_expectations_and_a_handling_note():
    assert set(CHECKS) == set(SEED.DEFECTS_BUS) == set(SEED.DEFECTS_AIRCRAFT) == set(HANDLING)


def test_teleport_threshold_is_what_flags_the_jump(db):
    # B8 jumps ~39 km in 20 s (~7,000 km/h). Raising the bus limit past that clears it.
    import qa.data_quality as dq
    original = dict(dq.MAX_KMH)
    try:
        dq.MAX_KMH["bus"] = 10_000
        assert run_checks(db, layers=("bus",), checks=("teleports",))["bus"]["teleports"] == 0
    finally:
        dq.MAX_KMH.clear()
        dq.MAX_KMH.update(original)


def test_gap_list_names_the_overnight_hole(db):
    with db.cursor() as cur:
        cur.execute("SET TIME ZONE 'America/Montreal'")
    gaps = gap_list(db, "bus")
    assert len(gaps) == 1
    prev, nxt, missing = gaps[0]
    assert (prev.hour, nxt.hour, float(missing)) == (8, 23, 14)


def test_checks_refuse_to_write(db):
    """The audit runs on read-only sessions; prove the session really is read-only."""
    import psycopg2
    with db.cursor() as cur, pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
        cur.execute("DELETE FROM vehicle_positions")


def test_report_renders_every_section(db, tmp_path):
    from qa.data_quality import write_report
    out = tmp_path / "report.md"
    write_report(db, out, progress=lambda *_: None)
    text = out.read_text()
    for heading in ("## Archive", "## Checks", "## Collection gaps", "## Time-window query plan"):
        assert heading in text
    assert "| bus | 118 |" in text
    assert "idx_vp_fetched_at" in text
