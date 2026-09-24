# Evaluation

## What is measured

The evaluator reads the synthetic `Inquiries` sheet and checks each case against the deterministic workflow:

- expected decision;
- required intent coverage;
- required tool or validation-trace steps;
- expected escalation queues;
- absence of case-specific forbidden phrases.

Unit tests separately cover parsing boundaries, amount-role extraction, ambiguous matching, missing evidence, status conflicts, customer claims, document metadata wording, database failure handling, time injection, and trace minimization.

## Reproduce

```bash
python3 setup_db.py --force
python3 -m unittest discover -s tests -v
python3 evaluate.py
```

Both the unit test command and evaluator must exit successfully. The GitHub workflow runs the same commands.

## Verified result

On the checked-in synthetic snapshot, all 20 cases pass every configured dimension:

| Dimension | Result |
| --- | ---: |
| Scenario pass rate | 20 / 20 |
| Decision routing | 20 / 20 |
| Intent coverage | 20 / 20 |
| Tool/validation trace coverage | 20 / 20 |
| Escalation routing | 20 / 20 |
| Expected evidence fields | 20 / 20 |
| Forbidden-phrase checks | 20 / 20 |
| Review gate | 20 / 20 |

The result is a regression result over these hand-authored cases. It must not be described as 100% production accuracy.

## Interpretation limits

This suite is a regression check over hand-authored synthetic examples. It is not a production accuracy claim, a model benchmark, an estimate of live-bank coverage, or evidence of performance on unrestricted language. A passing forbidden-phrase check only covers the phrases specified for each scenario; it is not a general hallucination detector.
