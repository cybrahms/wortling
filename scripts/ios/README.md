# Native Shortcut checks

These tests run Apple's Shortcuts, Safari, Files and Photos inside an iPhone
Simulator. Browser automation cannot establish that a Shortcut executes correctly.

Requirements: Xcode, an installed iOS runtime, `xcodegen`, and the locked Python
environment. This station uses Xcode 26.6 with the iOS 26.5 arm64 runtime. To install
the runtime on another Mac:

```sh
xcodebuild -downloadPlatform iOS -architectureVariant arm64
```

From the repository root, run:

```sh
bash scripts/ios/test.sh
```

The script creates or reuses **Wortling Shortcut Test**, signs a test copy of the
generated template with only its capture endpoints changed to localhost, and starts
the real capture API on `127.0.0.1:8791`. It uses a public dummy token, an isolated
SQLite database, disabled processing, and disabled Anki writes. No live learner
configuration or AnkiWeb account is used. The script stops its API on exit.

Each test downloads and imports the signed file through Safari and Files, fills in
the setup question, and verifies installation finishes. A synthetic German-text
HEIC fixture is shared from Photos. Filename-only input and normal German text
use Apple's documented `shortcuts://run-shortcut` URL scheme. Image assertions
check the stored image is a 2000 × 1500 JPEG; text assertions check exact content.
The image checks fail if a filename arrives without image bytes.

Artifacts and the test database stay under ignored `data/ios-*` paths. Result
bundles include screenshots and can be opened in Xcode. The simulator is retained
for debugging. Linux CI continues to run the Python and artifact checks; it does
not claim to run iOS.

To reproduce the old filename-only behavior without changing the checkout, save
the old plist under `data/` and set `WORTLING_SIMULATOR_TEMPLATE` to its absolute
path when invoking the script. The filename-recovery test should fail with
`Photo became text`.

The test copy has a localhost endpoint and no personal key. Never distribute it
to learners. The production download remains the artifact under `src/wortling/assets/`.
