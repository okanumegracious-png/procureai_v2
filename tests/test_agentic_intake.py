from decimal import Decimal
import pytest
from agentic.intake import IntakeAgent, SQLiteRequestStore, deterministic_extract

def test_extracts_cement_request_and_budget():
    text = ("I need 500 bags of cement for a construction project in Port Harcourt. "
            "Delivery is required within 5 days. My budget is NGN 8 million.")
    request = deterministic_extract(text)
    assert request.item == "cement"
    assert request.quantity == Decimal("500")
    assert request.unit == "bags"
    assert request.budget == Decimal("8000000")
    assert request.required_within_days == 5
    assert "Port Harcourt" in request.delivery_location

def test_agent_saves_ready_request(tmp_path):
    store = SQLiteRequestStore(tmp_path / "test.db")
    result = IntakeAgent(store).process("Need 500 bags cement delivered in Port Harcourt within 5 days")
    assert result.sourcing_ready is True
    assert result.status == "ready_for_sourcing"
    assert store.get(result.request_id)["status"] == "ready_for_sourcing"

def test_agent_flags_missing_fields(tmp_path):
    result = IntakeAgent(SQLiteRequestStore(tmp_path / "test.db")).process(
        "I need cement for a site in Port Harcourt")
    assert result.sourcing_ready is False
    assert "quantity" in result.missing_fields
    assert "delivery deadline" in result.missing_fields

def test_empty_request_rejected(tmp_path):
    with pytest.raises(ValueError):
        IntakeAgent(SQLiteRequestStore(tmp_path / "test.db")).process("   ")
