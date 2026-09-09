"""四类 inspection 的统一准入管线（F-R9-02/T-P3.4）。

inspect 动作的真实安全事实来源：对 provider content 执行 schema/供应链/网络/
contracts 四类检查并产出 JSON 可序列化的合并报告，由 API 面持久化到
CapabilityVersion.metadata；blocking finding 存在时阻断 tested 转移。
此前 inspection 模块生产调用为零（F-R9-02）——「已 inspected」不构成任何安全事实。
"""

from __future__ import annotations

from typing import Any

from zhiwei.capabilities.domain import ProviderVersion
from zhiwei.capabilities.inspection.contracts import (
    ContractReport,
    ContractViolation,
    validate_effect_unknown,
)
from zhiwei.capabilities.inspection.network import check_url_safety
from zhiwei.capabilities.inspection.schema import (
    InspectionFinding,
    InspectionReport,
    Severity,
    scan_prompt_injection,
    scan_secret_exfiltration,
    validate_tool_args,
)
from zhiwei.capabilities.inspection.supply_chain import (
    SBOMEntry,
    SupplyChainReport,
    validate_sbom,
)

_ALLOWED_EFFECTS = frozenset({"apply", "preview", "dry_run"})


def _provider_tools(content: dict[str, Any]) -> list[Any]:
    tools = content.get("tools")
    return tools if isinstance(tools, list) else []


def run_admission_inspections(provider: ProviderVersion) -> dict[str, Any]:
    """对 provider content 执行四类检查，返回 JSON 可序列化合并报告。

    报告结构：{"provider_version_id", "content_digest", "passed",
    "checks": {schema|supply_chain|network|contracts: [report dumps]},
    "blocking": [finding messages]}。
    """
    checks: dict[str, list[dict[str, Any]]] = {
        "schema": [],
        "supply_chain": [],
        "network": [],
        "contracts": [],
    }
    collected: dict[str, list[Any]] = {
        "schema": [],
        "supply_chain": [],
        "network": [],
        "contracts": [],
    }

    content = provider.content if isinstance(provider.content, dict) else {}

    # ① schema 面：工具 schema 结构校验 + 描述注入/秘密切描
    for idx, tool in enumerate(_provider_tools(content)):
        if not isinstance(tool, dict):
            # 畸形条目不是「跳过」，是 blocking finding——否则恶意 provider 可用
            # 畸形结构条目规避描述扫描（fail closed）。
            collected["schema"].append(
                InspectionReport().add(
                    InspectionFinding(
                        check="malformed_tool_entry",
                        severity=Severity.HIGH,
                        message=f"tools[{idx}] is not a JSON object; refusing silent skip",
                        path=f"tools[{idx}]",
                    )
                )
            )
            continue
        name = str(tool.get("name", idx))
        collected["schema"].append(
            validate_tool_args(tool.get("inputSchema") or {}, tool_name=name)
        )
        description = tool.get("description", "")
        description_text = description if isinstance(description, str) else str(description)
        collected["schema"].append(
            scan_prompt_injection(description_text, field=f"tools[{idx}].description")
        )
        collected["schema"].append(
            scan_secret_exfiltration(description_text, field=f"tools[{idx}].description")
        )

    # ② supply_chain 面：SBOM 许可证/漏洞（如声明）
    sbom_entries = content.get("sbom")
    if isinstance(sbom_entries, list) and sbom_entries:
        parsed: list[SBOMEntry] = []
        for idx, entry in enumerate(sbom_entries):
            if isinstance(entry, dict):
                parsed.append(
                    SBOMEntry(
                        name=str(entry.get("name", "")),
                        version=str(entry.get("version", "")),
                        supplier=str(entry.get("supplier", "")),
                        license=str(entry.get("license", "")),
                        purl=str(entry.get("purl", "")),
                        checksum=str(entry.get("checksum", "")),
                    )
                )
            else:
                collected["supply_chain"].append(
                    SupplyChainReport().add_finding(
                        InspectionFinding(
                            check="malformed_sbom_entry",
                            severity=Severity.HIGH,
                            message=f"sbom[{idx}] is not a JSON object; refusing silent skip",
                            path=f"sbom[{idx}]",
                        )
                    )
                )
        if parsed:
            collected["supply_chain"].append(validate_sbom(parsed))

    # ③ network 面：source_url 的 SSRF/协议/端口检查
    if provider.source_url:
        collected["network"].append(check_url_safety(provider.source_url))

    # ④ contracts 面：effect 声明词表（effect_unknown → S2 重试门的输入）
    effects = content.get("effects")
    if isinstance(effects, list):
        for idx, effect in enumerate(effects):
            if isinstance(effect, str):
                collected["contracts"].append(
                    validate_effect_unknown(effect=effect, allowed_effects=_ALLOWED_EFFECTS)
                )
            else:
                collected["contracts"].append(
                    ContractReport().add_violation(
                        ContractViolation(
                            rule="malformed_effect_entry",
                            severity=Severity.HIGH,
                            message=f"effects[{idx}] is not a string; refusing silent skip",
                            context={"index": idx},
                        )
                    )
                )

    blocking: list[str] = []
    for group, reports in collected.items():
        for report in reports:
            for finding in report.findings:
                if finding.is_blocking():
                    blocking.append(finding.message)
            checks[group].append(report.model_dump())

    return {
        "provider_version_id": str(provider.id),
        "content_digest": provider.content_digest,
        "passed": not blocking,
        "checks": checks,
        "blocking": blocking,
    }
