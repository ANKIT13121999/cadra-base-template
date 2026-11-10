# Artefact

Fill this file with the written deliverable your problem brief asks for (for example, a
Reconciliation Report). The grader reads this exact file — do not rename or move it.

## Not built

- **Persistent ledger.** The SQLite run/ledger DB (`DB_PATH`, default `build/runs.db`) is on an ephemeral filesystem on the Render free tier, so runs and approvals reset on restart. Production needs a managed database or persistent volume.
- **Outbound actions.** The service never sends email or issues refunds; drafts are proposals only.
- **Operational hardening.** No auth, rate limiting, monitoring, or backup/restore for the ledger yet.
