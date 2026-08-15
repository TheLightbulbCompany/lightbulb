# Apple root certificates

The trust anchors for every Apple IAP signed payload this backend accepts.
`core/services/apple_iap._verifier` pins the JWS certificate chain to these, so
they are the entire security boundary of the feature: a forged transaction that
chained to anything else is rejected.

| file | subject | expires |
|---|---|---|
| `AppleRootCA-G3.cer` | Apple Root CA - G3 | 2039-04-30 |
| `AppleRootCA-G2.cer` | Apple Root CA - G2 | 2039-04-30 |

Downloaded from <https://www.apple.com/certificateauthority/>. Both are public,
self-signed roots — nothing here is a secret, and committing them is deliberate:
fetching at image-build time would mean a network failure silently produces a
backend whose purchases all 503, and pinning to a copy in git makes the anchor
reviewable in a diff.

StoreKit 2 chains to **G3**; G2 is kept because Apple has rotated signing roots
before and the library accepts a list.

## Wiring

`APPLE_IAP_ROOT_CERTS_DIR` points at where this directory lands in the image —
`/app/certs/apple`, set by `apps/infra/lib/stacks/service-stack.ts`. The
backend `Dockerfile` does a whole-context `COPY . .` under `WORKDIR /app`, so
the path follows from this directory's location; moving it means changing that
env var too.

## Replacing them

Re-download from the URL above and confirm each file is a self-signed Apple root
before committing:

```
openssl x509 -inform DER -in AppleRootCA-G3.cer -noout -subject -issuer -enddate
```

`subject` and `issuer` must be identical (self-signed) and name Apple Inc.
`tests/unit/services/test_apple_iap_certs.py` fails if the directory stops
producing a usable verifier.
