"""Synthetic seed data. Allowed only in local/test."""
from app.audit import audit
from app.config import LOCAL_ENVS, Settings
from app.models import Identity, RepairCase

IDENTITIES = {
    "owner-1": "owner",
    "disp-1": "dispatcher",
    "tech-1": "technician",
    "tech-2": "technician",
    "dev-1": "developer",
}
SEEDED_ACCEPTED_CASE_ID = "00000000-0000-4000-8000-000000000001"  # assigned to tech-1
SEEDED_OTHER_TECH_CASE_ID = "00000000-0000-4000-8000-000000000002"  # assigned to tech-2


def seed(session) -> None:
    for identity_id, role in IDENTITIES.items():
        if session.get(Identity, identity_id) is None:
            session.add(Identity(identity_id=identity_id, role=role))
    session.flush()
    for case_id, technician, name, units in (
        (SEEDED_ACCEPTED_CASE_ID, "tech-1", "Sample Customer Seeded",
         '[{"appliance_type": "REFRIGERATOR", "problem": "Not cooling"}]'),
        (SEEDED_OTHER_TECH_CASE_ID, "tech-2", "Sample Customer Other",
         '[{"appliance_type": "DISHWASHER", "problem": "Leaks at the door"}]'),
    ):
        existing = session.get(RepairCase, case_id)
        if existing is not None:
            existing.assigned_technician_id = existing.assigned_technician_id or technician
            continue
        session.add(RepairCase(
            case_id=case_id, case_phase="ACCEPTED", intake_status="READY_FOR_REVIEW",
            source_attribution="seed", assigned_technician_id=technician,
            contact_display_name=name, contact_phone="+1-555-0199",
            location_type="RESIDENTIAL", units_json=units,
        ))
        session.flush()
        audit(session, "CASE_CREATED", "system:seed", case_id=case_id, case_phase="ACCEPTED")
    session.commit()


def main() -> None:
    from app.db import make_engine, make_sessionmaker

    settings = Settings()
    if settings.app_env not in LOCAL_ENVS:
        raise SystemExit("Synthetic seed is allowed only in local/test")
    with make_sessionmaker(make_engine(settings.database_url))() as session:
        seed(session)
    print("seeded")


if __name__ == "__main__":
    main()
