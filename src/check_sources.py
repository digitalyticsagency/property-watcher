"""Gate before publishing: did at least one data source actually work?

A single broken provider is survivable — the run continues and the dashboard
carries an outage banner. Every provider failing means there was nothing to
publish, so the run must fail rather than quietly ship an empty dashboard
(constraint #10).
"""

from __future__ import annotations

import sys

from common import STATUS_PATH, log, read_json


def main() -> int:
    status = read_json(STATUS_PATH, {})
    if not status:
        log.warning("no source status recorded — nothing ran?")
        return 0

    ok = sorted(k for k, v in status.items() if v.get("ok"))
    failed = sorted(k for k, v in status.items() if not v.get("ok"))

    for name in failed:
        detail = (status[name].get("detail") or "").splitlines()
        log.error("source FAILED: %s — %s", name, detail[0][:200] if detail else "")

    if not ok:
        log.error("Every configured data source failed — refusing to publish.")
        return 1

    log.info("sources ok: %s%s", ", ".join(ok),
             f" | degraded: {', '.join(failed)}" if failed else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
