import base64
import io
from importlib.resources import files

import pytest
from PIL import Image
from playwright.sync_api import sync_playwright


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.firefox.launch()
        page = browser.new_page()
        page.route(
            "https://wortling.test/",
            lambda route: route.fulfill(
                content_type="text/html", body=files("wortling").joinpath("trial.html").read_text()
            ),
        )
        page.route(
            "**/v1/report",
            lambda route: route.fulfill(
                json={
                    "learner": "Test",
                    "processing_enabled": False,
                    "writes_enabled": False,
                    "known_index": {"entries": 0},
                    "usage_month": {"cost_usd": 0},
                    "alerts": [],
                    "cards": [],
                    "captures": [],
                    "decisions": [],
                }
            ),
        )
        yield page
        browser.close()


def test_image_upload_without_bitmap_decoder(page):
    page.add_init_script("window.createImageBitmap=async()=>{throw new TypeError('Type error')}")
    sent = []

    def receive(route):
        sent.append(route.request.post_data_json)
        route.fulfill(status=202, json={"id": "test-capture", "status": "captured"})

    page.route("**/v1/captures", receive)
    page.goto("https://wortling.test/")
    page.locator("#manual-capture summary").click()
    image = io.BytesIO()
    Image.new("RGB", (2400, 1200), "white").save(image, "JPEG")
    page.locator("#image").set_input_files(
        {"name": "photo.jpg", "mimeType": "image/jpeg", "buffer": image.getvalue()}
    )
    page.get_by_role("button", name="Capture", exact=True).click()
    page.wait_for_function("document.querySelector('#status').textContent==='Captured.'")
    assert len(sent) == 1
    assert sent[0]["image_mime"] == "image/jpeg"
    with Image.open(io.BytesIO(base64.b64decode(sent[0]["image_b64"]))) as resized:
        assert resized.format == "JPEG"
        assert resized.size == (2000, 1000)


def test_undecodable_photo_has_actionable_error_and_is_not_sent(page):
    sent = []
    page.route("**/v1/captures", lambda route: sent.append(route.request))
    page.goto("https://wortling.test/")
    page.locator("#manual-capture summary").click()
    page.locator("#image").set_input_files(
        {"name": "broken.heic", "mimeType": "image/heic", "buffer": b"invalid image"}
    )
    page.get_by_role("button", name="Capture", exact=True).click()
    page.wait_for_function("document.querySelector('#status').textContent.includes('JPEG/PNG')")
    assert "Could not prepare capture" in page.locator("#status").inner_text()
    assert "retry the same request" not in page.locator("#status").inner_text()
    assert not sent


def test_capture_appears_in_queue_then_refreshes_when_done(page):
    state = {"captured": False, "done": False}

    def report(route):
        capture = {
            "id": "photo",
            "received_at": "2026-10-14T05:29:19+00:00",
            "text": None,
            "url": None,
            "image_mime": "image/jpeg",
            "error": None,
            "status": "processed" if state["done"] else "received",
            "processing_status": "done" if state["done"] else "pending",
        }
        route.fulfill(
            json={
                "learner": "Test",
                "processing_enabled": True,
                "writes_enabled": False,
                "known_index": {"entries": 0},
                "usage_month": {"cost_usd": 0},
                "alerts": [],
                "cards": [],
                "decisions": [],
                "captures": [capture] if state["captured"] else [],
            }
        )

    def receive(route):
        state["captured"] = True
        route.fulfill(status=202, json={"id": "photo", "status": "captured"})

    page.route("**/v1/report", report)
    page.route("**/v1/captures", receive)
    page.goto("https://wortling.test/")
    page.locator("#manual-capture summary").click()
    image = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(image, "JPEG")
    page.locator("#image").set_input_files(
        {
            "name": "photo.jpg",
            "mimeType": "image/jpeg",
            "buffer": image.getvalue(),
        }
    )
    page.get_by_role("button", name="Capture", exact=True).click()
    queued = page.locator('[data-pending-capture="photo"]')
    queued.wait_for()
    assert "Photo captured — queued for processing" in queued.inner_text()
    assert "safely saved" in queued.inner_text()
    state["done"] = True
    # No manual refresh: the pending placeholder must disappear on the next poll.
    queued.wait_for(state="detached", timeout=10000)
    assert "Photo captured — processed" in page.locator("#latest-capture").inner_text()
    page.get_by_text("Recent captures (1)", exact=True).click()
    assert "processed" in page.locator("#capture-photo").inner_text()


def test_shortcut_copy_waits_for_key_and_preserves_click_gesture(page):
    page.add_init_script("""Object.defineProperty(navigator,'clipboard',{value:{write:async items=>{
      window.copiedKey=await (await items[0].getType('text/plain')).text();
    }}});""")
    requests = []

    def key(route):
        requests.append(route.request.method)
        route.fulfill(json={"token": "test-shortcut-key"})

    page.route("**/v1/shortcut/key", key)
    page.goto("https://wortling.test/")
    assert not requests
    page.locator("#iphone-setup summary").click()
    page.get_by_role("button", name="Copy Shortcut key", exact=True).click()
    page.wait_for_function("window.copiedKey==='test-shortcut-key'")
    assert requests == ["POST"]
    assert "Copied" in page.locator("#shortcut-status").inner_text()
    assert page.locator("#shortcut-key-fallback").is_hidden()


def test_latest_capture_precedes_form_and_other_shared_messages_are_collapsed(page):
    captures = [
        {
            "id": str(index),
            "received_at": "2026-10-14T17:44:01+00:00",
            "text": text,
            "url": None,
            "image_mime": None,
            "error": None,
            "status": "received",
            "processing_status": "pending",
        }
        for index, text in enumerate(
            ["Bitte Sportsachen mitgeben.\nDanke!", "Freitag in der dritten Stunde.", "Stimmt."]
        )
    ]
    page.route(
        "**/v1/report",
        lambda route: route.fulfill(
            json={
                "learner": "Test",
                "processing_enabled": True,
                "writes_enabled": True,
                "known_index": {"entries": 100},
                "usage_month": {"cost_usd": 0},
                "alerts": [],
                "cards": [],
                "decisions": [],
                "captures": captures,
            }
        ),
    )
    page.goto("https://wortling.test/")
    latest = page.locator("#latest-capture")
    latest.get_by_text("Bitte Sportsachen mitgeben.\nDanke!", exact=True).wait_for()
    assert "queued for processing" in latest.inner_text()
    assert page.locator("#text").is_hidden()
    assert latest.bounding_box()["y"] < page.locator("#manual-capture").bounding_box()["y"]
    other = page.locator('[data-pending-capture="1"]')
    assert other.is_hidden()
    page.get_by_text("2 more saved captures · processing status", exact=True).click()
    assert other.is_visible()
    page.get_by_text("Recent captures (3)", exact=True).click()
    assert "Freitag in der dritten Stunde." in page.locator("#capture-1").inner_text()
    page.evaluate("report()")
    assert other.is_visible()
    assert page.locator("#capture-1").is_visible()


def test_cards_show_word_and_definition_with_full_card_collapsed(page):
    page.set_viewport_size({"width": 390, "height": 844})
    page.route(
        "**/v1/report",
        lambda route: route.fulfill(
            json={
                "learner": "Test",
                "processing_enabled": False,
                "writes_enabled": False,
                "known_index": {"entries": 100},
                "usage_month": {"cost_usd": 0},
                "alerts": [],
                "cards": [
                    {
                        "id": "meeting",
                        "status": "pending",
                        "created_at": "2026-10-18T10:00:00+00:00",
                        "fields": {
                            "Lemma": "die Sitzung",
                            "Gloss": "meeting &amp; session",
                            "CEFR": "B1",
                            "Text": "Die {{c1::Sitzung}} beginnt.",
                            "SentenceEN": "The meeting starts.",
                            "Forms": "Sitzungen",
                            "Source": "school",
                        },
                    }
                ],
                "captures": [],
                "decisions": [],
            }
        ),
    )
    page.goto("https://wortling.test/")
    card = page.locator('[data-card-id="meeting"]')
    card.wait_for()
    assert card.get_by_role("heading", name="die Sitzung").is_visible()
    assert card.get_by_text("meeting & session", exact=True).is_visible()
    assert card.get_by_text("Die {{c1::Sitzung}} beginnt.", exact=True).is_hidden()
    assert card.bounding_box()["height"] < 150
    card.get_by_text("Full card", exact=True).click()
    assert card.get_by_text("Die {{c1::Sitzung}} beginnt.", exact=True).is_visible()
    page.evaluate("report()")
    assert card.get_by_text("The meeting starts.", exact=True).is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
