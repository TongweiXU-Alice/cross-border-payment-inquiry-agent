"""Deterministic, parameterized tools over the local synthetic SQLite data."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from .db import rows


MONEY_QUANTUM = Decimal("0.01")
DEFAULT_DATA_AS_OF = "2026-09-22 12:00"


def quantize_money(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid monetary value") from exc


def money_as_float(value: Any) -> Optional[float]:
    quantized = quantize_money(value)
    return float(quantized) if quantized is not None else None


def _normalize_transaction(row: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(row)
    for key in ("original_amount", "received_amount"):
        normalized[key] = money_as_float(normalized.get(key))
    return normalized


def _normalize_fee(row: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(row)
    normalized["recorded_fee"] = money_as_float(normalized.get("recorded_fee"))
    normalized["verified"] = bool(normalized.get("verified"))
    return normalized


def search_transaction(fl_id: Optional[str] = None, transaction_id: Optional[str] = None) -> List[dict]:
    if fl_id:
        result = rows(
            "SELECT * FROM transactions WHERE UPPER(fl_id)=UPPER(?) LIMIT 2",
            (fl_id,),
        )
    elif transaction_id:
        result = rows(
            "SELECT * FROM transactions WHERE transaction_id=? LIMIT 2",
            (transaction_id,),
        )
    else:
        return []
    return [_normalize_transaction(row) for row in result]


def search_transaction_by_fields(
    amount: Optional[float] = None,
    currency: Optional[str] = None,
    bank: Optional[str] = None,
    date: Optional[str] = None,
    original_amount: Optional[float] = None,
) -> List[dict]:
    search_amount = original_amount if original_amount is not None else amount
    if all(value is None for value in [search_amount, currency, bank, date]):
        return []
    sql = "SELECT * FROM transactions WHERE 1=1"
    params: List[Any] = []
    if search_amount is not None:
        sql += " AND ABS(original_amount-?) < 0.005"
        params.append(money_as_float(search_amount))
    if currency:
        sql += " AND UPPER(currency)=UPPER(?)"
        params.append(currency)
    if bank:
        sql += " AND UPPER(bank)=UPPER(?)"
        params.append(bank)
    if date:
        sql += " AND substr(created_at,1,10)=?"
        params.append(date)
    sql += " ORDER BY created_at, transaction_id LIMIT 25"
    return [_normalize_transaction(row) for row in rows(sql, params)]


def get_bank_status(bank_reference: Optional[str]) -> Optional[dict]:
    if not bank_reference:
        return None
    result = rows(
        "SELECT * FROM bank_status WHERE bank_reference=? LIMIT 1",
        (bank_reference,),
    )
    return result[0] if result else None


def get_refund_info(transaction_id: Optional[str]) -> Optional[dict]:
    if not transaction_id:
        return None
    result = rows(
        "SELECT * FROM refunds WHERE transaction_id=? LIMIT 1",
        (transaction_id,),
    )
    if not result:
        return None
    refund = dict(result[0])
    refund["refund_amount"] = money_as_float(refund.get("refund_amount"))
    return refund


def get_fee_info(transaction_id: Optional[str]) -> List[dict]:
    if not transaction_id:
        return []
    return [
        _normalize_fee(row)
        for row in rows(
            "SELECT * FROM fees WHERE transaction_id=? ORDER BY fee_type, fee_source",
            (transaction_id,),
        )
    ]


def get_document(transaction_id: Optional[str], document_type: Optional[str]) -> Optional[dict]:
    if not transaction_id or not document_type:
        return None
    result = rows(
        """SELECT transaction_id, document_type, available, source_system
           FROM documents
           WHERE transaction_id=? AND UPPER(document_type)=UPPER(?)
           LIMIT 1""",
        (transaction_id, document_type),
    )
    if not result:
        return None
    document = dict(result[0])
    document["available"] = bool(document.get("available"))
    return document


def search_sop(term: Optional[str]) -> List[dict]:
    if not term:
        return []
    like = "%{}%".format(term)
    return rows(
        """SELECT * FROM sop
           WHERE condition_or_code LIKE ? OR meaning LIKE ? OR category LIKE ?
           ORDER BY rule_id LIMIT 10""",
        (like, like, like),
    )


def search_customer_history(customer_id: Optional[str]) -> List[dict]:
    if not customer_id:
        return []
    return rows(
        """SELECT t.transaction_id, t.fl_id, t.created_at, t.internal_status,
                  b.bank_status, b.last_updated
           FROM transactions t
           LEFT JOIN bank_status b ON t.bank_reference=b.bank_reference
           WHERE t.customer_id=?
           ORDER BY t.created_at DESC
           LIMIT 25""",
        (customer_id,),
    )


def detect_status_conflict(internal_status: Optional[str], bank_status: Optional[str]) -> bool:
    normalized_internal = {
        "REFUNDED": "RETURNED",
        "COMPLETED": "COMPLETED",
        "PROCESSING": "PROCESSING",
        "FAILED": "FAILED",
    }.get(internal_status, internal_status)
    normalized_bank = {
        "RETURNED": "RETURNED",
        "COMPLETED": "COMPLETED",
        "PROCESSING": "PROCESSING",
        "UNDER_RFI": "UNDER_RFI",
    }.get(bank_status, bank_status)
    if normalized_internal == normalized_bank:
        return False
    terminal = {"COMPLETED", "RETURNED", "FAILED"}
    return normalized_internal in terminal or normalized_bank in terminal


def calculate_amount_difference(original_amount: Any, received_amount: Any) -> Optional[float]:
    original = quantize_money(original_amount)
    received = quantize_money(received_amount)
    if original is None or received is None:
        return None
    return float((original - received).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP))


def processing_age_calendar_days(
    created_at: Any,
    as_of: Any = DEFAULT_DATA_AS_OF,
) -> int:
    """Return completed calendar days at an explicit synthetic-data snapshot."""

    start = created_at if isinstance(created_at, datetime) else datetime.fromisoformat(str(created_at))
    if isinstance(as_of, datetime):
        end = as_of
    elif isinstance(as_of, date):
        end = datetime.combine(as_of, datetime.min.time())
    else:
        end = datetime.fromisoformat(str(as_of))
    if end < start:
        raise ValueError("as_of precedes transaction creation time")
    return (end - start).days


# Compatibility alias with corrected calendar-day semantics.
def processing_age_days(created_at: Any, now: Any = DEFAULT_DATA_AS_OF) -> int:
    return processing_age_calendar_days(created_at, as_of=now)
