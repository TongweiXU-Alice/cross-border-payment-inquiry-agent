"""Evidence-gated workflow for the synthetic cross-border payment demo."""

from dataclasses import asdict
from decimal import Decimal
from time import perf_counter
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .models import (
    AgentResult,
    EvidenceCheck,
    EvidenceRecord,
    HumanReviewPacket,
    SUPPORTED_DECISIONS,
    ToolTraceEvent,
)
from .understanding import understand
from tools.db import DatabaseError
from tools.payment_tools import (
    DEFAULT_DATA_AS_OF,
    calculate_amount_difference,
    detect_status_conflict,
    get_bank_status,
    get_document,
    get_fee_info,
    get_refund_info,
    money_as_float,
    processing_age_calendar_days,
    quantize_money,
    search_customer_history,
    search_sop,
    search_transaction,
    search_transaction_by_fields,
)


def _dedupe(values: Sequence[str]) -> List[str]:
    return list(dict.fromkeys(values))


def _record_count(output: Any) -> int:
    if output is None:
        return 0
    if isinstance(output, dict):
        return 1
    if isinstance(output, (list, tuple, set)):
        return len(output)
    return 1


def _trace_event(
    result: AgentResult,
    *,
    kind: str,
    name: str,
    purpose: str,
    args: Optional[Dict[str, Any]] = None,
    status: str = "SUCCESS",
    source: str = "rule_engine",
    record_count: int = 0,
    summary: str = "",
    duration_ms: int = 0,
) -> None:
    result.add_trace(ToolTraceEvent(
        step=len(result.tool_trace) + 1,
        kind=kind,
        name=name,
        purpose=purpose,
        args=args or {},
        status=status,
        source=source,
        record_count=record_count,
        summary=summary,
        duration_ms=duration_ms,
    ))


def _call_tool(
    result: AgentResult,
    name: str,
    purpose: str,
    source: str,
    args: Dict[str, Any],
    function: Callable[..., Any],
    *function_args: Any,
    **function_kwargs: Any,
) -> Tuple[Any, Optional[DatabaseError]]:
    started = perf_counter()
    try:
        output = function(*function_args, **function_kwargs)
    except DatabaseError as exc:
        duration = int((perf_counter() - started) * 1000)
        _trace_event(
            result,
            kind="data_access",
            name=name,
            purpose=purpose,
            args=args,
            status="ERROR",
            source=source,
            record_count=0,
            summary="Synthetic data source unavailable or incompatible.",
            duration_ms=duration,
        )
        return None, exc
    duration = int((perf_counter() - started) * 1000)
    count = _record_count(output)
    _trace_event(
        result,
        kind="data_access",
        name=name,
        purpose=purpose,
        args=args,
        status="SUCCESS" if count else "EMPTY",
        source=source,
        record_count=count,
        summary="{} matching record(s).".format(count),
        duration_ms=duration,
    )
    return output, None


def _validation_trace(
    result: AgentResult,
    name: str,
    purpose: str,
    status: str,
    summary: str,
    args: Optional[Dict[str, Any]] = None,
) -> None:
    _trace_event(
        result,
        kind="validation",
        name=name,
        purpose=purpose,
        args=args,
        status=status,
        source="rule_engine",
        record_count=1,
        summary=summary,
        duration_ms=0,
    )


def _fact(
    result: AgentResult,
    key: str,
    value: Any,
    source: str,
    observed_at: Optional[str] = None,
    status: str = "VERIFIED",
    note: str = "",
) -> None:
    result.verified_facts[key] = value
    result.add_evidence(EvidenceRecord(
        key=key,
        value=value,
        source=source,
        observed_at=observed_at,
        status=status,
        note=note,
    ))


def _check(
    result: AgentResult,
    check: str,
    status: str,
    detail: str,
    evidence_keys: Optional[List[str]] = None,
) -> None:
    result.add_check(EvidenceCheck(
        check=check,
        status=status,
        detail=detail,
        evidence_keys=evidence_keys or [],
    ))


def _public_transaction(transaction: Dict[str, Any]) -> Dict[str, Any]:
    keys = [
        "transaction_id", "fl_id", "bank", "currency", "original_amount",
        "internal_status", "created_at",
    ]
    return {key: transaction.get(key) for key in keys}


def _set_review_packet(result: AgentResult) -> None:
    reasons = _dedupe(result.conflicts + result.unsupported)
    required = result.decision != "ANSWER"
    result.human_review_required = required
    result.review_packet = HumanReviewPacket(
        required=required,
        reason_codes=reasons,
        target_queues=list(result.escalation),
        status="PENDING_REVIEW" if required else "NOT_REQUIRED",
        draft_only=True,
        external_action="NONE",
        persistence="NOT_PERSISTED",
    ).to_dict()


def _finalize(
    result: AgentResult,
    decision: str,
    draft: Optional[str] = None,
) -> AgentResult:
    if decision not in SUPPORTED_DECISIONS:
        raise ValueError("Unsupported decision: {}".format(decision))
    result.decision = decision
    result.unsupported = _dedupe(result.unsupported)
    result.conflicts = _dedupe(result.conflicts)
    result.escalation = _dedupe(result.escalation)
    if draft is not None:
        result.draft_response = draft
    elif not result.draft_response:
        result.draft_response = build_draft(result)
    _set_review_packet(result)
    return result


def _data_source_failure(result: AgentResult) -> AgentResult:
    result.unsupported.append("synthetic_data_source_unavailable")
    result.escalation.append("OPERATIONS_REVIEW")
    _check(
        result,
        "data_source_health",
        "FAIL",
        "The local synthetic SQLite source could not be read safely.",
    )
    return _finalize(
        result,
        "ESCALATE",
        "本地合成数据源当前不可用或结构不兼容，未生成交易结论。建议由项目维护者检查数据库后重试。",
    )


def _amount_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    return quantize_money(left) == quantize_money(right)


def _classify_customer_claims(
    result: AgentResult,
    claims: Sequence[str],
    transaction: Dict[str, Any],
    bank: Optional[Dict[str, Any]],
) -> None:
    assessments: List[Dict[str, str]] = []
    for claim in claims:
        classification = "UNVERIFIED"
        detail = "No authoritative evidence was available for this customer statement."
        if claim == "internal_status_completed":
            classification = "SUPPORTED" if transaction.get("internal_status") == "COMPLETED" else "DISPUTED"
            detail = "Compared the customer statement with the internal transaction status."
        elif claim == "customer_attributes_difference_to_platform_fee":
            difference = result.verified_facts.get("amount_difference")
            verified_fee = result.verified_facts.get("verified_fee")
            if difference is not None and verified_fee is not None:
                classification = "SUPPORTED" if _amount_equal(difference, verified_fee) else "DISPUTED"
                detail = "Compared the claimed attribution with verified fee records."
        elif claim == "customer_claims_refunded":
            if transaction.get("internal_status") == "REFUNDED" or (bank and bank.get("bank_status") == "RETURNED"):
                classification = "SUPPORTED"
            elif bank:
                classification = "DISPUTED"
            detail = "Compared the refund statement with internal and bank-side statuses."
        elif claim == "bank_record_received_amount":
            stated = result.inquiry.get("received_amount")
            recorded = transaction.get("received_amount")
            if stated is not None and recorded is not None:
                classification = "SUPPORTED" if _amount_equal(stated, recorded) else "DISPUTED"
            detail = "Compared the stated received amount with the synthetic transaction record."

        assessments.append({"claim": claim, "classification": classification})
        result.add_evidence(EvidenceRecord(
            key="customer_claim.{}".format(claim),
            value=classification,
            source="customer_claim_vs_authoritative_evidence",
            status=classification,
            note=detail,
        ))
        _check(
            result,
            "customer_claim:{}".format(claim),
            classification,
            detail,
        )
    if assessments:
        result.verified_facts["customer_claim_assessment"] = assessments


def run_agent(text: str, as_of: Any = DEFAULT_DATA_AS_OF) -> AgentResult:
    inquiry = understand(text)
    result = AgentResult(
        inquiry=asdict(inquiry),
        understanding_mode=inquiry.understanding_mode,
    )

    if not inquiry.intents:
        result.unsupported.append("unsupported_or_non_domain_inquiry")
        return _finalize(
            result,
            "CLARIFY",
            "当前输入未包含可识别的跨境支付问询。请提供FL号，或提供原始金额、币种、银行和交易日期等合成演示字段。",
        )

    lookup_fields = [inquiry.original_amount, inquiry.currency, inquiry.bank, inquiry.date]
    if not inquiry.fl_id and all(value is None for value in lookup_fields):
        result.unsupported.append("transaction_locator_missing")
        return _finalize(
            result,
            "CLARIFY",
            "缺少可用于定位合成交易的信息。请提供FL号，或补充原始金额、币种、银行和交易日期。",
        )

    matches: List[Dict[str, Any]] = []
    if inquiry.fl_id:
        matches, error = _call_tool(
            result,
            "search_transaction",
            "Resolve the provided FL identifier.",
            "SQLite.transactions",
            {"fl_id": inquiry.fl_id},
            search_transaction,
            fl_id=inquiry.fl_id,
        )
        if error:
            return _data_source_failure(result)
        matches = matches or []
        if len(matches) == 1 and any(value is not None for value in lookup_fields):
            transaction = matches[0]
            provided: Dict[str, Any] = {}
            recorded: Dict[str, Any] = {}
            mismatch: List[str] = []
            if inquiry.original_amount is not None and not _amount_equal(
                transaction.get("original_amount"), inquiry.original_amount
            ):
                mismatch.append("original_amount")
                provided["original_amount"] = inquiry.original_amount
                recorded["original_amount"] = transaction.get("original_amount")
            if inquiry.currency and transaction.get("currency", "").upper() != inquiry.currency.upper():
                mismatch.append("currency")
                provided["currency"] = inquiry.currency
                recorded["currency"] = transaction.get("currency")
            if inquiry.bank and transaction.get("bank", "").upper() != inquiry.bank.upper():
                mismatch.append("bank")
                provided["bank"] = inquiry.bank
                recorded["bank"] = transaction.get("bank")
            if inquiry.date and not str(transaction.get("created_at", "")).startswith(inquiry.date):
                mismatch.append("date")
                provided["date"] = inquiry.date
                recorded["date"] = str(transaction.get("created_at", ""))[:10]
            if mismatch:
                candidates, candidate_error = _call_tool(
                    result,
                    "search_transaction_by_fields",
                    "Find possible transactions matching the descriptive fields without replacing the FL id.",
                    "SQLite.transactions",
                    {
                        "original_amount": inquiry.original_amount,
                        "currency": inquiry.currency,
                        "bank": inquiry.bank,
                        "date": inquiry.date,
                    },
                    search_transaction_by_fields,
                    original_amount=inquiry.original_amount,
                    currency=inquiry.currency,
                    bank=inquiry.bank,
                    date=inquiry.date,
                )
                if candidate_error:
                    return _data_source_failure(result)
                combined = matches + [row for row in (candidates or []) if row not in matches]
                result.matched_transactions = [_public_transaction(row) for row in combined]
                result.conflicts.append("IDENTIFIER_MISMATCH:{}".format(",".join(mismatch)))
                _fact(result, "possible_match_count", len(candidates or []), "SQLite.transactions")
                mismatch_fact = {
                    "fields": mismatch,
                    "provided": provided,
                    "recorded": recorded,
                }
                _fact(result, "identifier_mismatch", mismatch_fact, "rule_engine")
                _check(
                    result,
                    "identifier_consistency",
                    "FAIL",
                    "Provided FL id and descriptive fields disagree; both sides are retained.",
                    ["identifier_mismatch"],
                )
                return _finalize(
                    result,
                    "CLARIFY",
                    "提供的FL号与交易描述不一致。已保留用户提供值和系统记录值，不会自动替换交易编号；请确认正确的FL号。",
                )
    else:
        matches, error = _call_tool(
            result,
            "search_transaction_by_fields",
            "Resolve a transaction from supported descriptive fields.",
            "SQLite.transactions",
            {
                "original_amount": inquiry.original_amount,
                "currency": inquiry.currency,
                "bank": inquiry.bank,
                "date": inquiry.date,
            },
            search_transaction_by_fields,
            original_amount=inquiry.original_amount,
            currency=inquiry.currency,
            bank=inquiry.bank,
            date=inquiry.date,
        )
        if error:
            return _data_source_failure(result)
        matches = matches or []

    result.matched_transactions = [_public_transaction(row) for row in matches]
    if not matches:
        return _finalize(
            result,
            "CLARIFY",
            "根据当前支持的合成字段未定位到交易。请核对FL号，或补充原始金额、币种、银行和交易日期。",
        )
    if len(matches) > 1:
        _fact(result, "candidate_count", len(matches), "SQLite.transactions")
        _check(
            result,
            "entity_resolution",
            "FAIL",
            "More than one synthetic transaction matches the supplied fields.",
            ["candidate_count"],
        )
        return _finalize(
            result,
            "CLARIFY",
            "当前信息匹配到{}笔候选交易，不能安全地自动选择。请提供FL号。".format(len(matches)),
        )

    transaction = matches[0]
    _fact(result, "transaction_id", transaction["transaction_id"], "SQLite.transactions")
    _fact(result, "fl_id", transaction["fl_id"], "SQLite.transactions")
    _fact(result, "internal_status", transaction["internal_status"], "SQLite.transactions")
    _fact(result, "matched_transaction", transaction["fl_id"], "SQLite.transactions")

    critical_insufficiency = False
    bank: Optional[Dict[str, Any]] = None
    need_bank = any(intent in inquiry.intents for intent in [
        "STATUS_INQUIRY", "REFUND_INQUIRY", "REVIEW_INQUIRY",
    ]) or "bank_record_received_amount" in inquiry.customer_claims
    if need_bank:
        bank, error = _call_tool(
            result,
            "get_bank_status",
            "Read the bank-side status for the resolved synthetic transaction.",
            "SQLite.bank_status",
            {"bank_reference": transaction.get("bank_reference")},
            get_bank_status,
            transaction.get("bank_reference"),
        )
        if error:
            return _data_source_failure(result)
        if bank:
            _fact(
                result,
                "bank_status",
                bank["bank_status"],
                "SQLite.bank_status",
                observed_at=bank.get("last_updated"),
            )
            if bank.get("last_updated"):
                _fact(result, "bank_last_updated", bank["last_updated"], "SQLite.bank_status")
            if bank.get("bank_note"):
                _fact(result, "bank_note", bank["bank_note"], "SQLite.bank_status")
                _fact(result, "bank_message", bank["bank_note"], "SQLite.bank_status")
            if bank.get("reason_code"):
                _fact(result, "reason_code", bank["reason_code"], "SQLite.bank_status")
            if bank.get("eta"):
                _fact(result, "estimated_arrival_time", bank["eta"], "SQLite.bank_status")
        else:
            result.unsupported.append("bank_status_missing")
            result.escalation.append("BANK_INQUIRY")
            _check(
                result,
                "status_evidence",
                "FAIL",
                "No bank-side status record was found.",
                ["internal_status"],
            )

    if bank:
        has_conflict = detect_status_conflict(
            transaction.get("internal_status"), bank.get("bank_status")
        )
        _validation_trace(
            result,
            "detect_status_conflict",
            "Compare normalized internal and bank-side statuses.",
            "FAIL" if has_conflict else "PASS",
            "Status sources conflict." if has_conflict else "Status sources are compatible.",
            {
                "internal_status": transaction.get("internal_status"),
                "bank_status": bank.get("bank_status"),
            },
        )
        if has_conflict:
            status_values = {
                "internal_status": transaction.get("internal_status"),
                "bank_status": bank.get("bank_status"),
            }
            _fact(result, "status_conflict", status_values, "rule_engine", status="CONFLICT")
            result.conflicts.append("INTERNAL_BANK_STATUS_CONFLICT")
            result.escalation.append("OPERATIONS_REVIEW")
            _check(
                result,
                "status_consistency",
                "FAIL",
                "Authoritative sources disagree; neither side was selected as final truth.",
                ["internal_status", "bank_status", "status_conflict"],
            )
        else:
            _check(
                result,
                "status_consistency",
                "PASS",
                "Internal and bank-side statuses are compatible.",
                ["internal_status", "bank_status"],
            )

    if "FEE_INQUIRY" in inquiry.intents:
        original_amount = money_as_float(transaction.get("original_amount"))
        received_amount = money_as_float(transaction.get("received_amount"))
        _fact(result, "original_amount", original_amount, "SQLite.transactions")
        if received_amount is not None:
            _fact(result, "received_amount", received_amount, "SQLite.transactions")
        difference = calculate_amount_difference(original_amount, received_amount)
        if difference is not None:
            _fact(result, "amount_difference", difference, "calculation")

        fee_rows, error = _call_tool(
            result,
            "get_fee_info",
            "Read verified fee records for the resolved transaction.",
            "SQLite.fees",
            {"transaction_id": transaction["transaction_id"]},
            get_fee_info,
            transaction["transaction_id"],
        )
        if error:
            return _data_source_failure(result)
        verified_rows = [
            row for row in (fee_rows or [])
            if row.get("verified") and str(row.get("currency", "")).upper() == str(transaction.get("currency", "")).upper()
        ]
        verified_fee = None
        if verified_rows:
            total = sum((quantize_money(row.get("recorded_fee")) or Decimal("0")) for row in verified_rows)
            verified_fee = money_as_float(total)
            _fact(result, "verified_fee", verified_fee, "SQLite.fees")
        else:
            result.unsupported.append("verified_fee_evidence_missing")
            result.escalation.append("BANK_INQUIRY")
            critical_insufficiency = True

        if difference is None:
            result.unsupported.append("received_amount_missing")
            result.escalation.append("BANK_INQUIRY")
            critical_insufficiency = True
            _check(
                result,
                "fee_evidence_sufficiency",
                "FAIL",
                "A received amount is required to reconcile an amount difference.",
                ["original_amount"],
            )
        elif verified_fee is None:
            _check(
                result,
                "fee_evidence_sufficiency",
                "FAIL",
                "No verified same-currency fee record supports an explanation.",
                ["amount_difference"],
            )
        else:
            unexplained = calculate_amount_difference(difference, verified_fee)
            _fact(result, "unexplained_difference", unexplained, "calculation")
            if unexplained is not None and abs(unexplained) > 0.01:
                result.unsupported.append("remaining_amount_difference_reason")
                result.escalation.append("BANK_INQUIRY")
                if abs(verified_fee) <= 0.01:
                    critical_insufficiency = True
                _check(
                    result,
                    "fee_evidence_sufficiency",
                    "PARTIAL" if abs(verified_fee) > 0.01 else "FAIL",
                    "Only the recorded fee is attributable; the remainder has no verified cause.",
                    ["amount_difference", "verified_fee", "unexplained_difference"],
                )
            else:
                _check(
                    result,
                    "fee_evidence_sufficiency",
                    "PASS",
                    "The amount difference is fully reconciled by verified fee records.",
                    ["amount_difference", "verified_fee"],
                )

    refund: Optional[Dict[str, Any]] = None
    related_transaction: Optional[Dict[str, Any]] = None
    related_bank: Optional[Dict[str, Any]] = None
    if "REFUND_INQUIRY" in inquiry.intents:
        refund, error = _call_tool(
            result,
            "get_refund_info",
            "Read the refund record for the resolved transaction.",
            "SQLite.refunds",
            {"transaction_id": transaction["transaction_id"]},
            get_refund_info,
            transaction["transaction_id"],
        )
        if error:
            return _data_source_failure(result)
        if refund:
            _fact(result, "refund_status", refund["refund_status"], "SQLite.refunds")
            _fact(result, "original_refund_status", refund["refund_status"], "SQLite.refunds")
            _fact(result, "refund_type", refund["refund_type"], "SQLite.refunds")
            if refund.get("refund_reason"):
                _fact(result, "refund_reason", refund["refund_reason"], "SQLite.refunds")
                sop_rows, sop_error = _call_tool(
                    result,
                    "search_sop",
                    "Resolve the synthetic meaning and next action for the recorded refund code.",
                    "SQLite.sop",
                    {"term": refund["refund_reason"]},
                    search_sop,
                    refund["refund_reason"],
                )
                if sop_error:
                    return _data_source_failure(result)
                if sop_rows:
                    _fact(result, "refund_reason_meaning", sop_rows[0]["meaning"], "SQLite.sop")
                    _fact(result, "sop_explanation", sop_rows[0]["meaning"], "SQLite.sop")
                    _fact(result, "refund_next_action", sop_rows[0]["recommended_action"], "SQLite.sop")
                    _fact(result, "next_step", sop_rows[0]["recommended_action"], "SQLite.sop")
            _check(
                result,
                "refund_evidence_sufficiency",
                "PASS",
                "A refund record supports the refund status and recorded reason.",
                ["refund_status", "refund_reason"],
            )
            if refund.get("related_transaction_id"):
                related_matches, related_error = _call_tool(
                    result,
                    "search_transaction_related",
                    "Follow the recorded refund-to-new-payout relationship.",
                    "SQLite.transactions",
                    {"transaction_id": refund["related_transaction_id"]},
                    search_transaction,
                    transaction_id=refund["related_transaction_id"],
                )
                if related_error:
                    return _data_source_failure(result)
                if related_matches:
                    related_transaction = related_matches[0]
                    _fact(result, "related_transaction", related_transaction["fl_id"], "SQLite.transactions")
                    _fact(
                        result,
                        "related_internal_status",
                        related_transaction["internal_status"],
                        "SQLite.transactions",
                    )
                    related_bank, related_bank_error = _call_tool(
                        result,
                        "get_bank_status",
                        "Read the bank-side status for the related payout.",
                        "SQLite.bank_status",
                        {"bank_reference": related_transaction.get("bank_reference"), "scope": "related_payout"},
                        get_bank_status,
                        related_transaction.get("bank_reference"),
                    )
                    if related_bank_error:
                        return _data_source_failure(result)
                    if related_bank:
                        _fact(
                            result,
                            "related_bank_status",
                            related_bank["bank_status"],
                            "SQLite.bank_status",
                            observed_at=related_bank.get("last_updated"),
                        )
                        _fact(
                            result,
                            "related_payout_status",
                            related_bank["bank_status"],
                            "SQLite.bank_status",
                            observed_at=related_bank.get("last_updated"),
                        )
                    if "还没到账" in inquiry.text and related_transaction.get("internal_status") == "PROCESSING":
                        age = processing_age_calendar_days(related_transaction["created_at"], as_of=as_of)
                        _fact(result, "related_processing_age_calendar_days", age, "calculation")
                        _validation_trace(
                            result,
                            "check_processing_age",
                            "Compare related-payout age with the synthetic monitoring threshold.",
                            "FAIL" if age > 2 else "PASS",
                            "Related payout age is {} completed calendar day(s).".format(age),
                            {"threshold_calendar_days": 2, "as_of": str(as_of)},
                        )
                        if age > 2:
                            result.escalation.append("BANK_INQUIRY")
        else:
            result.unsupported.append("refund_record_missing")
            _check(
                result,
                "refund_evidence_sufficiency",
                "FAIL",
                "No refund record supports the requested refund conclusion.",
                ["internal_status", "bank_status"],
            )
            if bank and bank.get("bank_status") == "UNDER_RFI":
                _fact(result, "actual_status", "UNDER_RFI", "SQLite.bank_status")
                result.escalation.append("RISK_TEAM")
                critical_insufficiency = True
            else:
                result.escalation.append("BANK_INQUIRY")

    if "REVIEW_INQUIRY" in inquiry.intents:
        if bank and bank.get("bank_status") == "UNDER_RFI":
            _fact(
                result,
                "review_status",
                "UNDER_RFI",
                "SQLite.bank_status",
                observed_at=bank.get("last_updated"),
            )
            _fact(
                result,
                "current_review_status",
                "UNDER_RFI",
                "SQLite.bank_status",
                observed_at=bank.get("last_updated"),
            )
            if not bank.get("reason_code"):
                result.unsupported.append("review_reason")
                result.escalation.append("RISK_TEAM")
                critical_insufficiency = True
                _check(
                    result,
                    "review_reason_sufficiency",
                    "FAIL",
                    "Review status is visible, but no review reason is exposed.",
                    ["review_status"],
                )
        else:
            result.unsupported.append("review_status_not_supported")
            _check(
                result,
                "review_status_sufficiency",
                "FAIL",
                "Current authoritative status does not support an active-review premise.",
                ["bank_status"],
            )

        asks_history = any(marker in inquiry.text for marker in ["最近", "多笔", "每次", "历史"])
        if asks_history and transaction.get("customer_id"):
            history, history_error = _call_tool(
                result,
                "search_customer_history",
                "Count prior synthetic bank-review statuses for the same customer id.",
                "SQLite.transactions+bank_status",
                {"customer_id": transaction["customer_id"]},
                search_customer_history,
                transaction["customer_id"],
            )
            if history_error:
                return _data_source_failure(result)
            review_count = sum(1 for row in (history or []) if row.get("bank_status") == "UNDER_RFI")
            _fact(result, "historical_review_count", review_count, "SQLite.transactions+bank_status")

    if "DOCUMENT_REQUEST" in inquiry.intents:
        document_type = inquiry.document_type or "GPI"
        document_transaction_id = transaction["transaction_id"]
        if (
            document_type == "PAYMENT_PROOF"
            and related_transaction
            and "退款后" in inquiry.text
        ):
            document_transaction_id = related_transaction["transaction_id"]
        document, document_error = _call_tool(
            result,
            "get_document",
            "Read synthetic document metadata; no real document is fetched.",
            "SQLite.documents",
            {"transaction_id": document_transaction_id, "document_type": document_type},
            get_document,
            document_transaction_id,
            document_type,
        )
        if document_error:
            return _data_source_failure(result)
        metadata_available = bool(document and document.get("available"))
        _fact(result, "document_type", document_type, "inquiry_understanding")
        _fact(result, "document_available", metadata_available, "SQLite.documents")
        _fact(result, "document_metadata_available", metadata_available, "SQLite.documents")
        _fact(result, "document_downloadable", False, "demo_boundary")
        if document and document.get("source_system"):
            _fact(result, "document_source", document["source_system"], "SQLite.documents")
        if not metadata_available:
            result.unsupported.append("{}_metadata_unavailable".format(document_type))
            _check(
                result,
                "document_evidence_sufficiency",
                "FAIL",
                "No available synthetic metadata record supports this document request.",
                ["document_metadata_available"],
            )
        else:
            _check(
                result,
                "document_evidence_sufficiency",
                "PASS",
                "Synthetic metadata is marked available; no downloadable bank document is claimed.",
                ["document_metadata_available", "document_downloadable"],
            )

    explicit_age_question = "天" in inquiry.text or "超时" in inquiry.text
    if transaction.get("internal_status") == "PROCESSING" and explicit_age_question:
        age = processing_age_calendar_days(transaction["created_at"], as_of=as_of)
        _fact(result, "processing_age_days", age, "calculation")
        _fact(result, "processing_age", age, "calculation")
        _validation_trace(
            result,
            "check_processing_age",
            "Compare completed calendar days with the synthetic monitoring threshold.",
            "FAIL" if age > 2 else "PASS",
            "Processing age is {} completed calendar day(s).".format(age),
            {"threshold_calendar_days": 2, "as_of": str(as_of)},
        )
        if age > 2:
            result.unsupported.append("processing_over_synthetic_threshold")
            result.escalation.append("BANK_INQUIRY")
            critical_insufficiency = True

    asks_eta = any(marker in inquiry.text for marker in [
        "什么时候", "预计", "多久", "今天到账", "到账时间",
    ])
    if asks_eta:
        if not bank or not bank.get("eta"):
            result.unsupported.append("estimated_arrival_time")
            result.escalation.append("BANK_INQUIRY")
            _fact(result, "eta_missing", True, "rule_engine", status="UNAVAILABLE")
            if inquiry.intents == ["STATUS_INQUIRY"]:
                critical_insufficiency = True
            _check(
                result,
                "eta_sufficiency",
                "FAIL",
                "No source-backed ETA is available.",
                ["bank_status", "bank_last_updated"],
            )
        else:
            _check(
                result,
                "eta_sufficiency",
                "PASS",
                "A bank-side ETA is available.",
                ["estimated_arrival_time"],
            )

    _classify_customer_claims(result, inquiry.customer_claims, transaction, bank)

    if result.conflicts:
        decision = "ESCALATE"
        sufficiency_status = "FAIL"
    elif critical_insufficiency:
        decision = "ESCALATE"
        sufficiency_status = "FAIL"
    elif result.unsupported:
        decision = "PARTIAL_RESOLUTION"
        sufficiency_status = "PARTIAL"
    elif result.escalation:
        decision = "ESCALATE"
        sufficiency_status = "FAIL"
    else:
        decision = "ANSWER"
        sufficiency_status = "PASS"

    _validation_trace(
        result,
        "check_answer_sufficiency",
        "Confirm that every requested conclusion is supported before drafting.",
        sufficiency_status,
        "Decision {} from {} unsupported item(s), {} conflict(s), and {} escalation route(s).".format(
            decision, len(_dedupe(result.unsupported)), len(_dedupe(result.conflicts)),
            len(_dedupe(result.escalation)),
        ),
        {"intents": list(inquiry.intents)},
    )
    return _finalize(result, decision)


def build_draft(result: AgentResult) -> str:
    facts = result.verified_facts
    parts: List[str] = []
    if facts.get("fl_id"):
        parts.append("已定位合成交易 {}。".format(facts["fl_id"]))
    if "internal_status" in facts:
        parts.append("内部状态为 {}。".format(facts["internal_status"]))
    if "bank_status" in facts:
        freshness = "，最后更新时间 {}".format(facts["bank_last_updated"]) if facts.get("bank_last_updated") else ""
        parts.append("银行侧状态为 {}{}。".format(facts["bank_status"], freshness))
    if facts.get("status_conflict"):
        conflict = facts["status_conflict"]
        parts.append(
            "两侧记录分别为内部 {}、银行 {}；现有规则不会擅自选择一侧作为最终事实。".format(
                conflict.get("internal_status"), conflict.get("bank_status")
            )
        )
    if "amount_difference" in facts:
        parts.append("原始金额与到账金额差额为 {:.2f}。".format(facts["amount_difference"]))
    if "verified_fee" in facts:
        parts.append("同币种、已验证费用记录合计为 {:.2f}。".format(facts["verified_fee"]))
    if "unexplained_difference" in facts and abs(facts["unexplained_difference"]) > 0.01:
        parts.append(
            "仍有 {:.2f} 的差额缺少可验证原因，不能直接归因。".format(
                facts["unexplained_difference"]
            )
        )
    if facts.get("refund_status"):
        parts.append("原交易退款记录状态为 {}。".format(facts["refund_status"]))
    if facts.get("refund_reason"):
        parts.append("记录的退款原因代码为 {}。".format(facts["refund_reason"]))
    if facts.get("refund_reason_meaning"):
        parts.append("合成SOP说明：{}。".format(facts["refund_reason_meaning"]))
    if facts.get("refund_next_action"):
        parts.append("合成SOP建议：{}。".format(facts["refund_next_action"]))
    if facts.get("related_transaction"):
        parts.append(
            "退款关联的新出金为 {}，银行侧状态为 {}。".format(
                facts["related_transaction"], facts.get("related_bank_status", "暂无记录")
            )
        )
    if "review_status" in facts:
        parts.append("当前可确认交易处于 {}。".format(facts["review_status"]))
    if "historical_review_count" in facts:
        parts.append("合成历史中可验证的UNDER_RFI记录数为 {}。".format(facts["historical_review_count"]))
    if "document_metadata_available" in facts:
        if facts["document_metadata_available"]:
            parts.append(
                "{} 的合成元数据记录标记为可用；本演示不提供真实银行文件下载。".format(
                    facts.get("document_type", "文档")
                )
            )
        else:
            parts.append("未找到标记为可用的 {} 合成元数据记录。".format(facts.get("document_type", "文档")))
    if facts.get("processing_age_days") is not None:
        parts.append(
            "截至合成数据快照，已处理 {} 个完整日历日。".format(facts["processing_age_days"])
        )
    if "estimated_arrival_time" in result.unsupported:
        parts.append("当前证据不支持给出预计到账时间。")
    if "review_reason" in result.unsupported:
        parts.append("当前数据未暴露审查原因，不能推断客户风险或触发原因。")
    if "refund_record_missing" in result.unsupported:
        parts.append("现有记录不支持退款前提，也没有可验证的退款原因。")
    if "received_amount_missing" in result.unsupported:
        parts.append("缺少到账金额，当前无法完成费用差额核验。")
    if result.conflicts:
        parts.append("检测到需要人工处理的数据冲突。")
    if result.escalation:
        parts.append("建议升级处理：{}。".format("、".join(_dedupe(result.escalation))))
    if result.decision != "ANSWER":
        parts.append("该内容仅为待复核草稿，不会发送或写回任何外部系统。")
    return "".join(parts) or "当前证据不足，未生成交易结论。"
