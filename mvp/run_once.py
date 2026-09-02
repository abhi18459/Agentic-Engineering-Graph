

from pathlib import Path
import argparse
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixture"

TASK_PROMPT = """
You are solving one small coding task in the current repository.

Inspect the existing implementation and tests. Do not modify, create or
delete files in this invocation.

Return a plausible implementation attempt as:
1. A complete unified diff
2. A concise explanation of the changes
3. The test commands that should verify the changes

Do not claim that tests passed because this invocation is read only.

Task:
Improve starter_repo.plot_data.read_csv_data.

Requirements:
1. Preserve the current behavior when both requested columns exist.
2. If x_col or y_col is missing from the csv data, raise ValueError instead of allowing pandas to raise KeyError.
3. The error message must identify the missing column name or names.
4. Add pytest tests under tests/ for valid columns, a missing x column, and a missing y column.
5. Do not change the command line interface or unrelated plotting behavior.

Acceptance criteria:
- Existing tests continue to pass.
- The new missing-column tests pass.
- Valid input still returns the selected x and y values
- The error is a clear ValueError containing the missing column name.
"""

def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument(
		"--output",
		default="mvp/attempt.txt",
		help="File where the Codex response will be written"
	)
	args = parser.parse_args()

	if not FIXTURE.is_dir():
		print(f"Fixture directory not found: {FIXTURE}", file=sys.stderr)
		return 2

	output_path = Path(args.output)
	if not output_path.is_absolute():
		output_path = ROOT / output_path

	command = [
		"codex",
		"exec",
		"--cd",
		str(FIXTURE),
		"--sandbox",
		"read-only",
		"--ephemeral",
		TASK_PROMPT,
	]

	result = subprocess.run(
		command,
		cwd=ROOT,
		text=True,
		capture_output=True,
	)

	if result.returncode != 0:
		print(result.stderr.strip() or "Codex invocation failed", file=sys.stderr)
		return result.returncode

	output_path.parent.mkdir(parents=True, exist_ok=True)
	output_path.write_text(result.stdout, encoding="utf-8")

	print(f"Saved Codex response to {output_path}")
	return 0

if __name__ == "__main__":
	raise SystemExit(main())