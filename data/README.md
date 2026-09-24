# Synthetic dataset

`Cross_Border_Payment_Agent_V1.xlsx` is the versioned source for the local demo database and the regression scenarios.

| Sheet | Purpose |
| --- | --- |
| `Transactions` | Synthetic internal transaction records |
| `Bank_Status` | Synthetic bank-status metadata used for cross-source checks |
| `Refunds` | Refund records and optional links to a related payout |
| `Fees` | Recorded fees and verification flags |
| `Documents` | Document metadata only; paths are illustrative and are not shipped bank files |
| `SOP` | Synthetic code meanings and recommended actions |
| `Inquiries` | Hand-authored regression scenarios and expected safeguards |
| `Data_Dictionary` | Field meanings and workflow use |
| `README` | Dataset scope and usage boundary |

Run `python3 setup_db.py --force` to validate the expected columns and build `payments.db` atomically. The generated database is ignored by Git.

