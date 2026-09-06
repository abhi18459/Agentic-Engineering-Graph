# Implementation Plan

## Current behavior

- `starter_repo/plot_data.py`
  - `read_csv_data()` loads the CSV with pandas.
  - It collects missing requested columns in `(x_col, y_col)` order, deduplicates repeated names, and raises `ValueError("CSV is missing required column(s): ...")`.
  - Existing columns are returned unchanged as Python lists without validating their pandas dtypes.
  - `main()` calls `read_csv_data()` before `create_plot()`, so validation added there will occur before Matplotlib is invoked.
  - `create_plot()` and the command-line arguments are independent of CSV validation and should remain unchanged.
- `tests/test_plot_data.py`
  - Covers successful numeric reads, each missing axis, two missing columns, repeated missing-column deduplication, and basic plotting behavior.
  - It does not cover existing columns containing non-numeric data.

## Recommended implementation

1. Update `starter_repo/plot_data.py` to use pandas’ numeric-dtype predicate, preferably `pandas.api.types.is_numeric_dtype`.
2. Keep the current missing-column collection and `ValueError` unchanged and execute it before dtype inspection. This preserves both the existing message and missing-column precedence, including when another selected column is non-numeric.
3. After confirming all requested columns exist:
   - Iterate through `(x_col, y_col)` in axis order.
   - Collect each selected column whose pandas `Series` dtype is not numeric.
   - Deduplicate names while collecting so selecting the same non-numeric column for both axes identifies it only once.
4. If any non-numeric columns were collected, raise a descriptive `ValueError`, such as:
   - `CSV column(s) must be numeric: category`
   - `CSV column(s) must be numeric: category, label`
5. Only return `df[x_col].tolist(), df[y_col].tolist()` after both presence and numeric-type validation succeed.
6. Do not alter `main()`, `create_plot()`, CLI arguments, output handling, or numeric value conversion; valid numeric columns should retain their currently inferred values.

## Test changes

Extend `tests/test_plot_data.py` with temporary CSV fixtures or locally constructed DataFrames covering:

1. One non-numeric selected column:
   - Use one string column and one numeric column.
   - Call `read_csv_data()` with the string column selected for an axis.
   - Assert that `ValueError` is raised and that the column name occurs exactly once in the message.
2. Two distinct non-numeric selected columns:
   - Select separate string columns for `x_col` and `y_col`.
   - Assert both names appear and each occurs exactly once.
3. Repeated non-numeric selection:
   - Select the same string column for both axes.
   - Assert its name occurs exactly once, directly protecting the deduplication requirement.
4. Preserve the existing successful numeric test to verify numeric columns still return their original lists.
5. Retain all current missing-column tests. Optionally add a mixed case with one missing and one non-numeric requested column to assert the established missing-column error is raised first and remains unchanged.

## Validation commands

Run from the repository root:

```bash
pytest
ruff check starter_repo tests
ruff format --check starter_repo tests
mypy starter_repo tests
```

For parity with the configured lint workflow, also run:

```bash
pre-commit run --all-files
```

## Risks and edge cases

- Numeric classification should follow pandas’ inferred dtype rather than coercing values. A mixed column containing numbers and text will have a non-numeric dtype and must be rejected; fully numeric CSV fields inferred as integers or floats remain accepted.
- Missing-column validation must precede dtype lookup to avoid a pandas `KeyError` and preserve the current descriptive `ValueError`.
- Both missing and non-numeric collectors must deduplicate repeated axis selections while retaining `(x_col, y_col)` order for deterministic messages.
- Nulls in an otherwise numeric column generally preserve a numeric dtype and should remain supported.
- Boolean, datetime, categorical, and object/string columns follow pandas’ dtype predicate semantics; no special conversions should be introduced because that would expand the task and alter existing data behavior.
