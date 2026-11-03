"""Where the deployment this process serves lives (STU-1995).

Every path a deployment owns (`rules/`, `.studio/`, `audits/`, `state/`, `logs/`,
`reports/`, `.env`) used to be resolved off the package's own parent, which is only the
deployment while the kernel and its first deployment share one checkout. Installed as a dependency, that
parent is `site-packages`, and the webhook would read a policy that is not there.

`$EMAIL_CLASSIFIER_DEPLOYMENT_ROOT` names the deployment. Unset, the package's parent
stays the answer, so a single-checkout deployment behaves exactly as before. Resolved
once at import: module-level defaults are built from it, and a deployment does not move
under a running process.
"""

import os
from pathlib import Path

ROOT_ENV = "EMAIL_CLASSIFIER_DEPLOYMENT_ROOT"

ROOT = Path(os.environ.get(ROOT_ENV) or Path(__file__).resolve().parents[1])
