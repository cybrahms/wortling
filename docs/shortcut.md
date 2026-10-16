# Wortling iPhone Shortcut

After your operator builds a signed **Capture German v3** template, it is available from the service. Remove older
copies so you select the new version from the share sheet.

## Install on your iPhone

1. Turn on Tailscale and open Wortling in your signed-in browser.
2. Expand **Set up iPhone sharing** and tap **Copy Shortcut key**.
3. Tap **Install Capture German v3**. The file should be named **Capture German v3.shortcut**.
   If Firefox downloads it without opening Shortcuts, open Apple's **Files** app
   and search for **Capture German**. Firefox downloads are usually under
   **Browse → On My iPhone → Firefox → Downloads**. Tap the file there.
   If it only previews, touch and hold the file and, if available, choose
   **Open With → Shortcuts**. Make sure Apple's Shortcuts app is installed.
4. In the installation question, paste your copied key and finish adding the
   Shortcut. This is a one-time setup, not a key you type on every capture.
5. Share a German sentence or photo → **Capture German v3**. Allow network/photo
   access when asked. Check for “Captured”, then the saved entry in Wortling’s queue.
6. Run the Shortcut directly to type an overheard word. Optionally add it to your
   Home Screen through Shortcuts.

Each learner copies their own key from their signed-in page. Share only a
credential-free template. The service operator must first build a template for
the deployment hostname using the instructions below. No signed template is
distributed in this repository.

## Manual build alternative

Build one Shortcut named **Capture German**. Use the same actions for each learner;
the endpoint and bearer token are installation-specific. Obtain the token from the
server operator; do not put it into screenshots or shared Shortcut links.

## Setup

1. In Shortcuts, create Capture German. Open Details and enable **Show in Share Sheet**.
   Accept Text, URLs, Safari Web Pages, and Images. Also add it to the Home Screen.
2. Add a Text action containing the endpoint base URL, initially
   `https://wortling.example.invalid`, and Set Variable `Endpoint`.
3. Add a Text action for the learner's token and Set Variable `Token`. Under Shortcut
   Details → Setup, add an import question for this Text action: “Your Wortling token”.
   A distributed copy should contain a placeholder, never a real token.
4. Tailnet trial: enable Tailscale on the phone. The endpoint is tailnet-only.

## Input and payload

5. If **Shortcut Input has no value**, Ask for Input (Text), “What German did you hear?”
   and use that as the input. Otherwise use Shortcut Input.
6. Get Current Date and Format Date using ISO 8601, including the timezone. Set
   variable `ClientTimestamp`.
7. Combine the current date (including milliseconds) and a Random Number into
   `CaptureAttempt`. Keep it with this payload if retrying.
8. Build a Dictionary with `client_ts` = ClientTimestamp. Process the input by type:
   - Text: set `text` to the exact input, preserving punctuation and line breaks.
   - URL: set `url` to the URL. If selected text or Safari page text is also available,
     use Get Details of Safari Web Page → Page Contents and set `text` too. The worker
     can fetch public HTML/text URLs asynchronously; selected text also works for
     pages requiring a login or unsupported document formats.
   - Image: Get Details of Images → Width and Height. If width ≥ height, Resize Image
     to width 2000, automatic height; otherwise resize to automatic width, height 2000.
     Skip enlargement when the long edge is already ≤2000. Convert Image to JPEG
     (quality about 80%). Base64 Encode with **Line Breaks: None**. Set `image_b64`
     to that text and `image_mime` to `image/jpeg`. The raw image is retained server-side.
   Use one request per image if multiple images are shared. A long article remains
   one text capture; the pipeline caps its extracted candidates at 25.

## Send

9. In each input-type branch, add Get Contents of URL at `Endpoint` + `/v1/captures`:
   - Method POST.
   - Headers `Authorization`: `Bearer ` + Token, and `Idempotency-Key`: CaptureAttempt.
   - Request Body JSON: use **Add new field** to add `client_ts` (Text, variable
     ClientTimestamp), then the fields for that branch: `text` for text; `url`
     and optionally `text` for a page; `image_b64` and `image_mime` for an image.
     Set each field's value to the corresponding variable from step 8. This
     reproduces the Dictionary without needing a whole-body Dictionary picker.
     Add only applicable fields; avoid sending empty image fields.
   - Set variable `CaptureResponse` to the URL action's result, so the common
     success check after the branches can read it.
10. Get Dictionary Value `id` from CaptureResponse. If it has a value, Show Notification
    “Captured”. Otherwise Show Alert containing the response's `detail` field.
    A transport failure stops the native URL action and displays Shortcuts' network
    error. Do not display “Captured” after that action fails.

The server returns 202 only after storing the capture and durable job. It performs
no language-model calls during capture. Shortcuts may ask for network/photo access
on the first run. The 10 MB request limit includes base64 overhead.

## Acceptance trial on each phone

- Share selected Safari text; verify “Captured”, then check the stored text.
- Share a screenshot and a photo of a letter; verify both images are retained.
- Run without input; type an overheard word.
- Try an invalid token; verify an error, never a success notification.
- Retry an identical payload with the same CaptureAttempt and timestamp: same ID.
- Turn Tailscale off: observe a network failure, then restore it.

## Offline retry (later milestone)

The basic Shortcut has no unattended offline queue. Do not regenerate CaptureAttempt
or ClientTimestamp when manually retrying an existing request. A durable offline
queue and retry helper will be documented separately when implemented.

Apple documents [JSON request fields](https://support.apple.com/en-ca/guide/shortcuts-mac/apd58d46713f/mac)
and [import questions](https://support.apple.com/en-euro/guide/shortcuts/apdf330fd3a0/ios).
Action labels can vary slightly by iOS version; phone acceptance remains a manual
check, not something the server tests establish.

Native simulator checks, including installation and HEIC capture, are documented
in [scripts/ios/README.md](../scripts/ios/README.md). These use an isolated capture
API with processing and Anki writes disabled. A successful simulator run does not
establish how every physical phone or sharing app supplies its input.

Apple documents [opening a file with a chosen app](https://support.apple.com/guide/iphone/iphe4bff8827/ios).
Mozilla documents [finding and sharing Firefox downloads](https://support.mozilla.org/en-US/kb/download-photos-and-files-firefox-ios).

## Rebuilding the installable template

Source: `shortcuts/CaptureGerman.cherri`; the inspectable unsigned placeholder
template and hash manifest are alongside it. Neither contains a deployment address
or learner credential. Build for your own HTTPS origin on macOS using
[Cherri](https://github.com/electrikmilk/cherri) at commit
`606c9a25e481f08bff43bedb93be5e3d9b1229c9`:

```sh
uv run python scripts/build-shortcut.py --compiler /path/to/cherri --base-url https://YOUR_HOST
```

The builder invokes Apple's `shortcuts sign --mode anyone` on a template with an
empty key. Apple receives the endpoint and template for validation. The generated
signed asset and deployment-specific build metadata are ignored by Git. Build the
asset before building the Docker image to enable downloads. Until then the
download endpoint returns 503; text and browser-photo captures remain available.
Never commit or distribute a learner's installed Shortcut containing their key.
Native simulator tests use the unsigned placeholder and their own local API.
