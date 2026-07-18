"""backoffice — the stack's business-admin org: double-entry ledger, ingest,
reporting/close, and the AR/HR/procurement/compliance registries. Work work
lane is firewalled out by design (see ingest.WORK_SOURCES)."""
from . import ledger, ingest, reports, admin  # noqa: F401
