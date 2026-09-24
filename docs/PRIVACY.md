# Privacy and data boundary

## Included data

All transaction IDs, FL numbers, customer IDs, amounts, statuses, notes, inquiries, SOP rules, and document records in this repository are synthetic. They were created to exercise workflow branches and do not represent customers or activity from a real company.

Real bank names appear only as familiar labels in a fictional dataset. Nothing in this project should be read as an implementation of those banks' internal status models, reason codes, service levels, document flows, or operating procedures.

## Excluded data and connectivity

The project contains no:

- customer PII or real transaction history;
- bank credentials, API keys, or production secrets;
- SWIFT/GPI, bank portal, payment processor, or employer-system connection;
- real bank document or downloadable payment proof;
- confidential SOP or policy copied from an employer;
- payment execution or outbound communication capability.

## Local mode

The default deterministic mode runs locally. The source workbook is converted into a local SQLite database, and the generated database is ignored by Git.

## Optional OpenAI parser

The OpenAI-assisted parser is disabled by default and requires an explicit environment flag, API key, and model. When enabled, the inquiry text is transmitted to the configured OpenAI API project for structured parsing. Only synthetic inquiry text should be used. The returned structure is validated; failures fall back to the local parser. Transaction lookup, calculations, evidence validation, and decisions remain local and deterministic.

## Publishing checklist

- Keep `.env` and `.streamlit/secrets.toml` out of Git.
- Rebuild the database from the checked-in workbook instead of committing a runtime `.db` file.
- Do not replace synthetic examples with employer or customer data.
- Do not add screenshots containing real transaction, bank, customer, or ticket information.
- Re-run unit tests and the synthetic scenario evaluation before publishing.

