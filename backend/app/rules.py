"""Business rules as pure functions: eligibility and visit fees.

No database, no I/O, no randomness: the same input always gives the same output,
so every rule is covered by plain unit tests (docs/SPEC.md, section 6.1).
The result is advisory input for a human: nothing here sends, declines or books.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Eligibility(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    CONDITIONAL = "CONDITIONAL"
    OWNER_REVIEW = "OWNER_REVIEW"
    LICENSE_HOLD = "LICENSE_HOLD"
    DECLINE = "DECLINE"
    EMERGENCY_STOP = "EMERGENCY_STOP"


# A stricter result always wins; a looser rule can never override it.
SEVERITY: dict[Eligibility, int] = {
    Eligibility.ELIGIBLE: 0,
    Eligibility.CONDITIONAL: 1,
    Eligibility.OWNER_REVIEW: 2,
    Eligibility.LICENSE_HOLD: 3,
    Eligibility.DECLINE: 4,
    Eligibility.EMERGENCY_STOP: 5,
}

SUPPORTED_APPLIANCES = frozenset({
    "WASHER", "DRYER", "DISHWASHER", "REFRIGERATOR", "FREEZER",
    "OVEN", "RANGE", "COOKTOP", "MICROWAVE",
})
NOT_WORTH_REPAIR = frozenset({"TOASTER", "COFFEE_MAKER"})
HAZARDS = frozenset({"GAS_SMELL", "SMOKE", "ARCING", "WATER_NEAR_LIVE_PARTS"})
FORBIDDEN_WORK = frozenset({"BUILDING_WIRING", "PLUMBING", "GAS_LINE"})
LICENSED_POWER_SOURCES = frozenset({"GAS", "HARDWIRED"})

PRIMARY_DISTRICTS = frozenset({"NORTH QUAY", "OLD MILL", "GREENFIELD", "HARBOR HEIGHTS"})
SECONDARY_DISTRICTS = frozenset({"EASTBROOK"})


@dataclass(frozen=True)
class Unit:
    appliance_type: str
    power_source: str = "ELECTRIC"  # ELECTRIC | GAS | HARDWIRED
    customer_supplied_part: bool = False


@dataclass(frozen=True)
class EligibilityInput:
    location_type: str  # RESIDENTIAL | COMMERCIAL
    units: tuple[Unit, ...]
    district: str | None = None
    hazards: frozenset[str] = frozenset()
    requested_work: frozenset[str] = frozenset()
    # True only when a technician's gas/hardwired clearance is recorded; never from the customer.
    clearance_on_record: bool = False


@dataclass(frozen=True)
class Finding:
    code: str
    result: Eligibility
    message: str


@dataclass(frozen=True)
class EligibilityResult:
    result: Eligibility
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def codes(self) -> list[str]:
        return [f.code for f in self.findings]


def evaluate_eligibility(request: EligibilityInput) -> EligibilityResult:
    findings: list[Finding] = []

    def add(code: str, result: Eligibility, message: str) -> None:
        findings.append(Finding(code, result, message))

    for hazard in sorted(request.hazards & HAZARDS):
        add(f"HAZARD_{hazard}", Eligibility.EMERGENCY_STOP,
            "Emergency signal: stop automatic handling and escalate to the owner")

    for work in sorted(request.requested_work & FORBIDDEN_WORK):
        add(f"FORBIDDEN_WORK_{work}", Eligibility.DECLINE,
            "Building wiring, plumbing and gas-line work are outside the service scope")

    if not request.units:
        add("NO_UNITS", Eligibility.OWNER_REVIEW, "No appliance units were provided")

    for i, unit in enumerate(request.units, start=1):
        name = f"unit {i} ({unit.appliance_type})"
        if unit.appliance_type in NOT_WORTH_REPAIR:
            add(f"UNIT_{i}_NOT_SERVICED", Eligibility.DECLINE, f"{name}: not serviced")
        elif unit.appliance_type not in SUPPORTED_APPLIANCES:
            add(f"UNIT_{i}_OUT_OF_CATALOG", Eligibility.OWNER_REVIEW,
                f"{name}: not in the appliance catalog")
        if unit.power_source in LICENSED_POWER_SOURCES and not request.clearance_on_record:
            add(f"UNIT_{i}_LICENSE_REQUIRED", Eligibility.LICENSE_HOLD,
                f"{name}: {unit.power_source} appliance needs a recorded technician clearance")
        if unit.customer_supplied_part:
            add(f"UNIT_{i}_CUSTOMER_PART", Eligibility.CONDITIONAL,
                f"{name}: customer-supplied part needs exact model and verified fit")

    if request.location_type == "COMMERCIAL":
        add("COMMERCIAL_LOCATION", Eligibility.OWNER_REVIEW,
            "Commercial objects are reviewed case by case")

    if request.district:
        district = request.district.strip().upper()
        if district in SECONDARY_DISTRICTS:
            add("DISTRICT_SECONDARY", Eligibility.CONDITIONAL,
                "Secondary area: depends on schedule")
        elif district not in PRIMARY_DISTRICTS:
            add("DISTRICT_UNKNOWN", Eligibility.OWNER_REVIEW, "Area is not on the approved list")

    findings.sort(key=lambda f: -SEVERITY[f.result])
    overall = findings[0].result if findings else Eligibility.ELIGIBLE
    return EligibilityResult(overall, tuple(findings))


def _upper_strs(value: Any) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(v.strip().upper() for v in value if isinstance(v, str))


def request_from_payload(payload: dict[str, Any]) -> EligibilityInput:
    """Tolerant mapping of a raw intake payload; malformed parts are simply ignored."""
    raw_units = payload.get("units")
    units = tuple(
        Unit(
            appliance_type=str(u.get("appliance_type", "")).strip().upper(),
            power_source=str(u.get("power_source", "ELECTRIC")).strip().upper(),
            customer_supplied_part=u.get("customer_supplied_part") is True,
        )
        for u in (raw_units if isinstance(raw_units, list) else [])
        if isinstance(u, dict)
    )
    district = payload.get("district")
    return EligibilityInput(
        location_type=str(payload.get("location_type", "")).strip().upper(),
        units=units,
        district=district if isinstance(district, str) else None,
        hazards=_upper_strs(payload.get("hazards")),
        requested_work=_upper_strs(payload.get("requested_work")),
    )


# --- visit fees (exact decimal arithmetic, never floats) ------------------------------------

RESIDENTIAL_FEES: tuple[Decimal, ...] = (Decimal("45.00"), Decimal("35.00"), Decimal("25.00"))
COMMERCIAL_MINIMUM = Decimal("90.00")


@dataclass(frozen=True)
class FeeLine:
    unit_index: int  # 1-based
    status: str  # FIXED | QUOTE_REQUIRED
    amount: Decimal | None = None  # set only for FIXED
    minimum: Decimal | None = None  # floor for a custom quote


@dataclass(frozen=True)
class FeeQuote:
    lines: tuple[FeeLine, ...]

    @property
    def fixed_total(self) -> Decimal:
        return sum((ln.amount for ln in self.lines if ln.amount is not None), Decimal("0.00"))

    @property
    def needs_quote(self) -> bool:
        return any(ln.status == "QUOTE_REQUIRED" for ln in self.lines)


def calculate_visit_fees(location_type: str, unit_count: int) -> FeeQuote:
    if unit_count < 0:
        raise ValueError("unit_count must not be negative")
    lines: list[FeeLine] = []
    for i in range(1, unit_count + 1):
        if location_type == "COMMERCIAL":
            lines.append(FeeLine(i, "QUOTE_REQUIRED", minimum=COMMERCIAL_MINIMUM))
        elif i <= len(RESIDENTIAL_FEES):
            lines.append(FeeLine(i, "FIXED", amount=RESIDENTIAL_FEES[i - 1]))
        else:
            lines.append(FeeLine(i, "QUOTE_REQUIRED"))
    return FeeQuote(tuple(lines))


def fee_credit(
    line: FeeLine, repair_unit_index: int, repair_approved: bool, repair_completed: bool
) -> Decimal:
    """A visit fee is credited only to the repair of the same unit, once approved and done."""
    if (
        line.status == "FIXED"
        and line.amount is not None
        and line.unit_index == repair_unit_index
        and repair_approved
        and repair_completed
    ):
        return line.amount
    return Decimal("0.00")
