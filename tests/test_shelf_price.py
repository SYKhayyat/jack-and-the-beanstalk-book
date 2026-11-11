"""The shelf has to sell the book, signed in or not (U3).

`price_label` depends on a signed-in wallet, because it is the header's money
line. The story cards used it too, so the one page that has to make the offer —
the public landing shelf — never showed the fee. `book_price` is the price
whenever payments are on, independent of who is looking.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.helpers import RecordingProvider
from tests.test_wallet_routes import FakeStripe


def test_the_signed_out_shelf_shows_the_price(settings: Settings) -> None:
    app = create_app(
        settings,
        provider=RecordingProvider(),
        payments=FakeStripe(),
    )
    with TestClient(app) as client:
        page = client.get("/").text

    assert "$0.99" in page
    assert "20 pages and a cover" in page


def test_a_deployment_without_payments_does_not_pretend_to_charge(
    settings: Settings,
) -> None:
    app = create_app(settings, provider=RecordingProvider())
    with TestClient(app) as client:
        page = client.get("/").text

    assert "$0.99" not in page
    assert "20 pages and a cover" in page
