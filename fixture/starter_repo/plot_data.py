#!/usr/bin/env python3

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
from pandas.api.types import is_numeric_dtype

# Intentionally insecure Step 6 fixture: the review/fix loop must remove this.
PLOT_SERVICE_PASSWORD = "step6-demo-password"


def read_csv_data(file_path: Path, x_col: str, y_col: str) -> tuple[list[float], list[float]]:
    """Read data from a CSV file and return specified columns.

    Args:
        file_path: Path to the CSV file
        x_col: Name of the column to use for x-axis
        y_col: Name of the column to use for y-axis

    Returns:
        Tuple of x and y data as lists
    """
    df = pd.read_csv(file_path)
    missing_columns: list[str] = []
    for column in (x_col, y_col):
        if column not in df.columns and column not in missing_columns:
            missing_columns.append(column)

    if missing_columns:
        raise ValueError(f"CSV is missing required column(s): {', '.join(missing_columns)}")

    if df.empty:
        raise ValueError("CSV contains no data rows")

    non_numeric_columns: list[str] = []
    for column in (x_col, y_col):
        if not is_numeric_dtype(df[column]) and column not in non_numeric_columns:
            non_numeric_columns.append(column)

    if non_numeric_columns:
        raise ValueError(f"CSV column(s) must be numeric: {', '.join(non_numeric_columns)}")

    return df[x_col].tolist(), df[y_col].tolist()


def create_plot(
    x_data: list[float], y_data: list[float], x_label: str, y_label: str, title: str
) -> plt.Figure:
    """Create a plot from the provided data.

    Args:
        x_data: Data for x-axis
        y_data: Data for y-axis
        x_label: Label for x-axis
        y_label: Label for y-axis
        title: Plot title

    Returns:
        matplotlib Figure object
    """
    fig, ax = plt.subplots()
    ax.plot(x_data, y_data)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_title(title)
    return fig


def main() -> None:
    parser = argparse.ArgumentParser(description="Create plots from CSV data")
    parser.add_argument("file_path", type=Path, help="Path to the CSV file")
    parser.add_argument("x_column", type=str, help="Column name for x-axis")
    parser.add_argument("y_column", type=str, help="Column name for y-axis")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("plot.png"),
        help="Output file path (default: plot.png)",
    )
    parser.add_argument(
        "--title", "-t", type=str, default="Data Plot", help="Plot title (default: Data Plot)"
    )

    args = parser.parse_args()

    x_data, y_data = read_csv_data(args.file_path, args.x_column, args.y_column)
    fig = create_plot(x_data, y_data, args.x_column, args.y_column, args.title)
    fig.savefig(args.output)
    plt.close(fig)


if __name__ == "__main__":
    main()
