# Dependencies

**Status:** shipped contract (2026-07-12).

Python production dependencies are split between `requirements-api.txt` and `requirements-worker.txt`, with shared pins in `constraints.txt`. Development-only tooling is in `requirements-dev.txt`. Core is headless and has no frontend or browser-test dependencies; those belong to archive frontends and the `rekolekt-web` kit.

Python 3.11 is required. `make verify` runs pip-audit. Stripe is not a code dependency: voluntary support redirects to a validated Stripe-hosted Payment Link, while billing remains disabled.

## October 26, 2026 audit refresh

Hosted job 31005 passed 1,921 tests with 84% coverage, then reported 17 advisory
matches in five dependency groups. The pinned audit repair uses:

| Group | Pin | Scope and source |
|---|---|---|
| Black | 26.10.1 | Development formatter; [upstream advisory](https://github.com/psf/black/security/advisories/GHSA-cg8m-r9f2-5wm2) |
| filelock | 4.2.1 | Transitive locking library; [upstream fixes](https://github.com/tox-dev/filelock/releases/tag/4.2.1) |
| OpenTelemetry | API/SDK/exporters 1.45.1, semantic conventions and shared transports 0.66b1 | Keep the coupled family aligned; [API advisory](https://github.com/open-telemetry/opentelemetry-python/security/advisories/GHSA-xcq7-p3pf-p88h) |
| pypdf | 6.21.2 | Development PDF assertions; [upstream security release](https://github.com/py-pdf/pypdf/releases/tag/6.21.2) |
| setuptools | 84.1.0 | Constraints, Docker build/bootstrap and verification/release bootstrap; [upstream advisory](https://github.com/pypa/setuptools/security/advisories/GHSA-xmj3-9gf7-h52w) |

The requirements/constraints files are authoritative for these environments;
the existing minimal editable-project `uv.lock` does not describe this graph.
The full Python 3.11 requirements graph resolved with these pins, and its 205
entries passed the existing advisory policy. No audit exception was added.

filelock 4 requires Python 3.11, matching this project's contract. Its
`SoftReadWriteLock` disk protocol changed at 4.0; the application has no direct
use of that lock type. Any future shared users of that protocol must upgrade
together. An API-only role release reuses the existing worker images. Classic
FileLock exclusion/release and in-memory OpenTelemetry span export passed in
the clean validation environment; hosted full-runtime verification remains a
separate release requirement.
