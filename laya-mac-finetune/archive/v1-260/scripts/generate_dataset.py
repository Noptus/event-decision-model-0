#!/usr/bin/env python3
"""Generate deterministic bilingual Solace-style event-routing records."""
from __future__ import annotations

import copy
import random
from collections import Counter

from _shared import DATA_DIR, ROUTES, ROUTING_QUESTION, update_manifest, write_jsonl

SEED = 20261003
SPLIT_COUNTS = {
    "train": (27, 27, 27, 27, 26, 26),
    "validation": (7, 7, 7, 7, 6, 6),
    "test": (10, 10, 10, 10, 10, 10),
}
ROUTE_DOMAINS = {
    "order-processing": "orders",
    "payment-operations": "payments",
    "logistics": "shipments",
    "inventory-management": "stock",
    "customer-support": "care",
    "fraud-review": "risk",
}
SECONDARY = {
    "order-processing": "inventory-management",
    "payment-operations": "fraud-review",
    "logistics": "customer-support",
    "inventory-management": "order-processing",
    "customer-support": "logistics",
    "fraud-review": "payment-operations",
}
VARIANTS = {
    "order-processing": [
        ("OrderCreated", "order.created", "created"),
        ("OrderAmended", "order.amended", "amended"),
        ("OrderCancellationRequested", "order.cancellation.requested", "cancel-requested"),
    ],
    "payment-operations": [
        ("PaymentAuthorized", "payment.authorized", "authorized"),
        ("PaymentFailed", "payment.failed", "failed"),
        ("RefundInitiated", "refund.initiated", "refund-started"),
    ],
    "logistics": [
        ("ShipmentDispatched", "shipment.dispatched", "dispatched"),
        ("DeliveryDelayed", "delivery.delayed", "delayed"),
        ("CustomsHoldRecorded", "shipment.customs.hold", "customs-hold"),
    ],
    "inventory-management": [
        ("StockReserved", "inventory.reserved", "reserved"),
        ("StockLevelCorrected", "inventory.corrected", "corrected"),
        ("ReplenishmentRequested", "inventory.replenishment.requested", "replenish"),
    ],
    "customer-support": [
        ("SupportTicketOpened", "support.ticket.opened", "opened"),
        ("ComplaintEscalated", "customer.complaint.escalated", "escalated"),
        ("CallbackRequested", "support.callback.requested", "callback"),
    ],
    "fraud-review": [
        ("TransactionRiskAlert", "risk.transaction.alert", "transaction-alert"),
        ("AccountTakeoverSuspected", "security.account.takeover", "account-takeover"),
        ("VelocityRuleTriggered", "risk.velocity.triggered", "velocity-triggered"),
    ],
}
MESSAGES = {
    "order-processing": {
        "en": ["Customer submitted a new basket.", "Please amend the delivery quantity.", "Cancel before allocation."],
        "fr": ["Le client a validé un nouveau panier.", "Modifier la quantité avant préparation.", "Annuler avant l'allocation."],
    },
    "payment-operations": {
        "en": ["Card authorization completed.", "The issuer declined the payment.", "Refund was requested after settlement."],
        "fr": ["L'autorisation de carte est confirmée.", "La banque a refusé le paiement.", "Remboursement demandé après règlement."],
    },
    "logistics": {
        "en": ["Parcel handed to the carrier.", "Delivery missed its promised window.", "Customs requested supporting documents."],
        "fr": ["Le colis a été remis au transporteur.", "La livraison dépasse le créneau promis.", "La douane demande des justificatifs."],
    },
    "inventory-management": {
        "en": ["Units reserved at the warehouse.", "Cycle count corrected available stock.", "Replenishment threshold was crossed."],
        "fr": ["Unités réservées dans l'entrepôt.", "Le comptage a corrigé le stock disponible.", "Le seuil de réapprovisionnement est franchi."],
    },
    "customer-support": {
        "en": ["Customer opened a service ticket.", "Complaint needs a human response.", "Customer requested a callback."],
        "fr": ["Le client a ouvert un ticket d'assistance.", "La réclamation exige une réponse humaine.", "Le client demande à être rappelé."],
    },
    "fraud-review": {
        "en": ["Risk engine detected an unusual transaction.", "Login pattern suggests account takeover.", "Several attempts exceeded the velocity rule."],
        "fr": ["Le moteur de risque détecte une transaction inhabituelle.", "Les connexions suggèrent un piratage du compte.", "Plusieurs tentatives dépassent la règle de vélocité."],
    },
}


def payload_for(route: str, index: int, language: str, variant: int) -> dict:
    event_id = f"EVT-{index:06d}"
    message = MESSAGES[route][language][variant]
    if route == "order-processing":
        return {
            "event_id": event_id,
            "order_id": f"O-{80000 + index}",
            "customer_id": f"C-{1000 + index % 91}",
            "items": [{"sku": f"SKU-{100 + index % 37}", "quantity": 1 + index % 4}],
            "total": round(24.5 + (index % 23) * 7.25, 2),
            "currency": "EUR" if language == "fr" else "USD",
            "message": message,
        }
    if route == "payment-operations":
        return {
            "event_id": event_id,
            "transaction_id": f"TX-{500000 + index}",
            "order_id": f"O-{80000 + index}",
            "amount": round(18.0 + (index % 29) * 11.4, 2),
            "currency": "EUR" if language == "fr" else "USD",
            "processor_status": ("approved", "declined", "refund_pending")[variant],
            "message": message,
        }
    if route == "logistics":
        return {
            "event_id": event_id,
            "shipment_id": f"S-{300000 + index}",
            "carrier": ("NorthParcel", "BlueRail", "AtlasAir")[index % 3],
            "tracking_code": f"TRK{900000 + index}",
            "status": ("in_transit", "delayed", "customs_hold")[variant],
            "eta_hours": 8 + index % 72,
            "message": message,
        }
    if route == "inventory-management":
        return {
            "event_id": event_id,
            "sku": f"SKU-{100 + index % 37}",
            "warehouse": ("CDG-1", "YUL-2", "BOS-1")[index % 3],
            "on_hand": 10 + index % 90,
            "reserved": index % 13,
            "delta": (-3, 2, 24)[variant],
            "reason": ("allocation", "cycle_count", "reorder_point")[variant],
            "message": message,
        }
    if route == "customer-support":
        return {
            "event_id": event_id,
            "ticket_id": f"T-{700000 + index}",
            "customer_id": f"C-{1000 + index % 91}",
            "channel": ("email", "chat", "phone")[index % 3],
            "priority": ("normal", "high", "normal")[variant],
            "summary": message,
            "message": message,
        }
    return {
        "event_id": event_id,
        "alert_id": f"A-{600000 + index}",
        "transaction_id": f"TX-{500000 + index}",
        "risk_score": round(0.72 + (index % 25) / 100, 2),
        "signals": [
            ("new_device", "impossible_travel", "multiple_declines")[variant],
            "velocity_spike",
        ],
        "ip_country": ("GB", "CA", "FR")[index % 3],
        "message": message,
    }


def topic_for(split: str, route: str, verb: str, index: int, conflicting: bool) -> tuple[str, list[str]]:
    domain = ROUTE_DOMAINS[SECONDARY[route]] if conflicting else ROUTE_DOMAINS[route]
    region = ("eu", "na", "apac")[index % 3]
    environment = ("prod", "uat")[index % 2]
    tenant = ("acme", "globex", "initech")[index % 3]
    tags = []
    if split == "test" and index % 10 in {0, 4, 8}:
        # This hierarchy never appears in train/validation. The selected cycle positions cover
        # both languages and all three event variants across each route's held-out examples.
        topic = f"event/v2/{verb}/{tenant}/{region}/{domain}"
        tags.append("unseen_topic_pattern")
    elif split == "validation":
        topic = f"{tenant}/{region}/mesh/{domain}/{verb}/{environment}"
    elif index % 2:
        topic = f"mesh/{environment}/{domain}/{region}/{verb}/v1"
    else:
        topic = f"{tenant}/{environment}/{region}/{domain}/{verb}/v1"
    if conflicting:
        tags.append("topic_payload_conflict")
    return topic, tags


def target_distribution(route: str, secondary: str, ambiguous: bool, conflicting: bool) -> dict[str, float]:
    if ambiguous:
        values = {name: 0.0175 for name in ROUTES}
        values[route] = 0.68
        values[secondary] = 0.25
    elif conflicting:
        values = {name: 0.01 for name in ROUTES}
        values[route] = 0.86
        values[secondary] = 0.10
    else:
        values = {name: 0.016 for name in ROUTES}
        values[route] = 0.92
    return values


def make_record(split: str, route: str, class_index: int, serial: int) -> dict:
    variant_count = len(VARIANTS[route])
    variant = class_index % variant_count
    # Cycle language independently of the event variant: each complete variant block switches
    # language, so every route/event-type pair appears in both languages in every split.
    language = "fr" if (class_index // variant_count) % 2 else "en"
    schema_name, event_type, verb = VARIANTS[route][variant]
    # Two independently cycled positions make ambiguous examples bilingual even in the 6-row
    # validation classes and the 10-row test classes.
    ambiguous = class_index % 10 in {2, 3}
    conflicting = class_index % 10 == 6
    secondary = SECONDARY[route]
    topic, challenge_tags = topic_for(split, route, verb, class_index, conflicting)
    payload = payload_for(route, serial, language, variant)

    if class_index % 5 == 2:
        payload["message"] = payload.get("message", "") + (
            " FYI: replay after maintenance; correlation header may be stale."
            if language == "en"
            else " Info : rejeu après maintenance, l'en-tête de corrélation peut être ancien."
        )
        challenge_tags.append("noisy_text")
    if class_index % 7 == 1:
        payload.pop("message", None)
        payload.pop("tracking_code", None)
        challenge_tags.append("missing_optional_fields")
    if ambiguous:
        if route == "payment-operations":
            payload["risk_note"] = "new device, review only if authorization pattern repeats"
        elif route == "fraud-review":
            payload["settlement_status"] = "pending operations acknowledgement"
        elif route == "logistics":
            payload["customer_note"] = "requests an update if the delay exceeds one day"
        elif route == "customer-support":
            payload["delivery_reference"] = f"S-{300000 + serial}"
        elif route == "order-processing":
            payload["allocation_note"] = "stock reservation follows order acceptance"
        else:
            payload["order_reference"] = f"O-{80000 + serial}"
        challenge_tags.append("ambiguous")

    state = {
        "topic": topic,
        "schema_name": schema_name,
        "schema_version": f"{1 + class_index % 2}.0",
        "event_type": event_type,
        "payload": payload,
    }
    probabilities = target_distribution(route, secondary, ambiguous, conflicting)
    return {
        "id": f"mesh-{split[:3]}-{serial:04d}",
        "split": split,
        "language": language,
        "challenge_tags": sorted(challenge_tags),
        "state": state,
        "questions": copy.deepcopy(ROUTING_QUESTION),
        "gold": {
            "route": {
                "label": route,
                "probabilities": probabilities,
            }
        },
    }


def main() -> None:
    rng = random.Random(SEED)
    serial = 0
    summary = {}
    for split, counts in SPLIT_COUNTS.items():
        records = []
        for route, count in zip(ROUTES, counts):
            for class_index in range(count):
                serial += 1
                records.append(make_record(split, route, class_index, serial))
        rng.shuffle(records)
        write_jsonl(DATA_DIR / f"{split}.jsonl", records)
        labels = Counter(record["gold"]["route"]["label"] for record in records)
        languages = Counter(record["language"] for record in records)
        challenges = Counter(tag for record in records for tag in record["challenge_tags"])
        summary[split] = {
            "count": len(records),
            "routes": dict(labels),
            "languages": dict(languages),
            "challenges": dict(challenges),
        }
        print(f"{split}: {len(records)} events")
        print(f"  routes={dict(labels)}")
        print(f"  languages={dict(languages)} challenges={dict(challenges)}")
    update_manifest(dataset={"seed": SEED, "synthetic": True, "splits": summary})


if __name__ == "__main__":
    main()
