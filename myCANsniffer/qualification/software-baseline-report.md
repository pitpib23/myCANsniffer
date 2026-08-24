# Qualification run: software-baseline-v1

Overall: **PASS**

| Entry | Kind | Provenance | Result | Checks |
|---|---|---|---|---:|
| synthetic-protocols | CAPTURE | synthetic | PASS | 9/9 |
| synthetic-negative | CAPTURE | synthetic | PASS | 6/6 |
| synthetic-eds | EDS | synthetic | PASS | 2/2 |
| synthetic-dcf | DCF | synthetic | PASS | 2/2 |
| synthetic-j1939-definition | J1939_DEFINITION | synthetic | PASS | 3/3 |

## Detection metrics

```json
{
  "diagnostic_correlation": {
    "ambiguous": 0,
    "fn": 0,
    "fp": 0,
    "labeled_entries": 1,
    "tp": 1
  },
  "ground_truth_correlation": {
    "expectations": 22,
    "passed_expectations": 22
  },
  "protocol_false_negatives": {
    "detected": 3,
    "missed": 0,
    "opportunities": 3,
    "partial": 0
  },
  "protocol_false_positives": {
    "count": 0,
    "observed_levels": {
      "Confirmed": 0,
      "None": 4,
      "Possible": 0,
      "Strong": 0,
      "Weak": 1
    },
    "opportunities": 5,
    "unexpected_levels": {
      "Confirmed": 0,
      "None": 0,
      "Possible": 0,
      "Strong": 0,
      "Weak": 0
    }
  }
}
```

Synthetic PASS results are SOFTWARE_TESTED only. A real, lawful capture is required for CAPTURE_VALIDATED.
