"""S4 Capability Runners — isolated execution backends.

Runners provide sandboxed execution for capability tools. The Gateway delegates
actual execution to runners via authenticated IPC. Supported backends:

- prebuilt: local dedicated provider runner service (local-product)
- remote_http: remote HTTP endpoint with precise origin/network zone control
- kubernetes: per-invocation Job/Pod (production)

No Docker socket or K8s credential on API/Agent Worker (S4 spec §5).
"""

from __future__ import annotations
