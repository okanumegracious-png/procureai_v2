import main as procureai_main


def test_magic_extraction_parses_multiple_independently_priced_lines():
    message = """T10 reinforcement — 15,000 kg @ ₦780/kg
Cable 2.5mm — 20 rolls at ₦18,000 per roll
Delivery in 4 days
Transport charge ₦45,000
Payment: 70% upfront"""
    result = procureai_main._parse_supplier_quote_message(message)
    assert len(result["line_items"]) == 2
    assert result["line_items"][0]["quantity"] == 15000
    assert result["line_items"][0]["unit"] == "kg"
    assert result["line_items"][0]["unit_price"] == 780
    assert "T10" in result["line_items"][0]["description"]
    assert result["line_items"][1]["quantity"] == 20
    assert result["line_items"][1]["unit"] == "rolls"
    assert result["line_items"][1]["unit_price"] == 18000
    assert "Cable 2.5mm" in result["line_items"][1]["description"]
    assert result["lead_days"] == 4
    assert result["payment_terms"] == "70% upfront"


def test_magic_extraction_accepts_informal_single_line_wording():
    result = procureai_main._parse_supplier_quote_message(
        "I can do 15,000kg of T10 for 780 naira per kg, delivery in 4 days"
    )
    assert len(result["line_items"]) == 1
    assert result["line_items"][0]["quantity"] == 15000
    assert result["line_items"][0]["unit_price"] == 780
    assert "T10" in result["line_items"][0]["description"]


def test_magic_extraction_does_not_invent_unpriced_items():
    result = procureai_main._parse_supplier_quote_message(
        "15,000kg of T10 available. Delivery in 4 days."
    )
    assert result["line_items"] == []
