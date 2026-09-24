# Workflow specification

## Objective

Convert a bounded cross-border payment inquiry into an evidence-grounded draft and a clear next step using only the synthetic local dataset.

## Intents

| Intent | Question answered | Required local evidence |
| --- | --- | --- |
| `STATUS_INQUIRY` | What status do the local sources show? | One uniquely matched transaction; bank-status metadata when the workflow needs it |
| `FEE_INQUIRY` | How much of the received-amount difference is supported by recorded fees? | Original and received amounts plus verified fee records in the same currency |
| `REFUND_INQUIRY` | Was a refund recorded, why, and is there a related payout? | Refund record; linked transaction/status when present; synthetic SOP match for code meaning |
| `REVIEW_INQUIRY` | Does the local snapshot show review/RFI? | Bank-status record; a reason may be reported only when the source contains one |
| `DOCUMENT_REQUEST` | Does the local table contain document metadata? | Documents-row availability flag; the project never implies that a real file can be downloaded |

## Decisions

| Decision | Meaning |
| --- | --- |
| `ANSWER` | The requested fields are supported and no policy check requires review. |
| `CLARIFY` | The transaction cannot be uniquely located using the fields the implementation actually supports. |
| `PARTIAL_RESOLUTION` | Some requested facts are verified, but one or more requested conclusions remain unsupported. |
| `ESCALATE` | A source conflict, missing high-risk review evidence, overdue synthetic processing rule, or data-access failure requires human follow-up. |

## Control rules

1. Customer statements are captured as claims, not facts.
2. Only parameterized SQL results and deterministic calculations enter the verified evidence ledger.
3. Amount differences use the transaction currency and two-decimal Decimal arithmetic.
4. A verified fee can explain only its recorded amount; any remainder stays unexplained.
5. A source conflict shows both values and routes to operations review.
6. An RFI/review reason remains unknown when the reason field is empty.
7. Missing ETA, refund, fee, or document evidence cannot silently produce `ANSWER`.
8. The UI and CLI produce drafts only. No external message, approval, or payment action exists.

## Time convention

The bundled dataset is a fixed demonstration snapshot. Processing age is measured in calendar days against the explicit snapshot time used by the run. It is not a bank service-level agreement or a business-day calculation.

