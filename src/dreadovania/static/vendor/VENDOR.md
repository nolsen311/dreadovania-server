# Vendored files

Third-party files served from this origin (CSP `script-src 'self'`). Each is
pinned to an exact version and checked against the SHA-256 below by
`tests/test_ui.py`, so any change fails CI.

| File | Version | Source | License | SHA-256 |
| --- | --- | --- | --- | --- |
| `htmx-2.0.11.min.js` | 2.0.11 | npm `htmx.org` (`dist/htmx.min.js`); tarball verified against npm's published sha512 integrity | 0BSD (`htmx-LICENSE.txt`) | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |

To update: download the new release tarball from the npm registry, verify its
`dist.integrity`, copy `dist/htmx.min.js` here under the new version's name,
and update this table, the test, and `templates/base.html`.
