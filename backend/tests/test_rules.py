"""Pure unit tests for eligibility and fees. No database involved."""
from decimal import Decimal

import pytest

from app.rules import (
    Eligibility,
    EligibilityInput,
    FeeLine,
    Unit,
    calculate_visit_fees,
    evaluate_eligibility,
    fee_credit,
    request_from_payload,
)

E = Eligibility


DEFAULT_UNITS = (Unit("WASHER"),)


def make(units=DEFAULT_UNITS, location="RESIDENTIAL", district="North Quay", **kw):
    return EligibilityInput(location_type=location, units=tuple(units), district=district, **kw)


def result(**kw):
    return evaluate_eligibility(make(**kw)).result


# --- eligibility -------------------------------------------------------------------------

def test_plain_residential_washer_in_primary_area_is_eligible():
    r = evaluate_eligibility(make())
    assert r.result == E.ELIGIBLE
    assert r.findings == ()


@pytest.mark.parametrize("hazard", ["GAS_SMELL", "SMOKE", "ARCING", "WATER_NEAR_LIVE_PARTS"])
def test_each_hazard_is_an_emergency_stop(hazard):
    r = evaluate_eligibility(make(hazards=frozenset({hazard})))
    assert r.result == E.EMERGENCY_STOP
    assert f"HAZARD_{hazard}" in r.codes


def test_hazard_beats_everything_even_with_clearance_on_record():
    r = evaluate_eligibility(make(
        units=(Unit("RANGE", power_source="GAS"),),
        hazards=frozenset({"GAS_SMELL"}),
        requested_work=frozenset({"GAS_LINE"}),
        clearance_on_record=True,
    ))
    assert r.result == E.EMERGENCY_STOP


@pytest.mark.parametrize("work", ["BUILDING_WIRING", "PLUMBING", "GAS_LINE"])
def test_forbidden_work_is_declined(work):
    assert result(requested_work=frozenset({work})) == E.DECLINE


def test_cheap_countertop_items_are_declined():
    assert result(units=(Unit("TOASTER"),)) == E.DECLINE


@pytest.mark.parametrize("source", ["GAS", "HARDWIRED"])
def test_gas_and_hardwired_are_held_without_clearance(source):
    r = evaluate_eligibility(make(units=(Unit("OVEN", power_source=source),)))
    assert r.result == E.LICENSE_HOLD
    assert r.codes == ["UNIT_1_LICENSE_REQUIRED"]


@pytest.mark.parametrize("source", ["GAS", "HARDWIRED"])
def test_recorded_clearance_releases_the_license_hold(source):
    units = (Unit("OVEN", power_source=source),)
    assert result(units=units, clearance_on_record=True) == E.ELIGIBLE


def test_commercial_location_goes_to_owner_review():
    assert result(location="COMMERCIAL") == E.OWNER_REVIEW


def test_commercial_gas_is_held_not_just_reviewed():
    r = evaluate_eligibility(make(location="COMMERCIAL", units=(Unit("OVEN", power_source="GAS"),)))
    assert r.result == E.LICENSE_HOLD
    assert set(r.codes) == {"COMMERCIAL_LOCATION", "UNIT_1_LICENSE_REQUIRED"}


def test_unknown_appliance_goes_to_owner_review():
    assert result(units=(Unit("WINE_COOLER"),)) == E.OWNER_REVIEW


def test_customer_supplied_part_is_conditional():
    assert result(units=(Unit("DISHWASHER", customer_supplied_part=True),)) == E.CONDITIONAL


def test_no_units_goes_to_owner_review():
    r = evaluate_eligibility(make(units=()))
    assert r.result == E.OWNER_REVIEW
    assert r.codes == ["NO_UNITS"]


@pytest.mark.parametrize("district,expected", [
    ("North Quay", E.ELIGIBLE),
    ("  old mill ", E.ELIGIBLE),
    ("Eastbrook", E.CONDITIONAL),
    ("Faraway", E.OWNER_REVIEW),
])
def test_territory_classification(district, expected):
    assert result(district=district) == expected


def test_missing_district_is_not_judged():
    assert result(district=None) == E.ELIGIBLE


def test_worst_unit_wins_and_findings_are_sorted_by_severity():
    r = evaluate_eligibility(make(units=(
        Unit("WASHER", customer_supplied_part=True),   # CONDITIONAL
        Unit("TOASTER"),                               # DECLINE
        Unit("RANGE", power_source="GAS"),             # LICENSE_HOLD
    )))
    assert r.result == E.DECLINE
    assert [f.result for f in r.findings] == [E.DECLINE, E.LICENSE_HOLD, E.CONDITIONAL]


def test_precedence_follows_the_severity_ladder():
    ladder = [
        (make(units=(Unit("WASHER", customer_supplied_part=True),)), E.CONDITIONAL),
        (make(location="COMMERCIAL"), E.OWNER_REVIEW),
        (make(units=(Unit("OVEN", power_source="GAS"),)), E.LICENSE_HOLD),
        (make(requested_work=frozenset({"PLUMBING"})), E.DECLINE),
        (make(hazards=frozenset({"SMOKE"})), E.EMERGENCY_STOP),
    ]
    for request, expected in ladder:
        assert evaluate_eligibility(request).result == expected


def test_evaluation_is_deterministic():
    request = make(location="COMMERCIAL", units=(Unit("OVEN", power_source="GAS"),))
    assert evaluate_eligibility(request) == evaluate_eligibility(request)


def test_payload_mapping_is_tolerant_of_malformed_input():
    request = request_from_payload({
        "location_type": "residential",
        "units": [{"appliance_type": "washer"}, "garbage", {"appliance_type": 5}],
        "hazards": ["smoke", 3, None],
        "requested_work": "not-a-list",
        "district": 42,
    })
    assert request.location_type == "RESIDENTIAL"
    assert request.units[0] == Unit("WASHER")
    assert request.hazards == frozenset({"SMOKE"})
    assert request.requested_work == frozenset()
    assert request.district is None


def test_customer_cannot_inject_a_clearance_through_the_payload():
    request = request_from_payload({
        "location_type": "RESIDENTIAL",
        "units": [{"appliance_type": "OVEN", "power_source": "GAS"}],
        "clearance_on_record": True,
    })
    assert evaluate_eligibility(request).result == E.LICENSE_HOLD


# --- fees --------------------------------------------------------------------------------

def test_residential_fees_step_down_per_unit():
    quote = calculate_visit_fees("RESIDENTIAL", 3)
    amounts = [ln.amount for ln in quote.lines]
    assert amounts == [Decimal("45.00"), Decimal("35.00"), Decimal("25.00")]
    assert quote.fixed_total == Decimal("105.00")
    assert not quote.needs_quote


def test_fourth_residential_unit_requires_a_quote():
    quote = calculate_visit_fees("RESIDENTIAL", 4)
    assert quote.lines[3].status == "QUOTE_REQUIRED"
    assert quote.lines[3].amount is None
    assert quote.fixed_total == Decimal("105.00")  # the quoted unit adds nothing automatically
    assert quote.needs_quote


def test_commercial_units_never_get_an_automatic_amount():
    quote = calculate_visit_fees("COMMERCIAL", 2)
    assert all(ln.status == "QUOTE_REQUIRED" and ln.amount is None for ln in quote.lines)
    assert all(ln.minimum == Decimal("90.00") for ln in quote.lines)
    assert quote.fixed_total == Decimal("0.00")


def test_zero_units_gives_an_empty_quote_and_negative_is_rejected():
    assert calculate_visit_fees("RESIDENTIAL", 0).lines == ()
    with pytest.raises(ValueError):
        calculate_visit_fees("RESIDENTIAL", -1)


def test_money_is_exact_decimal_not_float():
    total = calculate_visit_fees("RESIDENTIAL", 3).fixed_total
    assert isinstance(total, Decimal)
    assert str(total) == "105.00"


def test_fee_is_credited_only_to_the_same_unit_after_approved_and_completed_repair():
    line = FeeLine(2, "FIXED", amount=Decimal("35.00"))
    assert fee_credit(line, 2, repair_approved=True, repair_completed=True) == Decimal("35.00")
    assert fee_credit(line, 1, repair_approved=True, repair_completed=True) == Decimal("0.00")
    assert fee_credit(line, 2, repair_approved=False, repair_completed=True) == Decimal("0.00")
    assert fee_credit(line, 2, repair_approved=True, repair_completed=False) == Decimal("0.00")


def test_a_quoted_line_has_no_automatic_credit():
    line = FeeLine(4, "QUOTE_REQUIRED")
    assert fee_credit(line, 4, repair_approved=True, repair_completed=True) == Decimal("0.00")
