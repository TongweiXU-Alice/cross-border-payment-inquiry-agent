"""Typed domain models for the synthetic payment-inquiry workflow."""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


SUPPORTED_DECISIONS = {
    "ANSWER",
    "CLARIFY",
    "PARTIAL_RESOLUTION",
    "ESCALATE",
}


@dataclass
class Inquiry:
    text: str
    intents: List[str] = field(default_factory=list)
    fl_id: Optional[str] = None
    # ``amount`` remains for backwards compatibility and represents an explicitly
    # stated original/sent amount. New code should use the role-specific fields.
    amount: Optional[float] = None
    original_amount: Optional[float] = None
    received_amount: Optional[float] = None
    currency: Optional[str] = None
    bank: Optional[str] = None
    date: Optional[str] = None
    document_type: Optional[str] = None
    reason_code: Optional[str] = None
    customer_claims: List[str] = field(default_factory=list)
    understanding_mode: str = "deterministic"


@dataclass
class ToolTraceEvent:
    """Minimal audit event; intentionally excludes raw database rows."""

    step: int
    kind: str
    name: str
    purpose: str
    args: Dict[str, Any] = field(default_factory=dict)
    status: str = "SUCCESS"
    source: str = ""
    record_count: int = 0
    summary: str = ""
    duration_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceRecord:
    key: str
    value: Any
    source: str
    observed_at: Optional[str] = None
    status: str = "VERIFIED"
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceCheck:
    check: str
    status: str
    detail: str
    evidence_keys: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HumanReviewPacket:
    """A local review hand-off only; it never sends or persists an action."""

    required: bool
    reason_codes: List[str] = field(default_factory=list)
    target_queues: List[str] = field(default_factory=list)
    status: str = "NOT_REQUIRED"
    draft_only: bool = True
    external_action: str = "NONE"
    persistence: str = "NOT_PERSISTED"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentResult:
    inquiry: Dict[str, Any]
    matched_transactions: List[Dict[str, Any]] = field(default_factory=list)
    tool_trace: List[Dict[str, Any]] = field(default_factory=list)
    verified_facts: Dict[str, Any] = field(default_factory=dict)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    evidence_checks: List[Dict[str, Any]] = field(default_factory=list)
    unsupported: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    decision: str = "CLARIFY"
    escalation: List[str] = field(default_factory=list)
    human_review_required: bool = False
    review_packet: Optional[Dict[str, Any]] = None
    understanding_mode: str = "deterministic"
    draft_response: str = ""

    def add_trace(self, event: ToolTraceEvent) -> None:
        self.tool_trace.append(event.to_dict())

    def add_evidence(self, record: EvidenceRecord) -> None:
        self.evidence.append(record.to_dict())

    def add_check(self, check: EvidenceCheck) -> None:
        self.evidence_checks.append(check.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
