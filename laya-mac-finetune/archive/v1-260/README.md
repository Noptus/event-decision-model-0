# v1 — 260-event proof of concept

Frozen snapshot of the original six-route experiment before the enterprise eight-route dataset was introduced.

- Data: 160 train / 40 validation / 60 test synthetic events
- Routes: order-processing, payment-operations, logistics, inventory-management, customer-support, fraud-review
- Selected checkpoint: `../../outputs/models/v1-260/` (large, ignored)
- Recorded results: `results/`
- Selected run: head-only MPS, step 50, 118.8 seconds
- Test accuracy: base 0.8333, tuned 0.8333
- Raw NLL: base 0.5260, tuned 0.4736

The copied scripts and config preserve the implementation used for this result. They are archival; use the project-root scripts for the current experiment.
