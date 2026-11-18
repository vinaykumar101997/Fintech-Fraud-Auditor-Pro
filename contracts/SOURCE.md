# Screening contracts (vendored)

Copied from the `contracts/` directory of **payment-event-service** at commit
`e0a859e37dac7697bf256bf3fb9fc53474126fe3`. That repository owns them; do not edit
them here. `screening_worker.py` validates every request it reads and every verdict
it publishes against these schemas.

The files are stored with LF line endings. Each hash below is the SHA-256 of the
file's bytes with LF line endings; `tests/test_screening_worker.py` checks them,
so a local edit fails the build.

| File | SHA-256 (LF bytes) |
|---|---|
| `payment.screening.completed.v1.schema.json` | `03fa7cb4095b67309d4020dc812fab5dcd5afc9c9026b6c4a5701a5cb6656aed` |
| `payment.screening.requested.v1.schema.json` | `e24e3b6539e64d6a54c3ed8d7d7060488df2dba5d26817cdb8f0e344523570e8` |
| `examples/payment.screening.completed.v1.clear.json` | `ab61825211cf697b752c2920f7bf696f50e19f5dfb9d74afead76792023fe166` |
| `examples/payment.screening.completed.v1.error.json` | `86dd5391586b25da07bae2aa585a43d5ced5fe225d01d57913f57f94541668a1` |
| `examples/payment.screening.completed.v1.flag.json` | `d88025c06e71147542bc43ad0a57a21becaa1bca2f326e68693a97d1d493c12d` |
| `examples/payment.screening.completed.v1.reject.json` | `3e9ea33f47c531bc95257a97702d5c3ac353cf43c73e625a6e0b9dc8be5e3460` |
| `examples/payment.screening.requested.v1.json` | `a96a85e7e3341da7851f772d7b706bf8a12da451e0ec9babfc69d8f2194e1c9f` |

## Refreshing

1. Copy the changed files from payment-event-service `contracts/`, converting line
   endings to LF.
2. Update the commit above and the hash of each changed file.
3. Run `pytest tests/test_screening_worker.py`. The upstream examples must still
   validate, and the worker's own verdicts must still pass the completed schema.

A new schema version (`.v2`) is a new file and needs a worker change, not only a copy.
