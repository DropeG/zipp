from __future__ import annotations

import json
import os
from copy import deepcopy

import pytest
import requests

from stock_sync.config import Settings
from stock_sync.errors import RetryableSyncError, ReviewRequiredError
from stock_sync.meli import MeliClient, MeliTransport
from stock_sync.models import MeliListing, MeliOrderLine


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self, **kwargs):
        if isinstance(self.payload, Exception):
            raise self.payload
        return deepcopy(self.payload)


class FakeSession:
    def __init__(self):
        self.replies = []
        self.calls = []

    def queue(self, *payloads):
        self.replies.extend(p if isinstance(p, (Response, Exception)) else Response(p) for p in payloads)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert self.replies, f"Unexpected request: {method} {url}"
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def settings():
    return Settings("https://example.myshopify.com", "token", "secret", "app", "client-secret",
                    "100", "webhook", "2026-07", 1024, "test.db")


@pytest.fixture
def tokens_file(tmp_path, monkeypatch):
    monkeypatch.setattr("stock_sync.meli.time.time", lambda: 1000)
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"access_token": "old-access", "refresh_token": "old-refresh", "expires_at": 5000}))
    return path


@pytest.fixture
def session():
    return FakeSession()


@pytest.fixture
def meli(settings, session, tokens_file):
    return MeliClient(settings, transport=MeliTransport(settings, tokens_file=tokens_file, session=session))


def order(*, sku="ABC", variation_id=None, seller_id=100, status="paid", order_id=2001):
    return {"id": order_id, "seller": {"id": seller_id}, "status": status,
            "date_closed": "2026-09-06T12:00:00.000-04:00", "date_created": "2026-09-06T11:00:00.000-04:00",
            "currency_id": "CLP", "order_items": [
                {"item": {"id": "MLC1", "title": "Widget", "variation_id": variation_id,
                          "seller_custom_field": sku}, "quantity": 2, "unit_price": 12990, "currency_id": "CLP"}]}


def item(item_id="MLC1", *, sku="ABC", variations=None):
    return {"id": item_id, "seller_id": 100, "seller_custom_field": sku,
            "attributes": [], "available_quantity": 7, "variations": variations or []}


def variation(variation_id=8, sku="VAR", quantity=4):
    return {"id": variation_id, "seller_custom_field": None, "available_quantity": quantity,
            "attributes": [{"id": "SELLER_SKU", "value_name": sku}]}


def page(results, total, offset=0, scroll_id=None):
    payload = {"results": results, "paging": {"total": total, "offset": offset, "limit": 50}}
    if scroll_id is not None:
        payload["scroll_id"] = scroll_id
    return payload


def refreshed():
    return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600, "user_id": 100}


def test_verify_seller_uses_authenticated_user(meli, session):
    session.queue({"id": 100})
    assert meli.verify_seller() == "100"
    assert session.calls[0][:2] == ("GET", "https://api.mercadolibre.com/users/me")
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer old-access"


@pytest.mark.parametrize("payload", [{"id": 999}, {}, {"id": None}])
def test_verify_seller_rejects_wrong_or_missing_identity(meli, session, payload):
    session.queue(payload)
    with pytest.raises(ReviewRequiredError):
        meli.verify_seller()


def test_get_order_rejects_wrong_seller(meli, session):
    session.queue(order(seller_id=999))
    with pytest.raises(ReviewRequiredError, match="seller") as caught:
        meli.get_order("2001")
    assert caught.value.review_key == "order:2001"


@pytest.mark.parametrize("status", ["cancelled", "payment_required"])
def test_non_paid_order_skips_malformed_lines_and_dates(meli, session, status):
    session.queue({"id": 2001, "seller": {"id": 100}, "status": status,
                   "order_items": [{"quantity": "broken"}]})
    result = meli.get_order("2001")
    assert result.status == status
    assert result.lines == []
    assert len(session.calls) == 1


def test_non_paid_order_still_verifies_seller(meli, session):
    session.queue({"id": 2001, "seller": {"id": 999}, "status": "cancelled"})
    with pytest.raises(ReviewRequiredError, match="seller"):
        meli.get_order("2001")


def test_order_status_ignores_unrelated_malformed_historical_fields(meli, session):
    session.queue({
        "id": 2001,
        "seller": {"id": 100},
        "status": "cancelled",
        "order_items": None,
        "date_closed": "not-a-date",
    })

    assert meli.get_order_status("2001") == "cancelled"
    assert len(session.calls) == 1


def test_paid_order_preserves_status_and_exact_lines(meli, session):
    payload = order(status="paid")
    payload["order_items"].append({"item": {"id": "MLC2", "title": "Other", "variation_id": 9,
                                            "seller_custom_field": " ABC "},
                                   "quantity": 1, "unit_price": "10.50", "currency_id": "USD"})
    session.queue(payload)
    result = meli.get_order("2001")
    assert (result.order_id, result.seller_id, result.status, result.processed_at) == (
        "2001", "100", "paid", "2026-09-06T12:00:00.000-04:00")
    assert result.lines == [MeliOrderLine("MLC1", None, "Widget", 2, "12990", "CLP", "ABC"),
                            MeliOrderLine("MLC2", "9", "Other", 1, "10.50", "USD", " ABC ")]
    assert len(session.calls) == 1


def test_variation_sku_is_loaded_from_exact_item_variation(meli, session):
    session.queue(order(sku=None, variation_id=8), item(sku="WRONG", variations=[variation(9, "OTHER"), variation(8, "ABC")]))
    assert meli.get_order("2001").lines[0].sku == "ABC"
    assert session.calls[1][2]["params"] == {"include_attributes": "all"}


@pytest.mark.parametrize("variations", [[], [variation(9, "OTHER")], [variation(8, None)]])
def test_missing_variation_sku_never_falls_back_to_parent(meli, session, variations):
    session.queue(order(sku=None, variation_id=8), item(sku="WRONG", variations=variations))
    assert meli.get_order("2001").lines[0].sku == ""


def test_simple_item_sku_uses_seller_sku_attribute(meli, session):
    detail = item(sku=None)
    detail["attributes"] = [{"id": "SELLER_SKU", "value_name": "Exact-Sku"}]
    session.queue(order(sku=None), detail)
    assert meli.get_order("2001").lines[0].sku == "Exact-Sku"


@pytest.mark.parametrize("field,value", [("id", 2002), ("order_items", None), ("status", None), ("date_closed", "bad-date")])
def test_malformed_order_is_review(meli, session, field, value):
    payload = order()
    payload[field] = value
    session.queue(payload)
    with pytest.raises(ReviewRequiredError):
        meli.get_order("2001")


@pytest.mark.parametrize("field,value", [("quantity", 0), ("quantity", 1.5), ("unit_price", "NaN"), ("currency_id", None)])
def test_malformed_order_line_is_review(meli, session, field, value):
    payload = order()
    payload["order_items"][0][field] = value
    session.queue(payload)
    with pytest.raises(ReviewRequiredError):
        meli.get_order("2001")


def test_list_all_listings_uses_every_scan_page_and_variation(meli, session):
    session.queue(page(["MLC1"], 2, scroll_id="cursor"), page(["MLC2"], 2, scroll_id="cursor"),
                  item(), item("MLC2", sku="PARENT", variations=[variation(8, "A"), variation(9, "B", 0)]))
    assert meli.list_all_listings() == [MeliListing("MLC1", None, "ABC", 7),
                                       MeliListing("MLC2", "8", "A", 4), MeliListing("MLC2", "9", "B", 0)]
    assert session.calls[0][2]["params"] == {"search_type": "scan", "limit": 100}
    assert session.calls[1][2]["params"] == {"search_type": "scan", "limit": 100, "scroll_id": "cursor"}


def test_listing_scan_crosses_thousand_item_boundary(meli, session):
    ids = [f"MLC{i}" for i in range(1001)]
    for offset in range(0, 1001, 100):
        session.queue(page(ids[offset:offset + 100], 1001, scroll_id="cursor"))
    session.queue(*(item(item_id) for item_id in ids))
    listings = meli.list_all_listings()
    assert len(listings) == 1001
    assert listings[-1].item_id == "MLC1000"


@pytest.mark.parametrize("bad_page", [page([], 2), page(["MLC1"], 2), {"results": []}])
def test_incomplete_listing_pagination_is_review(meli, session, bad_page):
    session.queue(bad_page)
    with pytest.raises(ReviewRequiredError):
        meli.list_all_listings()


def test_paid_order_search_paginates_and_deduplicates(meli, session):
    session.queue(page([{"id": 2001}, {"id": 2002}], 4), page([{"id": 2002}, {"id": 2003}], 4, offset=2))
    assert meli.search_paid_orders("2026-09-01T00:00:00Z") == ["2001", "2002", "2003"]
    assert session.calls[0][1] == "https://api.mercadolibre.com/orders/search"
    assert session.calls[0][2]["params"] == {"seller": "100", "order.status": "paid",
          "order.date_created.from": "2026-09-01T00:00:00Z", "sort": "date_asc", "limit": 50, "offset": 0}
    assert session.calls[1][2]["params"]["offset"] == 2


def test_incomplete_order_search_is_review(meli, session):
    session.queue(page([{"id": 2001}], 2), page([], 2, offset=1))
    with pytest.raises(ReviewRequiredError):
        meli.search_paid_orders("2026-09-01T00:00:00Z")


@pytest.mark.parametrize("quantity,expected", [(-3, 0), (0, 0), (100000, 100000)])
def test_item_quantity_update_clamps_only_negative_values(meli, session, quantity, expected):
    result = item()
    result["available_quantity"] = expected
    session.queue(result)
    assert meli.set_available_quantity(MeliListing("MLC1", None, "ABC", 7), quantity) is None
    assert session.calls[0][:2] == ("PUT", "https://api.mercadolibre.com/items/MLC1")
    assert session.calls[0][2]["json"] == {"available_quantity": expected}


def test_variation_update_preserves_siblings_and_confirms_target(meli, session):
    session.queue(item(variations=[variation(8), variation(9)]), item(variations=[variation(8, quantity=12), variation(9)]))
    meli.set_available_quantity(MeliListing("MLC1", "8", "VAR", 4), 12)
    assert session.calls[1][:2] == ("PUT", "https://api.mercadolibre.com/items/MLC1")
    assert session.calls[1][2]["json"] == {"variations": [{"id": 8, "available_quantity": 12}, {"id": 9}]}


def test_missing_variation_does_not_write(meli, session):
    session.queue(item(variations=[variation(9)]))
    with pytest.raises(ReviewRequiredError):
        meli.set_available_quantity(MeliListing("MLC1", "8", "VAR", 4), 12)
    assert [c[0] for c in session.calls] == ["GET"]


def test_quantity_mismatch_is_review(meli, session):
    session.queue(item())
    with pytest.raises(ReviewRequiredError, match="quantity"):
        meli.set_available_quantity(MeliListing("MLC1", None, "ABC", 7), 12)


def test_token_refresh_is_atomic_and_uses_rotated_credentials(meli, session, tokens_file, monkeypatch):
    tokens_file.write_text(json.dumps({"access_token": "old-access", "refresh_token": "old-refresh", "expires_at": 1120}))
    session.queue(refreshed(), {"id": 100})
    replacements = []
    original_replace = os.replace

    def observe_replace(source, destination):
        assert json.loads(tokens_file.read_text())["access_token"] == "old-access"
        assert json.loads(source.read_text())["refresh_token"] == "new-refresh"
        replacements.append((source, destination))
        original_replace(source, destination)

    monkeypatch.setattr("stock_sync.meli.os.replace", observe_replace)
    assert meli.verify_seller() == "100"
    saved = json.loads(tokens_file.read_text())
    assert saved["expires_at"] == 4600
    assert saved["refresh_token"] == "new-refresh"
    assert len(replacements) == 1
    assert list(tokens_file.parent.iterdir()) == [tokens_file]
    assert session.calls[0][:2] == ("POST", "https://api.mercadolibre.com/oauth/token")
    assert session.calls[0][2]["data"] == {"grant_type": "refresh_token", "client_id": "app", "client_secret": "client-secret", "refresh_token": "old-refresh"}
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer new-access"


def test_401_refreshes_once_and_retries_original_request(meli, session):
    session.queue(Response({}, 401), refreshed(), order())
    assert meli.get_order("2001").order_id == "2001"
    assert [c[0] for c in session.calls] == ["GET", "POST", "GET"]
    assert session.calls[2][2]["headers"]["Authorization"] == "Bearer new-access"


def test_401_after_refresh_requires_review_without_loop(meli, session):
    session.queue(Response({}, 401), refreshed(), Response({}, 401))
    with pytest.raises(ReviewRequiredError, match="401"):
        meli.verify_seller()
    assert len(session.calls) == 3


@pytest.mark.parametrize("failure", [Response({}, 429), Response({}, 500), Response({}, 503), requests.Timeout("timeout")])
def test_transient_api_errors_are_retryable(meli, session, failure):
    session.queue(failure)
    with pytest.raises(RetryableSyncError):
        meli.get_order("2001")


@pytest.mark.parametrize("failure", [Response({}, 400), Response({}, 403), Response({}, 404), Response([], 200), Response(ValueError("bad JSON"))])
def test_permanent_or_malformed_responses_require_review(meli, session, failure):
    session.queue(failure)
    with pytest.raises(ReviewRequiredError):
        meli.get_order("2001")


@pytest.mark.parametrize("payload", ["bad json", "[]", '{}', '{"access_token":"x","refresh_token":"y","expires_at":"NaN"}'])
def test_bad_token_file_requires_review_without_network(meli, session, tokens_file, payload):
    tokens_file.write_text(payload)
    with pytest.raises(ReviewRequiredError):
        meli.verify_seller()
    assert session.calls == []


def test_invalid_refresh_does_not_replace_token_file(meli, session, tokens_file):
    previous = tokens_file.read_text()
    session.queue(Response({}, 401), {"access_token": "incomplete"})
    with pytest.raises(ReviewRequiredError):
        meli.verify_seller()
    assert tokens_file.read_text() == previous


def test_client_reads_environment_token_path(settings, session, tokens_file, monkeypatch):
    monkeypatch.setenv("MELI_TOKENS_FILE", str(tokens_file))
    monkeypatch.setattr("stock_sync.meli.requests.Session", lambda: session)
    session.queue({"id": 100})
    assert MeliClient(settings).verify_seller() == "100"


@pytest.mark.parametrize("attributes", [{}, "invalid"])
def test_malformed_sku_attributes_are_review_not_missing_sku(meli, session, attributes):
    detail = item(sku=None)
    detail["attributes"] = attributes
    session.queue(order(sku=None), detail)
    with pytest.raises(ReviewRequiredError):
        meli.get_order("2001")


@pytest.mark.parametrize("failure", [Response({}, 429), Response({}, 503), requests.Timeout("timeout")])
def test_sku_lookup_transient_failure_is_not_a_missing_sku(meli, session, failure):
    session.queue(order(sku=None), failure)
    with pytest.raises(RetryableSyncError):
        meli.get_order("2001")


@pytest.mark.parametrize("failure,error_type", [(Response({}, 429), RetryableSyncError),
                                               (Response({}, 503), RetryableSyncError),
                                               (Response({}, 400), ReviewRequiredError)])
def test_refresh_failure_preserves_existing_file(meli, session, tokens_file, failure, error_type):
    original = tokens_file.read_text()
    session.queue(Response({}, 401), failure)
    with pytest.raises(error_type):
        meli.verify_seller()
    assert tokens_file.read_text() == original


def test_401_after_proactive_refresh_does_not_refresh_again(meli, session, tokens_file):
    tokens_file.write_text(json.dumps({"access_token": "old-access", "refresh_token": "old-refresh", "expires_at": 1100}))
    session.queue(refreshed(), Response({}, 401))
    with pytest.raises(ReviewRequiredError):
        meli.verify_seller()
    assert [c[0] for c in session.calls] == ["POST", "GET"]


def test_failed_atomic_replace_preserves_previous_file(meli, session, tokens_file, monkeypatch):
    original = tokens_file.read_text()
    session.queue(Response({}, 401), refreshed())

    def fail_replace(source, destination):
        raise OSError("disk failure")

    monkeypatch.setattr("stock_sync.meli.os.replace", fail_replace)
    with pytest.raises(ReviewRequiredError, match="could not be saved"):
        meli.verify_seller()
    assert tokens_file.read_text() == original
    assert list(tokens_file.parent.iterdir()) == [tokens_file]


def test_missing_token_file_requires_review(meli, session, tokens_file):
    tokens_file.unlink()
    with pytest.raises(ReviewRequiredError):
        meli.verify_seller()
    assert session.calls == []


def test_unpaid_order_without_closed_date_does_not_need_import_details(meli, session):
    payload = order(status="payment_required")
    payload["date_closed"] = None
    session.queue(payload)
    result = meli.get_order("2001")
    assert result.status == "payment_required"
    assert result.lines == []


def test_sku_fallback_rejects_item_from_another_seller(meli, session):
    detail = item()
    detail["seller_id"] = 999
    session.queue(order(sku=None), detail)
    with pytest.raises(ReviewRequiredError, match="seller"):
        meli.get_order("2001")


def test_variation_response_quantity_must_match_target(meli, session):
    session.queue(item(variations=[variation(8)]), item(variations=[variation(8, quantity=4)]))
    with pytest.raises(ReviewRequiredError, match="quantity"):
        meli.set_available_quantity(MeliListing("MLC1", "8", "VAR", 4), 12)


def test_empty_catalog_is_valid(meli, session):
    session.queue(page([], 0))
    assert meli.list_all_listings() == []


def test_repeated_scan_page_requires_review(meli, session):
    session.queue(page(["MLC1"], 3, scroll_id="cursor"), page(["MLC1"], 3, scroll_id="cursor"))
    with pytest.raises(ReviewRequiredError, match="repeated listing"):
        meli.list_all_listings()
