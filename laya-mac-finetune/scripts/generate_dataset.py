#!/usr/bin/env python3
"""Generate 10,000 deterministic, leakage-audited enterprise routing events."""
from __future__ import annotations

import copy
import hashlib
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from _shared import DATA_DIR, ROUTES, ROUTING_QUESTION, update_manifest, write_jsonl

SEED = 20261003
SPLIT_SCALE = {"train": 200, "validation": 25, "test": 25}
SPLIT_START = {
    "train": datetime(2026, 1, 1, tzinfo=timezone.utc),
    "validation": datetime(2026, 7, 1, tzinfo=timezone.utc),
    "test": datetime(2026, 9, 1, tzinfo=timezone.utc),
}

# Five units per domain and route. At scale 200/25/25 this yields exactly
# 8,000/1,000/1,000 records, 1,250 per domain, and 1,250 per route overall.
DOMAIN_ROUTE_UNITS = {
    "finance": {"finance-accounting": 4, "fraud-review": 1},
    "manufacturing": {"manufacturing-operations": 4, "fraud-review": 1},
    "payments": {"payment-operations": 4, "fraud-review": 1},
    "shipping": {"shipping-logistics": 4, "customer-crm": 1},
    "insurance": {"insurance-operations": 4, "fraud-review": 1},
    "it-ot": {"it-ot-operations": 4, "fraud-review": 1},
    "sap": {
        "finance-accounting": 1,
        "manufacturing-operations": 1,
        "payment-operations": 1,
        "shipping-logistics": 1,
        "it-ot-operations": 1,
    },
    "salesforce": {"customer-crm": 4, "insurance-operations": 1},
}
PORTFOLIOS = tuple(DOMAIN_ROUTE_UNITS)

SOURCES = {
    "finance": ("Treasury Hub", "Oracle Financials", "Billing Ledger"),
    "manufacturing": ("Plant MES", "Quality LIMS", "Maintenance EAM"),
    "payments": ("Card Gateway", "Bank Transfer Hub", "Merchant Acquirer"),
    "shipping": ("Carrier TMS", "Warehouse WMS", "Customs Gateway"),
    "insurance": ("Guidewire", "Policy Admin", "Claims Portal"),
    "it-ot": ("ServiceNow", "SCADA Historian", "Cloud Monitor"),
    "sap": ("SAP S/4HANA", "SAP EWM", "SAP Ariba"),
    "salesforce": ("Salesforce Sales Cloud", "Salesforce Service Cloud", "MuleSoft"),
}
ROUTE_TOPIC = {
    "finance-accounting": "ledger",
    "manufacturing-operations": "production",
    "payment-operations": "payment",
    "shipping-logistics": "freight",
    "insurance-operations": "policy",
    "it-ot-operations": "operations",
    "customer-crm": "customer",
    "fraud-review": "risk",
}
ENTITY_TYPES = {
    "finance-accounting": "financial-document",
    "manufacturing-operations": "production-order",
    "payment-operations": "transaction",
    "shipping-logistics": "shipment",
    "insurance-operations": "claim",
    "it-ot-operations": "managed-asset",
    "customer-crm": "customer-account",
    "fraud-review": "risk-case",
}
SECONDARY = {
    "finance-accounting": "payment-operations",
    "manufacturing-operations": "it-ot-operations",
    "payment-operations": "fraud-review",
    "shipping-logistics": "customer-crm",
    "insurance-operations": "fraud-review",
    "it-ot-operations": "manufacturing-operations",
    "customer-crm": "shipping-logistics",
    "fraud-review": "payment-operations",
}

# No family, schema, or event type is shared across splits.
SCENARIOS = {
    "finance-accounting": {
        "train": (
            ("journal-posting", "JournalEntryPosted", "finance.journal.posted", "journal-posted"),
            ("invoice-reconciliation", "InvoiceReconciled", "finance.invoice.reconciled", "invoice-reconciled"),
            ("receivable-aging", "ReceivableOverdue", "finance.receivable.overdue", "receivable-overdue"),
            ("tax-validation", "TaxDocumentValidated", "finance.tax.validated", "tax-validated"),
        ),
        "validation": (
            ("accrual-adjustment", "AccrualAdjusted", "finance.accrual.adjusted", "accrual-adjusted"),
            ("budget-control", "BudgetThresholdExceeded", "finance.budget.threshold", "budget-threshold"),
        ),
        "test": (
            ("treasury-position", "CashPositionChanged", "finance.cash.position.changed", "cash-position"),
            ("intercompany-break", "IntercompanyReconciliationFailed", "finance.intercompany.failed", "intercompany-failed"),
        ),
    },
    "manufacturing-operations": {
        "train": (
            ("production-release", "ProductionOrderReleased", "manufacturing.order.released", "order-released"),
            ("work-completion", "WorkOrderCompleted", "manufacturing.work.completed", "work-completed"),
            ("material-shortage", "MaterialShortageDetected", "manufacturing.material.shortage", "material-shortage"),
            ("quality-inspection", "QualityInspectionFailed", "manufacturing.quality.failed", "quality-failed"),
        ),
        "validation": (
            ("line-schedule", "ProductionScheduleChanged", "manufacturing.schedule.changed", "schedule-changed"),
            ("yield-variance", "YieldVarianceDetected", "manufacturing.yield.variance", "yield-variance"),
        ),
        "test": (
            ("bom-deviation", "BillOfMaterialsDeviation", "manufacturing.bom.deviation", "bom-deviation"),
            ("batch-hold", "ProductionBatchHeld", "manufacturing.batch.held", "batch-held"),
        ),
    },
    "payment-operations": {
        "train": (
            ("authorization", "PaymentAuthorized", "payment.authorized", "authorized"),
            ("capture", "PaymentCaptured", "payment.captured", "captured"),
            ("decline", "PaymentFailed", "payment.failed", "failed"),
            ("refund", "RefundInitiated", "payment.refund.initiated", "refund-started"),
        ),
        "validation": (
            ("settlement-break", "SettlementMismatch", "payment.settlement.mismatch", "settlement-mismatch"),
            ("payout-delay", "MerchantPayoutDelayed", "payment.payout.delayed", "payout-delayed"),
        ),
        "test": (
            ("chargeback-intake", "ChargebackReceived", "payment.chargeback.received", "chargeback-received"),
            ("reversal", "PaymentReversed", "payment.reversed", "reversed"),
        ),
    },
    "shipping-logistics": {
        "train": (
            ("dispatch", "ShipmentDispatched", "shipping.shipment.dispatched", "dispatched"),
            ("delivery-delay", "DeliveryDelayed", "shipping.delivery.delayed", "delivery-delayed"),
            ("customs-hold", "CustomsHoldRecorded", "shipping.customs.hold", "customs-hold"),
            ("freight-booking", "FreightBooked", "shipping.freight.booked", "freight-booked"),
        ),
        "validation": (
            ("delivery-exception", "DeliveryExceptionRaised", "shipping.delivery.exception", "delivery-exception"),
            ("carrier-capacity", "CarrierCapacityChanged", "shipping.capacity.changed", "capacity-changed"),
        ),
        "test": (
            ("proof-of-delivery", "ProofOfDeliveryReceived", "shipping.delivery.proved", "delivery-proved"),
            ("route-deviation", "TransportRouteDeviated", "shipping.route.deviated", "route-deviated"),
        ),
    },
    "insurance-operations": {
        "train": (
            ("claim-intake", "ClaimOpened", "insurance.claim.opened", "claim-opened"),
            ("policy-issue", "PolicyIssued", "insurance.policy.issued", "policy-issued"),
            ("premium-arrears", "PremiumOverdue", "insurance.premium.overdue", "premium-overdue"),
            ("underwriting-referral", "UnderwritingReferred", "insurance.underwriting.referred", "underwriting-referred"),
        ),
        "validation": (
            ("claim-document", "ClaimDocumentMissing", "insurance.claim.document.missing", "claim-document-missing"),
            ("policy-cancel", "PolicyCancellationRequested", "insurance.policy.cancel.requested", "policy-cancel"),
        ),
        "test": (
            ("catastrophe-notice", "CatastrophicLossReported", "insurance.catastrophe.reported", "catastrophe-reported"),
            ("claim-settlement", "ClaimSettlementApproved", "insurance.claim.settlement", "claim-settlement"),
        ),
    },
    "it-ot-operations": {
        "train": (
            ("service-incident", "ServiceIncidentOpened", "it.incident.opened", "incident-opened"),
            ("access-request", "AccessRequestRaised", "it.access.requested", "access-requested"),
            ("application-error", "ApplicationErrorObserved", "it.application.error", "application-error"),
            ("device-alert", "IndustrialDeviceAlert", "ot.device.alert", "device-alert"),
        ),
        "validation": (
            ("plc-warning", "PLCWarningRaised", "ot.plc.warning", "plc-warning"),
            ("scada-loss", "SCADAConnectionLost", "ot.scada.connection.lost", "scada-loss"),
        ),
        "test": (
            ("ransomware-signal", "RansomwareSignalDetected", "it.security.ransomware", "ransomware-signal"),
            ("safety-interlock", "SafetyInterlockTripped", "ot.safety.interlock", "interlock-tripped"),
        ),
    },
    "customer-crm": {
        "train": (
            ("lead-create", "LeadCreated", "crm.lead.created", "lead-created"),
            ("opportunity-stage", "OpportunityStageChanged", "crm.opportunity.stage", "opportunity-stage"),
            ("case-open", "CustomerCaseOpened", "crm.case.opened", "case-opened"),
            ("renewal-risk", "RenewalRiskRaised", "crm.renewal.risk", "renewal-risk"),
        ),
        "validation": (
            ("account-merge", "CustomerAccountsMerged", "crm.account.merged", "account-merged"),
            ("contract-renewal", "ContractRenewalRequested", "crm.contract.renewal", "contract-renewal"),
        ),
        "test": (
            ("customer-escalation", "CustomerEscalationRaised", "crm.customer.escalated", "customer-escalated"),
            ("consent-preference", "ConsentPreferenceChanged", "crm.consent.changed", "consent-changed"),
        ),
    },
    "fraud-review": {
        "train": (
            ("transaction-anomaly", "TransactionAnomalyDetected", "risk.transaction.anomaly", "transaction-anomaly"),
            ("account-takeover", "AccountTakeoverSuspected", "risk.account.takeover", "account-takeover"),
            ("identity-mismatch", "IdentityMismatchDetected", "risk.identity.mismatch", "identity-mismatch"),
            ("velocity-rule", "VelocityRuleTriggered", "risk.velocity.triggered", "velocity-triggered"),
        ),
        "validation": (
            ("merchant-abuse", "MerchantAbuseSuspected", "risk.merchant.abuse", "merchant-abuse"),
            ("synthetic-identity", "SyntheticIdentityAlert", "risk.synthetic.identity", "synthetic-identity"),
        ),
        "test": (
            ("mule-account", "MuleAccountSuspected", "risk.mule.account", "mule-account"),
            ("suspicious-claim", "SuspiciousClaimDetected", "risk.claim.suspicious", "suspicious-claim"),
        ),
    },
}

MESSAGES = {
    "finance-accounting": ("Reconcile this financial record.", "Rapprocher cette écriture financière."),
    "manufacturing-operations": ("Update the plant production plan.", "Mettre à jour le plan de production."),
    "payment-operations": ("Process this payment lifecycle event.", "Traiter cet événement du cycle de paiement."),
    "shipping-logistics": ("Coordinate the carrier movement.", "Coordonner le mouvement du transporteur."),
    "insurance-operations": ("Process this policy or claim event.", "Traiter cet événement de police ou de sinistre."),
    "it-ot-operations": ("Restore the affected digital or industrial service.", "Rétablir le service numérique ou industriel affecté."),
    "customer-crm": ("Update the customer relationship workflow.", "Mettre à jour le parcours de relation client."),
    "fraud-review": ("Investigate the risk signals before execution.", "Examiner les signaux de risque avant exécution."),
}

# Each pair family is genuinely different in each split; names are not split suffixes over one template.
PAIR_SCENARIOS = {
    "train": (
        ("payments", "payment-operations", "fraud-review", "device_trust", "trusted", "compromised", "PaymentAuthorizationRiskScored", "payment.authorization.risk-scored"),
        ("sap", "manufacturing-operations", "it-ot-operations", "safety_state", "normal", "trip", "MachineConditionAssessed", "plant.machine.condition-assessed"),
        ("shipping", "shipping-logistics", "customer-crm", "customer_response", "notify", "formal_dispute", "DeliveryOutcomeAssessed", "delivery.outcome.assessed"),
        ("insurance", "insurance-operations", "fraud-review", "claim_signal", "covered_loss", "fabricated_evidence", "ClaimIntakeAssessed", "insurance.claim.assessed"),
        ("finance", "finance-accounting", "fraud-review", "invoice_signal", "posting_variance", "supplier_identity_mismatch", "InvoiceControlAssessed", "finance.invoice.control-assessed"),
    ),
    "validation": (
        ("payments", "payment-operations", "fraud-review", "identity_match", True, False, "RefundRiskScreened", "payment.refund.risk-screened"),
        ("sap", "manufacturing-operations", "it-ot-operations", "controller_integrity", "healthy", "suspected_intrusion", "ProductionLineDeviationAssessed", "plant.line.deviation-assessed"),
        ("shipping", "shipping-logistics", "customer-crm", "requested_action", "reroute_parcel", "open_customer_case", "ParcelExceptionAssessed", "delivery.exception.assessed"),
        ("insurance", "insurance-operations", "fraud-review", "evidence_check", "consistent", "tampered", "CoverageEvidenceAssessed", "insurance.coverage.evidence-assessed"),
        ("finance", "finance-accounting", "fraud-review", "beneficiary_match", True, False, "TreasuryBeneficiaryScreened", "finance.beneficiary.screened"),
    ),
    "test": (
        ("payments", "payment-operations", "fraud-review", "beneficiary_verified", True, False, "MerchantPayoutScreened", "payment.payout.screened"),
        ("sap", "manufacturing-operations", "it-ot-operations", "interlock_override", False, True, "PlantRestartAssessment", "plant.restart.assessed"),
        ("shipping", "shipping-logistics", "customer-crm", "delivery_evidence", "carrier_scan_missing", "customer_disputes_receipt", "ProofOfDeliveryDisputeAssessed", "delivery.proof-dispute.assessed"),
        ("insurance", "insurance-operations", "fraud-review", "claim_pattern", "covered_event", "organized_ring", "ClaimPatternAssessed", "insurance.claim.pattern-assessed"),
        ("finance", "finance-accounting", "fraud-review", "transfer_control", "ledger_exception", "account_takeover_signal", "IntercompanyTransferScreened", "finance.transfer.screened"),
    ),
}

OOD_SCENARIOS = {
    "validation": (
        ("sustainability-certificate", "CarbonCertificateRetired", "sustainability.certificate.retired", "carbon-certificate"),
        ("facilities-occupancy", "BuildingOccupancyObserved", "facilities.occupancy.observed", "occupancy-observed"),
    ),
    "test": (
        ("drone-survey", "DroneSurveyCompleted", "geospatial.drone.surveyed", "drone-survey"),
        ("weather-threshold", "WeatherThresholdObserved", "environment.weather.threshold", "weather-threshold"),
    ),
}


def stable_id(*parts: object, length: int = 12) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:length]


def source_for(domain: str, serial: int) -> dict[str, str]:
    systems = SOURCES[domain]
    return {"system": systems[serial % len(systems)], "instance": f"node-{serial % 97:02d}"}


def payload_for(route: str, serial: int, description: str, variant: int) -> dict[str, Any]:
    common: dict[str, Any] = {"event_id": f"e{serial:05d}", "description": description}
    if route == "finance-accounting":
        common.update(document_id=f"D{100000 + serial}", amount=500 + serial % 7000, currency="EUR", company=f"C{serial % 17:02d}")
    elif route == "manufacturing-operations":
        common.update(plant=f"P{serial % 9:02d}", work_order=f"W{200000 + serial}", material=f"M{serial % 211:03d}", quantity=5 + serial % 80, quality=("accepted", "hold", "inspect")[variant % 3])
    elif route == "payment-operations":
        common.update(transaction_id=f"T{300000 + serial}", amount=12 + serial % 1300, currency=("EUR", "USD", "GBP")[serial % 3], status=("authorized", "captured", "declined", "refund_pending")[variant % 4], risk_score=round(0.08 + (serial % 38) / 100, 2))
    elif route == "shipping-logistics":
        common.update(shipment_id=f"S{400000 + serial}", carrier=("Atlas", "NorthParcel", "BlueRail")[serial % 3], status=("booked", "transit", "delayed", "customs_hold")[variant % 4], eta_hours=4 + serial % 96)
    elif route == "insurance-operations":
        common.update(policy_id=f"P{500000 + serial}", claim_id=f"C{600000 + serial}", line=("property", "motor", "travel")[serial % 3], claim_amount=250 + serial % 12000, coverage=("active", "pending", "verified")[variant % 3])
    elif route == "it-ot-operations":
        common.update(asset_id=f"A{7000 + serial % 401}", site=f"S{1 + serial % 14:02d}", severity=("warning", "major", "critical")[variant % 3], metric=("latency", "temperature", "packet_loss", "vibration")[variant % 4], value=40 + serial % 91)
    elif route == "customer-crm":
        common.update(account_id=f"A{800000 + serial}", case_id=f"C{900000 + serial}", channel=("email", "chat", "phone")[serial % 3], priority=("normal", "high", "urgent")[variant % 3], action="follow_up")
    else:
        common.update(alert_id=f"A{950000 + serial}", transaction_id=f"T{300000 + serial}", risk_score=round(0.72 + (serial % 27) / 100, 2), signals=[("new_device", "impossible_travel", "velocity_spike", "identity_mismatch")[variant % 4], "behavior_change"])
    return common


def target_distribution(primary: str, secondary: str, kind: str) -> dict[str, float]:
    primary_mass, secondary_mass = {
        "clear": (0.94, 0.0),
        "conflict": (0.82, 0.12),
        "ambiguous": (0.62, 0.28),
        "ood": (0.22, 0.18),
    }[kind]
    remainder = (1.0 - primary_mass - secondary_mass) / (len(ROUTES) - (2 if secondary_mass else 1))
    result = {route: remainder for route in ROUTES}
    result[primary] = primary_mass
    if secondary_mass:
        result[secondary] = secondary_mass
    result[primary] += 1.0 - sum(result.values())
    return result


def topic_for(split: str, domain: str, operation: str, index: int, conflict_route: str | None) -> tuple[str, str]:
    region = ("eu", "na", "apac", "latam")[index % 4]
    domain_segment = domain.replace("-", "")
    operation_segment = ROUTE_TOPIC[conflict_route] if conflict_route else operation
    version = 1 + (index * 5 + len(operation)) % 3
    if split == "train":
        if index % 2:
            return f"edm0/pilot/events/{region}/{domain_segment}/{operation_segment}/v{version}", "region-domain-operation"
        return f"edm0/pilot/events/{domain_segment}/{region}/{operation_segment}/v{version}", "domain-region-operation"
    if split == "validation":
        return f"edm0/pilot/events/{domain_segment}/{operation_segment}/{region}/v{version}", "domain-operation-region"
    return f"edm0/pilot/events/v{version}/{operation_segment}/{region}/{domain_segment}", "version-operation-region-domain"


def make_regular_record(split: str, domain: str, route: str, route_index: int, class_index: int, serial: int) -> dict[str, Any]:
    scenarios = SCENARIOS[route][split]
    variant = class_index % len(scenarios)
    family, schema_name, event_type, operation = scenarios[variant]
    language = "fr" if ((class_index // len(scenarios)) + route_index + PORTFOLIOS.index(domain)) % 2 else "en"
    secondary = SECONDARY[route]
    ambiguous = class_index % 20 in {14, 19}
    conflict = class_index % 17 == 12
    ood = split != "train" and class_index % 25 == 12
    kind = "ood" if ood else "ambiguous" if ambiguous else "conflict" if conflict else "clear"

    if ood:
        ood_index = class_index // 25
        family, schema_name, event_type, operation = OOD_SCENARIOS[split][ood_index % 2]
        language = "fr" if ((ood_index // 2) + route_index) % 2 else "en"
    topic, topic_pattern = topic_for(split, domain, operation, class_index, secondary if conflict and not ood else None)
    description = MESSAGES[route][1 if language == "fr" else 0]
    payload = payload_for(route, serial, description, variant)
    tags = []
    if ambiguous:
        tags.append("ambiguous")
        payload["secondary_signal"] = MESSAGES[secondary][1 if language == "fr" else 0]
    if conflict and not ood:
        tags.append("topic_payload_conflict")
    if ood:
        tags.append("out_of_distribution")
        payload = {
            "event_id": f"e{serial:05d}",
            "observation": (
                "Signal métier inédit; propriétaire inconnu."
                if language == "fr"
                else "Novel business signal; owner is unknown."
            ),
            "measurement": round(10 + (serial % 401) / 10, 1),
            "unit": ("ppm", "count", "index")[serial % 3],
        }
    if class_index % 13 == 11:
        tags.append("missing_optional_fields")
        payload.pop("description", None)
    if class_index % 11 == 10:
        tags.append("noisy_text")
        payload["note"] = "rejeu maintenance; en-tête ancien" if language == "fr" else "maintenance replay; stale header"
    if split == "test":
        tags.append("unseen_topic_pattern")
    if class_index % 19 == 13:
        tags.append("schema_evolution")

    entity_group = f"g{stable_id(split, domain, route, class_index // 4)}"
    timestamp = SPLIT_START[split] + timedelta(minutes=serial * 3)
    state = {
        "topic": topic,
        "schema_name": schema_name,
        "schema_version": f"{version_for(class_index)}.0",
        "event_type": event_type,
        "event_timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "source_system": source_for(domain, serial)["system"],
        "correlation_id": f"c{serial:05d}",
        "entity": {"type": "observation" if ood else ENTITY_TYPES[route], "id": entity_group},
        "payload": payload,
    }
    rationale = (
        "Unknown event family; provisional owner only and manual review required."
        if ood
        else f"The {schema_name} business semantics require the {route} capability."
    )
    return {
        "id": f"edm0-{stable_id(split, domain, route, class_index, serial)}",
        "split": split,
        "domain": domain,
        "language": language,
        "scenario_family": family,
        "template_id": f"{family}:{language}:{domain}",
        "entity_group": entity_group,
        "challenge_tags": sorted(tags),
        "annotation": {
            "rationale": rationale,
            "ambiguous": ambiguous or ood,
            "review_needed": ambiguous or ood,
            "secondary_route": secondary if ambiguous or ood or conflict else None,
        },
        "state": state,
        "questions": copy.deepcopy(ROUTING_QUESTION),
        "gold": {"route": {"label": route, "probabilities": target_distribution(route, secondary, kind)}},
        "generation": {"kind": kind, "topic_pattern": topic_pattern},
    }


def version_for(index: int) -> int:
    return 1 + (index * 7 + 3) % 3


def paired_state(split: str, domain: str, pair_index: int, side: int, field: str, value: Any, schema_name: str, event_type: str) -> tuple[dict[str, Any], str, str]:
    language = "fr" if pair_index % 2 else "en"
    entity_group = f"g{stable_id(split, domain, 'pair', pair_index)}"
    timestamp = SPLIT_START[split] + timedelta(minutes=100 + pair_index)
    source = source_for(domain, 50000 + pair_index)
    correlation = f"c{stable_id(split, domain, pair_index, side, length=10)}"
    event_id = f"e{stable_id('pair', split, domain, pair_index, side, length=10)}"
    message = (
        "Un seul fait métier validé détermine l'équipe responsable."
        if language == "fr"
        else "One verified business fact determines the responsible team."
    )
    state = {
        "topic": f"edm0/pilot/events/{domain.replace('-', '')}/decision/{event_type.replace('.', '-')}/v2",
        "schema_name": schema_name,
        "schema_version": "2.0",
        "event_type": event_type,
        "event_timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "source_system": source["system"],
        "correlation_id": correlation,
        "entity": {"type": "decision-case", "id": entity_group},
        "payload": {
            "event_id": event_id,
            "case_reference": f"P{stable_id(split, domain, pair_index, length=8)}",
            "amount": 100 + pair_index * 17,
            "currency": "EUR",
            "description": message,
            field: value,
        },
    }
    return state, language, entity_group


def apply_counterfactual_pairs(split: str, by_cell: dict[tuple[str, str], list[dict[str, Any]]]) -> None:
    for boundary_index, scenario in enumerate(PAIR_SCENARIOS[split]):
        domain, route_a, route_b, field, value_a, value_b, schema_name, event_type = scenario
        family = {
            "train": ("authorization-trust", "machine-safety-state", "delivery-response", "claim-evidence", "invoice-identity"),
            "validation": ("refund-identity", "controller-integrity", "parcel-action", "coverage-integrity", "beneficiary-integrity"),
            "test": ("payout-beneficiary", "restart-interlock", "delivery-proof", "claim-pattern", "transfer-control"),
        }[split][boundary_index]
        for pair_index in range(10):
            pair_id = f"pair-{stable_id(split, boundary_index, pair_index)}"
            for side, (route, value) in enumerate(((route_a, value_a), (route_b, value_b))):
                record = by_cell[(domain, route)][pair_index]
                serial = int(stable_id(split, domain, pair_index, side, length=7), 16)
                state, language, entity_group = paired_state(
                    split, domain, pair_index, side, field, value, schema_name, event_type
                )
                secondary = route_b if route == route_a else route_a
                record.update(
                    {
                        "language": language,
                        "scenario_family": family,
                        "template_id": f"{family}:{language}:{domain}",
                        "entity_group": entity_group,
                        "challenge_tags": ["counterfactual_pair", "hard_boundary"],
                        "annotation": {
                            "rationale": f"Only {field} changes the primary destination from the paired event.",
                            "ambiguous": False,
                            "review_needed": False,
                            "secondary_route": secondary,
                            "counterfactual_pair_id": pair_id,
                            "changed_field": f"payload.{field}",
                        },
                        "state": state,
                        "gold": {"route": {"label": route, "probabilities": target_distribution(route, secondary, "clear")}},
                        "generation": {"kind": "counterfactual", "topic_pattern": "paired-neutral"},
                    }
                )


def build_split(split: str, scale: int, serial_start: int) -> tuple[list[dict[str, Any]], int]:
    by_cell: dict[tuple[str, str], list[dict[str, Any]]] = {}
    route_indices = Counter()
    serial = serial_start
    for domain in PORTFOLIOS:
        for route, units in DOMAIN_ROUTE_UNITS[domain].items():
            records = []
            for _ in range(units * scale):
                serial += 1
                class_index = route_indices[route]
                route_indices[route] += 1
                records.append(make_regular_record(split, domain, route, ROUTES.index(route), class_index, serial))
            by_cell[(domain, route)] = records
    apply_counterfactual_pairs(split, by_cell)
    records = [record for cell in by_cell.values() for record in cell]
    random.Random(SEED + len(split)).shuffle(records)
    return records, serial


def main() -> None:
    serial = 0
    summary = {}
    for split, scale in SPLIT_SCALE.items():
        records, serial = build_split(split, scale, serial)
        write_jsonl(DATA_DIR / f"{split}.jsonl", records)
        routes = Counter(record["gold"]["route"]["label"] for record in records)
        domains = Counter(record["domain"] for record in records)
        languages = Counter(record["language"] for record in records)
        challenges = Counter(tag for record in records for tag in record["challenge_tags"])
        summary[split] = {
            "count": len(records),
            "routes": dict(routes),
            "domains": dict(domains),
            "languages": dict(languages),
            "challenges": dict(challenges),
        }
        print(f"{split}: {len(records)} events")
        print(f"  routes={dict(routes)}")
        print(f"  domains={dict(domains)}")
        print(f"  languages={dict(languages)} challenges={dict(challenges)}")
    update_manifest(dataset={"version": "edm0-enterprise-v2", "seed": SEED, "synthetic": True, "splits": summary})


if __name__ == "__main__":
    main()
