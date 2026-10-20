"""Owner phone page: a browser opening an owner link gets the HTML page; API calls to the same URL
(the page's own fetches, scripts, anything with a token header) still get JSON."""

from __future__ import annotations

import httpx
import pytest

from askonce_server.app.links import OwnerLinkSigner

pytestmark = pytest.mark.anyio

BAKERY = "thimbleberry-bakehouse"
BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


@pytest.mark.parametrize("path", [f"/owner/{BAKERY}/inbox", f"/owner/{BAKERY}/requests/c_pending1"])
async def test_browser_navigation_gets_the_page(api: httpx.AsyncClient, path: str) -> None:
    response = await api.get(path, params={"token": "anything"}, headers={"accept": BROWSER_ACCEPT})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    # The page carries no tenant data: everything comes from the authenticated API.
    assert BAKERY not in response.text
    assert "Authorization" in response.text


async def test_api_calls_still_get_json(api: httpx.AsyncClient, app_signer: OwnerLinkSigner, seeded: object) -> None:
    token = app_signer.sign(business_id=BAKERY, scope="inbox")
    with_header = await api.get(
        f"/owner/{BAKERY}/inbox", headers={"accept": BROWSER_ACCEPT, "authorization": f"Bearer {token}"}
    )
    assert with_header.status_code == 200
    assert with_header.json()["business_id"] == BAKERY
    no_html = await api.get(f"/owner/{BAKERY}/inbox", params={"token": token}, headers={"accept": "application/json"})
    assert no_html.json()["business_id"] == BAKERY


@pytest.mark.parametrize(
    "path", [f"/owner/{BAKERY}/links", f"/owner/{BAKERY}/requests/c_pending1/decision", "/owner/Bad Id/inbox"]
)
async def test_other_owner_paths_are_never_the_page(api: httpx.AsyncClient, path: str) -> None:
    response = await api.get(path, headers={"accept": BROWSER_ACCEPT})
    assert not response.headers.get("content-type", "").startswith("text/html")
