"""Issue #160 guard: the orchestrator Lambda must cap concurrent executions.

The orchestrator runs 512MB with a 15-minute timeout and fans out via async
self-invoke (``InvocationType='Event'``). Without reserved concurrency a burst
of CI failures spawns unbounded concurrent executions that exhaust the account
limit and DoS the function itself.

Run: ``python check_reserved_concurrency.py`` from ``infra/aws/``.
Exit 0 = capped, exit 1 = exposed (prints which check failed).
Stdlib only — no new dependencies.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LAMBDA_TF = ROOT / "lambda.tf"
VARIABLES_TF = ROOT / "variables.tf"


def check(label: str, cond: bool) -> bool:
    print(("PASS" if cond else "FAIL") + ": " + label)
    return cond


def main() -> int:
    lambda_src = LAMBDA_TF.read_text(encoding="utf-8")
    vars_src = VARIABLES_TF.read_text(encoding="utf-8")

    ok = True
    ok &= check(
        "reserved_concurrent_executions set on the main Lambda",
        "reserved_concurrent_executions" in lambda_src,
    )
    ok &= check(
        "lambda_reserved_concurrent_executions variable defined",
        'variable "lambda_reserved_concurrent_executions"' in vars_src,
    )

    print("OK: Lambda concurrency is capped" if ok else "OPEN #160: Lambda concurrency uncapped")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
