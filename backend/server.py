"""BitBotPro API process (Spec 012). App wiring only (spec §2).

    uvicorn server:app --host 0.0.0.0 --port 8000 --workers 1 --no-proxy-headers

The legacy monolith that lived here was retired in Phase 6B (spec §14,
``specs/012-clean-hierarchy/legacy-inventory.md``); it is preserved in git
(commit 1c20d2a, production image ``bitbotpro-api:spec011-f245d7ed3bdb``).
Everything is assembled by :mod:`api.production` from the environment; a
configuration error stops the process with a message naming the variable.
"""
import logging
import os
import sys

import log_redaction

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log_redaction.install()          # spec 008: secrets, tokens and webhook paths never reach the logs

from api.production import ProductionConfig, ProductionConfigError, create_api_app  # noqa: E402

try:
    config = ProductionConfig.from_environment(os.environ)
except ProductionConfigError as exc:
    print(f"configuration error: {exc}", file=sys.stderr)
    raise SystemExit(78)

app = create_api_app(config)
