"""Inquiry parsing with a deterministic default and an explicit optional LLM path."""

import os
import re
from datetime import date as date_type
from typing import Any, Dict, List, Optional

from .models import Inquiry


ALLOWED_INTENTS = {
    "STATUS_INQUIRY",
    "FEE_INQUIRY",
    "REFUND_INQUIRY",
    "REVIEW_INQUIRY",
    "DOCUMENT_REQUEST",
}
ALLOWED_DOCUMENT_TYPES = {"GPI", "PAYMENT_PROOF", "PAYMENT_MESSAGE", "RETURN_MESSAGE"}
ALLOWED_CLAIMS = {
    "internal_status_completed",
    "customer_attributes_difference_to_platform_fee",
    "customer_claims_refunded",
    "bank_record_received_amount",
}
CURRENCY_ALIASES = {
    "usd": "USD",
    "美元": "USD",
    "eur": "EUR",
    "欧元": "EUR",
    "gbp": "GBP",
    "英镑": "GBP",
    "cny": "CNY",
    "人民币": "CNY",
    "jpy": "JPY",
    "日元": "JPY",
}
DEFAULT_DATA_YEAR = 2026
MAX_INQUIRY_CHARS = 4000


def _dedupe(values: List[str]) -> List[str]:
    return list(dict.fromkeys(values))


def _valid_iso_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return date_type.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return None


def _extract_date(text: str) -> Optional[str]:
    match = re.search(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)", text)
    if match:
        return _valid_iso_date(match.group(1))
    match = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日", text)
    if not match:
        return None
    candidate = f"{DEFAULT_DATA_YEAR}-{int(match.group(1)):02d}-{int(match.group(2)):02d}"
    return _valid_iso_date(candidate)


def _extract_bank(low: str) -> Optional[str]:
    if "花旗" in low or re.search(r"\bciti\b", low):
        return "Citi"
    if "渣打" in low or "standard chartered" in low or re.search(r"\bscb\b", low):
        return "SCB"
    if "德银" in low or "deutsche" in low or re.search(r"\bdb\b", low):
        return "DB"
    return None


def _extract_currency(low: str) -> Optional[str]:
    for alias in sorted(CURRENCY_ALIASES, key=len, reverse=True):
        if alias in low:
            return CURRENCY_ALIASES[alias]
    return None


def _extract_amounts(text: str, currency: Optional[str]) -> Dict[str, Optional[float]]:
    """Extract sent/original and received amounts without conflating their roles."""

    cleaned = re.sub(r"(?<![A-Za-z0-9])FL\d+", " ", text, flags=re.I)
    cleaned = re.sub(r"20\d{2}-\d{2}-\d{2}", " ", cleaned)
    cleaned = re.sub(r"\d{1,2}月\d{1,2}日", " ", cleaned)
    number_re = re.compile(
        r"(?<![A-Za-z0-9])(?P<number>\d[\d,]*(?:\.\d+)?)\s*"
        r"(?P<unit>USD|EUR|GBP|CNY|JPY|美元|欧元|英镑|人民币|日元)?",
        re.I,
    )
    original = None
    received = None
    unclassified: List[float] = []
    for match in number_re.finditer(cleaned):
        value = float(match.group("number").replace(",", ""))
        if not (100 < value < 100_000_000):
            continue
        unit = match.group("unit")
        if not unit and not currency:
            continue
        prefix = cleaned[max(0, match.start() - 24) : match.start()].lower()
        if any(marker in prefix for marker in [
            "只到账", "到账", "只收到", "收到", "实收", "银行记录只有", "记录只有",
        ]):
            if received is None:
                received = value
            continue
        if any(marker in prefix for marker in [
            "汇了", "汇出", "汇款", "付款", "发起", "原始", "支付", "转出",
        ]):
            if original is None:
                original = value
            continue
        unclassified.append(value)

    # A currency-qualified, otherwise unclassified amount is a transaction
    # description. Received wording above always wins, so "只收到 9950 USD" is
    # never used for an FL/original-amount mismatch check.
    if original is None and unclassified:
        original = unclassified[0]
    return {"original_amount": original, "received_amount": received}


def deterministic_understand(text: str) -> Inquiry:
    text = (text or "").strip()[:MAX_INQUIRY_CHARS]
    low = text.lower()
    intents: List[str] = []

    if any(k in text for k in [
        "哪里", "进度", "状态", "没到账", "未到账", "还没到", "什么时候能到账",
        "什么时候到账", "现在在哪里", "资金实际", "催问资金",
    ]) or any(k in low for k in ["processing", "payment has been processed", "status"]):
        intents.append("STATUS_INQUIRY")
    if any(k in text for k in [
        "手续费", "少了", "只到账", "只收到", "到账金额", "扣了", "金额差", "差额", "记录只有",
    ]) or "fee" in low:
        intents.append("FEE_INQUIRY")
    if any(k in text for k in ["退款", "退回", "退票"]) or any(k in low for k in ["refund", "returned"]):
        intents.append("REFUND_INQUIRY")
    if any(k in text for k in ["审查", "风控"]) or any(k in low for k in ["rfi", "review"]):
        intents.append("REVIEW_INQUIRY")
    if any(k in text for k in ["报文", "凭证"]) or any(k in low for k in ["gpi", "payment proof"]):
        intents.append("DOCUMENT_REQUEST")

    fl_match = re.search(r"(?<![A-Za-z0-9])FL\d+", text, re.I)
    fl_id = fl_match.group(0).upper() if fl_match else None
    currency = _extract_currency(low)
    bank = _extract_bank(low)
    inquiry_date = _extract_date(text)
    amounts = _extract_amounts(text, currency)

    document_type = None
    if "gpi" in low:
        document_type = "GPI"
    elif "付款凭证" in text or "payment proof" in low or "凭证" in text:
        document_type = "PAYMENT_PROOF"
    elif ("退款" in text or "退回" in text) and "报文" in text:
        document_type = "RETURN_MESSAGE"
    elif "报文" in text:
        document_type = "PAYMENT_MESSAGE"

    reason_code = None
    if re.search(r"AC[\s_-]*NOT[\s_-]*IN[\s_-]*BOOKS", text, re.I):
        reason_code = "AC_NOT_IN_BOOKS"
    elif re.search(r"BENEFICIARY[\s_-]*REJECTED", text, re.I):
        reason_code = "BENEFICIARY_REJECTED"

    claims: List[str] = []
    if "系统显示付款成功" in text or "系统显示成功" in text:
        claims.append("internal_status_completed")
    if "认为平台扣" in text:
        claims.append("customer_attributes_difference_to_platform_fee")
    if "已经被退回" in text or "已经退回" in text:
        claims.append("customer_claims_refunded")
    if "银行记录" in text:
        claims.append("bank_record_received_amount")

    domain_signal = any([
        fl_id,
        currency,
        bank,
        inquiry_date,
        amounts["original_amount"],
        amounts["received_amount"],
    ])
    if not intents and domain_signal:
        intents.append("STATUS_INQUIRY")

    return Inquiry(
        text=text,
        intents=_dedupe(intents),
        fl_id=fl_id,
        amount=amounts["original_amount"],
        original_amount=amounts["original_amount"],
        received_amount=amounts["received_amount"],
        currency=currency,
        bank=bank,
        date=inquiry_date,
        document_type=document_type,
        reason_code=reason_code,
        customer_claims=_dedupe(claims),
        understanding_mode="deterministic",
    )


def _parsed_to_inquiry(text: str, data: Dict[str, Any]) -> Inquiry:
    intents = data.get("intents") or []
    if not isinstance(intents, list) or any(i not in ALLOWED_INTENTS for i in intents):
        raise ValueError("invalid intents")
    claims = data.get("customer_claims") or []
    if not isinstance(claims, list) or any(c not in ALLOWED_CLAIMS for c in claims):
        raise ValueError("invalid customer claims")
    document_type = data.get("document_type")
    if document_type is not None and document_type not in ALLOWED_DOCUMENT_TYPES:
        raise ValueError("invalid document type")
    inquiry_date = data.get("date")
    if inquiry_date and not _valid_iso_date(inquiry_date):
        raise ValueError("invalid date")
    fl_id = data.get("fl_id")
    if fl_id:
        fl_id = str(fl_id).upper()
        if not re.fullmatch(r"FL\d+", fl_id):
            raise ValueError("invalid FL id")
    original_amount = data.get("original_amount")
    received_amount = data.get("received_amount")
    for amount in [original_amount, received_amount]:
        if amount is not None and (not isinstance(amount, (int, float)) or amount <= 0):
            raise ValueError("invalid amount")
    return Inquiry(
        text=text,
        intents=_dedupe(intents),
        fl_id=fl_id,
        amount=float(original_amount) if original_amount is not None else None,
        original_amount=float(original_amount) if original_amount is not None else None,
        received_amount=float(received_amount) if received_amount is not None else None,
        currency=data.get("currency"),
        bank=data.get("bank"),
        date=_valid_iso_date(inquiry_date),
        document_type=document_type,
        reason_code=data.get("reason_code"),
        customer_claims=_dedupe(claims),
        understanding_mode="openai_structured",
    )


def llm_understand(text: str) -> Inquiry:
    """Use the Responses structured parser only after explicit opt-in."""

    enabled = os.getenv("OPENAI_PARSER_ENABLED") == "1"
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")
    if not (enabled and api_key and model):
        return deterministic_understand(text)

    try:
        from typing import Literal

        from openai import OpenAI
        from pydantic import BaseModel, ConfigDict, Field

        class ParsedInquiry(BaseModel):
            model_config = ConfigDict(extra="forbid")

            intents: List[Literal[
                "STATUS_INQUIRY",
                "FEE_INQUIRY",
                "REFUND_INQUIRY",
                "REVIEW_INQUIRY",
                "DOCUMENT_REQUEST",
            ]] = Field(default_factory=list)
            fl_id: Optional[str] = None
            original_amount: Optional[float] = Field(default=None, gt=0)
            received_amount: Optional[float] = Field(default=None, gt=0)
            currency: Optional[Literal["USD", "EUR", "GBP", "CNY", "JPY"]] = None
            bank: Optional[Literal["Citi", "SCB", "DB"]] = None
            date: Optional[str] = None
            document_type: Optional[Literal[
                "GPI", "PAYMENT_PROOF", "PAYMENT_MESSAGE", "RETURN_MESSAGE",
            ]] = None
            reason_code: Optional[str] = None
            customer_claims: List[Literal[
                "internal_status_completed",
                "customer_attributes_difference_to_platform_fee",
                "customer_claims_refunded",
                "bank_record_received_amount",
            ]] = Field(default_factory=list)

        client = OpenAI(api_key=api_key)
        response = client.responses.parse(
            model=model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "Extract only explicitly stated fields for the synthetic cross-border "
                        "payment demo. Distinguish original/sent amount from received amount. "
                        "Do not invent missing values."
                    ),
                },
                {"role": "user", "content": (text or "")[:MAX_INQUIRY_CHARS]},
            ],
            text_format=ParsedInquiry,
            store=False,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise ValueError("structured parser returned no object")
        inquiry = _parsed_to_inquiry(text, parsed.model_dump())
        if not inquiry.intents and any([
            inquiry.fl_id,
            inquiry.original_amount,
            inquiry.received_amount,
            inquiry.currency,
            inquiry.bank,
            inquiry.date,
        ]):
            inquiry.intents = ["STATUS_INQUIRY"]
        return inquiry
    except Exception:
        fallback = deterministic_understand(text)
        fallback.understanding_mode = "deterministic_fallback"
        return fallback


def understand(text: str) -> Inquiry:
    return llm_understand(text)
