import pytest
from email.message import EmailMessage

import main as procureai_main
import supplier_portal


def test_rfq_email_uses_shared_delivery_and_supports_multiple_addresses(monkeypatch):
    captured = {}

    def fake_delivery(message, recipients, text_body, html_body):
        captured["message"] = message
        captured["recipients"] = recipients
        captured["text"] = text_body
        captured["html"] = html_body

    monkeypatch.setattr(procureai_main, "_deliver_supplier_email", fake_delivery)
    supplier_portal._send_email(
        "first@supplier.example; second@supplier.example",
        "RFQ test", "Quotation requested", "<p>Quotation requested</p>",
    )
    assert captured["recipients"] == ["first@supplier.example", "second@supplier.example"]
    assert captured["message"]["To"] == "first@supplier.example, second@supplier.example"


def test_rfq_email_rejects_invalid_recipient_before_delivery(monkeypatch):
    called = False

    def fake_delivery(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(procureai_main, "_deliver_supplier_email", fake_delivery)
    with pytest.raises(RuntimeError, match="missing or invalid"):
        supplier_portal._send_email(
            "valid@supplier.example; not-an-email", "RFQ test", "Body", "<p>Body</p>"
        )
    assert called is False


def test_shared_delivery_rejects_empty_recipient_list():
    message = EmailMessage()
    message["Subject"] = "RFQ test"
    with pytest.raises(Exception, match="At least one valid supplier email"):
        procureai_main._deliver_supplier_email(message, [], "Body", "<p>Body</p>")
