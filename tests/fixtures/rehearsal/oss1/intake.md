### Repository

kintsugi-practice/crm-practice

### Agent entry point

practice_crm/agent.py

### Run command

python -m hostkit.evalrun --host practice_crm --split train

### Model provider

none (the practice sync makes no model calls)

### What goes wrong

DISCLOSED REHEARSAL FIXTURE (Stage 10, A-73): a filled-in Repair Clinic intake for the OSS-1
onboarding rehearsal. The repository slug is fictional and never contacted; the checkout is the
disclosed CRM practice host (tests/fixtures/rehearsal/crm_practice). Fixes arrive as pull
requests only (foreign mode). Symptom: when the geo API rate-limits mid-batch, some contacts
are created twice in the CRM (FAIL:DUPLICATE_CONTACT), a few batches in a hundred.

### Consent

- [x] I maintain this repository or have its maintainers' permission
- [x] Omega may analyse a local copy of the repository read-only
- [x] Fixes arrive as pull requests; only maintainers merge
- [x] Traces are exported by us; Omega never collects them
- [x] The repair receipt and its proof bundle may be published
