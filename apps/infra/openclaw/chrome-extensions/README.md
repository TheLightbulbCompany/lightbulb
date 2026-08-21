# Chrome extensions baked into the container image

Branded Google Chrome (137+) ignores `--load-extension`, so extensions ride the
one install path Linux Chrome still honors: an *external extension* preferences
file at `/usr/share/google-chrome/extensions/<ID>.json` pointing at a local
`.crx` (`external_crx` + `external_version`). Chrome installs it into the
(ephemeral, /tmp) profile on every launch — no flag, no policy, no server.

## popups-to-tabs (`fdojippeikcjnafolmkpdgfcjlbnalpo`)

Folds every new Chrome window (a page's `window.open` OAuth popup, typically)
into the main window as a tab. The container's kiosk WM fullscreens every
top-level window with no switcher, so a popup otherwise covers the page in the
live view and the owner is stuck. Chrome's own tab strip is the switcher;
moving the tab keeps its renderer, so `window.opener` / `postMessage` survive.

Source: `popups-to-tabs/`. Packed artifact: `popups-to-tabs.crx` (committed —
packing needs a Chrome binary, which the image build has but CI lint does not).
To change it: edit the source, bump `version` in `manifest.json` AND in
`popups-to-tabs.external.json` (Chrome only re-installs on a higher
`external_version`), repack, and update `EXTENSION_ID` if you packed with a new
key — the ID is what the JSON filename in the Dockerfile is named after:

    # macOS (branded Chrome):
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --no-message-box \
      --pack-extension=$PWD/popups-to-tabs --user-data-dir=/tmp/pack-ud          # → .crx + .pem
    # Linux (e.g. inside the container image, which has /usr/bin/google-chrome):
    google-chrome --headless=new --no-sandbox --disable-gpu \
      --pack-extension=$PWD/popups-to-tabs --user-data-dir=/tmp/pack-ud          # same outputs
    # Re-sign with the SAME key to keep the id: add --pack-extension-key=popups-to-tabs.pem
    openssl rsa -in popups-to-tabs.pem -pubout -outform DER | openssl dgst -sha256 -binary \
      | head -c 16 | xxd -p -c 32 | tr '0-9a-f' 'a-p'                              # → EXTENSION_ID

Keep the `.pem` OUT of the repo (nothing ever fetches updates, so the key only
pins the ID; a new key just means a new ID + JSON rename).
