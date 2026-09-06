# Implementation plan

1. Update `starter_repo/plot_data.py`
   - In `read_csv_data`, retain the current `pd.read_csv` call and missing-column validation unchanged.
   - Immediately after the missing-column check and before `is_numeric_dtype` validation, check whether the DataFrame has zero rows.
   - Raise `ValueError("CSV contains no data rows")` when it does.
   - Leave numeric validation, returned list values, CLI argument handling, and plotting logic unchanged.

2. Update `tests/test_plot_data.py`
   - Add a header-only CSV fixture or create files within the individual tests using `tmp_path`.
   - Add a test where the header contains both requested columns (for example, `x,y`) and assert that `read_csv_data` raises `ValueError` with the exact or clearly matching message `CSV contains no data rows`.
   - Add a precedence test using a header-only CSV missing one requested column and assert that the existing exact error remains, such as `CSV is missing required column(s): missing_y`.
   - Retain the existing populated numeric test, which already verifies values are returned unchanged.
   - Retain the existing non-numeric and missing-column tests to guard the established validation behavior and ordering.

3. Validation commands

   ```bash
   pytest tests/test_plot_data.py
   pytest
   ruff check .
   ruff format --check .
   mypy starter_repo tests
   ```

4. Risks and edge cases
   - The empty-row check must follow missing-column validation; placing it earlier would violate the required error precedence.
   - It must precede numeric-type validation because pandas generally assigns ambiguous/non-numeric dtypes to header-only columns, which currently causes the misleading numeric-column error.
   - Define “no data rows” strictly as zero parsed rows. Do not reject populated rows merely because their values are blank or `NaN`.
   - A completely blank file may still raise pandas’ existing `EmptyDataError`; this task specifically targets parseable, header-only CSV files.
   - When both axes reference the same existing column, a header-only CSV should still produce the no-data-rows error rather than numeric validation.
 ## Human review amendment

Treat these instructions as required:

- Preserve missing-column validation as the first check.
- Check `df.empty` immediately after missing-column validation and before numeric-dtype validation.
- Add a regression test using a header-only CSV containing exactly `x,y`.
- Do not modify `create_plot()` or command-line parsing.
