"""S6 Verify handler: Runtime handler for evidence bundle verification.

Calls the evidence application service (via verifier) and commits
results/failures as canonical Task events.

事实源：S6 spec §4、§6。
"""

from __future__ import annotations

import logging
from typing import Any

from zhiwei.evidence.bundles import EvidenceBundle
from zhiwei.evidence.verifier import (
    VerifyExitCode,
    VerifyResult,
    map_load_error,
    verify_bundle,
)
from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput

logger = logging.getLogger(__name__)


class VerifyHandler(TaskHandler):
    """Handler for the Verify primitive.

    Verifies an evidence bundle through all layers (schema, source,
    replay, value, claim, digest) and returns a canonical result.
    """

    @property
    def primitive_type(self) -> str:
        return "Verify"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        """Verify an evidence bundle.

        Input values must contain:
          - bundle: dict representation of EvidenceBundle (required)

        Optional input values:
          - reference_bundles: dict of reference bundle dicts keyed by bundle_id
          - expected_result_copy_digests: dict of expected digests keyed by ref_id

        Output values contain:
          - status: completed | error
          - verification_ok: bool
          - exit_code: int
          - checks: list of check dicts
          - check_count: int
          - bundle_id: str (echoed back)
        """
        values = input.input_values

        bundle_dict = values.get("bundle")
        if not bundle_dict:
            return TaskOutput(
                output_values={
                    "status": "error",
                    "error": "missing bundle in input_values",
                    "verification_ok": False,
                    "exit_code": int(VerifyExitCode.INPUT_SCHEMA),
                    "checks": [],
                    "check_count": 0,
                }
            )

        try:
            bundle = EvidenceBundle.model_validate(bundle_dict)
        except Exception as exc:
            # spec §3：载入失败按违规层落稳定退出码（claim 等级违规=5、
            # copy_frozen 绑定缺失=4、其余 schema 类=2），不全部折叠成 2。
            logger.warning("Invalid bundle: %s", exc)
            return TaskOutput(
                output_values={
                    "status": "error",
                    "error": f"invalid bundle: {exc}",
                    "verification_ok": False,
                    "exit_code": int(map_load_error(exc)),
                    "checks": [],
                    "check_count": 0,
                }
            )

        ref_bundles = self._parse_reference_bundles(
            values.get("reference_bundles")
        )
        expected_digests = self._parse_expected_digests(
            values.get("expected_result_copy_digests")
        )

        result: VerifyResult = verify_bundle(
            bundle,
            reference_bundles=ref_bundles,
            expected_result_copy_digests=expected_digests,
        )

        return TaskOutput(
            output_values={
                "status": "completed",
                "verification_ok": result.ok,
                "exit_code": int(result.exit_code),
                "checks": [c.as_dict() for c in result.checks],
                "check_count": len(result.checks),
                "bundle_id": str(bundle.bundle_id),
            }
        )

    @staticmethod
    def _parse_reference_bundles(
        raw: dict[str, Any] | None,
    ) -> dict[str, EvidenceBundle] | None:
        """Parse raw reference bundle dicts into EvidenceBundle objects."""
        if not raw:
            return None
        bundles: dict[str, EvidenceBundle] = {}
        for key, bundle_dict in raw.items():
            try:
                bundles[key] = EvidenceBundle.model_validate(bundle_dict)
            except Exception as exc:
                logger.warning("Skipping invalid reference bundle %s: %s", key, exc)
        return bundles if bundles else None

    @staticmethod
    def _parse_expected_digests(
        raw: dict[str, str] | None,
    ) -> dict[str, str] | None:
        """Validate expected result copy digests."""
        if not raw:
            return None
        validated: dict[str, str] = {}
        for ref_id, digest in raw.items():
            if isinstance(digest, str) and digest.startswith("sha256:"):
                validated[ref_id] = digest
            else:
                logger.warning(
                    "Skipping invalid expected digest for ref %s", ref_id
                )
        return validated if validated else None
