import sys
from pathlib import Path

import pandas as pd
import pytest

from starter_repo.plot_data import create_plot, main, read_csv_data


@pytest.fixture
def sample_csv(tmp_path: Path) -> Path:
    """Create a sample CSV file for testing."""
    df = pd.DataFrame({"x": [1.0, 2.0, 3.0, 4.0, 5.0], "y": [2.0, 4.0, 6.0, 8.0, 10.0]})
    file_path = tmp_path / "test.csv"
    df.to_csv(file_path, index=False)
    return file_path


@pytest.fixture
def non_numeric_csv(tmp_path: Path) -> Path:
    """Create a CSV with numeric and non-numeric columns."""
    df = pd.DataFrame({"category": ["a", "b"], "label": ["first", "second"], "value": [1, 2]})
    file_path = tmp_path / "non_numeric.csv"
    df.to_csv(file_path, index=False)
    return file_path


def test_read_csv_data(sample_csv: Path) -> None:
    """Test reading data from CSV file."""
    x_data, y_data = read_csv_data(sample_csv, "x", "y")
    assert x_data == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert y_data == [2.0, 4.0, 6.0, 8.0, 10.0]


@pytest.mark.parametrize(
    "csv_value",
    ["NaN", "inf", "-inf"],
    ids=["nan", "positive_inf", "negative_inf"],
)
@pytest.mark.parametrize("affected_column", ["horizontal", "vertical"])
def test_read_csv_data_rejects_non_finite_values_in_either_column(
    tmp_path: Path, csv_value: str, affected_column: str
) -> None:
    """Test that each kind of non-finite value is rejected in either selected column."""
    x_value = csv_value if affected_column == "horizontal" else "1.0"
    y_value = csv_value if affected_column == "vertical" else "2.0"
    file_path = tmp_path / "non_finite.csv"
    file_path.write_text(f"horizontal,vertical\n{x_value},{y_value}\n")

    with pytest.raises(ValueError) as exc_info:
        read_csv_data(file_path, "horizontal", "vertical")
    message = str(exc_info.value)
    assert message == f"CSV column(s) contain non-finite values: {affected_column}"
    assert message.count(affected_column) == 1


def test_read_csv_data_reports_each_non_finite_column_once(tmp_path: Path) -> None:
    """Test that all affected columns are reported once in selection order."""
    file_path = tmp_path / "two_non_finite_columns.csv"
    file_path.write_text("horizontal,vertical\nNaN,-inf\n")

    with pytest.raises(ValueError) as exc_info:
        read_csv_data(file_path, "horizontal", "vertical")
    message = str(exc_info.value)
    assert message == "CSV column(s) contain non-finite values: horizontal, vertical"
    assert message.count("horizontal") == 1
    assert message.count("vertical") == 1


def test_read_csv_data_reports_shared_non_finite_column_once(tmp_path: Path) -> None:
    """Test that a non-finite column selected twice is not reported twice."""
    file_path = tmp_path / "shared_non_finite_column.csv"
    file_path.write_text("horizontal\ninf\n")

    with pytest.raises(ValueError) as exc_info:
        read_csv_data(file_path, "horizontal", "horizontal")
    message = str(exc_info.value)
    assert message == "CSV column(s) contain non-finite values: horizontal"
    assert message.count("horizontal") == 1


def test_read_csv_data_with_no_data_rows(tmp_path: Path) -> None:
    """Test that a header-only CSV raises a descriptive ValueError."""
    file_path = tmp_path / "header_only.csv"
    file_path.write_text("x,y\n")

    with pytest.raises(ValueError, match="^CSV contains no data rows$"):
        read_csv_data(file_path, "x", "y")


def test_missing_column_error_precedes_empty_data_validation(tmp_path: Path) -> None:
    """Test that missing-column validation precedes the no-data-rows check."""
    file_path = tmp_path / "header_only_missing_column.csv"
    file_path.write_text("x\n")

    with pytest.raises(ValueError, match=r"^CSV is missing required column\(s\): missing_y$"):
        read_csv_data(file_path, "x", "missing_y")


def test_read_csv_data_with_one_non_numeric_column(non_numeric_csv: Path) -> None:
    """Test that one non-numeric selected column is identified once."""
    with pytest.raises(ValueError) as exc_info:
        read_csv_data(non_numeric_csv, "value", "category")
    message = str(exc_info.value)
    assert message == "CSV column(s) must be numeric: category"
    assert message.count("category") == 1


def test_read_csv_data_with_two_non_numeric_columns(non_numeric_csv: Path) -> None:
    """Test that two non-numeric selected columns are each identified once."""
    with pytest.raises(ValueError) as exc_info:
        read_csv_data(non_numeric_csv, "category", "label")
    message = str(exc_info.value)
    assert message == "CSV column(s) must be numeric: category, label"
    assert message.count("category") == 1
    assert message.count("label") == 1


def test_read_csv_data_same_non_numeric_column_for_both_axes(non_numeric_csv: Path) -> None:
    """Test that a shared non-numeric column is identified without duplication."""
    with pytest.raises(ValueError) as exc_info:
        read_csv_data(non_numeric_csv, "category", "category")
    message = str(exc_info.value)
    assert message == "CSV column(s) must be numeric: category"
    assert message.count("category") == 1


def test_read_csv_data_missing_x_column(sample_csv: Path) -> None:
    """Test that a missing x column raises a descriptive ValueError."""
    with pytest.raises(ValueError, match="missing_x"):
        read_csv_data(sample_csv, "missing_x", "y")


def test_read_csv_data_missing_y_column(sample_csv: Path) -> None:
    """Test that a missing y column raises a descriptive ValueError."""
    with pytest.raises(ValueError, match="missing_y"):
        read_csv_data(sample_csv, "x", "missing_y")


def test_read_csv_data_missing_both_columns(sample_csv: Path) -> None:
    """Test that both missing requested columns are identified."""
    with pytest.raises(ValueError) as exc_info:
        read_csv_data(sample_csv, "missing_x", "missing_y")
    message = str(exc_info.value)
    assert "missing_x" in message
    assert "missing_y" in message


def test_read_csv_data_same_missing_column_for_both_axes(sample_csv: Path) -> None:
    """Test that a shared missing column is identified without duplication."""
    missing_column = "absent"
    with pytest.raises(ValueError) as exc_info:
        read_csv_data(sample_csv, missing_column, missing_column)
    message = str(exc_info.value)
    assert message.count(missing_column) == 1


def test_missing_column_error_precedes_non_numeric_validation(non_numeric_csv: Path) -> None:
    """Test that missing-column validation retains precedence."""
    with pytest.raises(ValueError) as exc_info:
        read_csv_data(non_numeric_csv, "missing_x", "category")
    assert str(exc_info.value) == "CSV is missing required column(s): missing_x"


def test_create_plot() -> None:
    """Test plot creation."""
    x_data = [1.0, 2.0, 3.0]
    y_data = [2.0, 4.0, 6.0]
    fig = create_plot(x_data, y_data, "X", "Y", "Test Plot")
    assert fig is not None
    # Basic check that the figure contains the expected elements
    ax = fig.axes[0]
    assert ax.get_xlabel() == "X"
    assert ax.get_ylabel() == "Y"
    assert ax.get_title() == "Test Plot"


def test_create_plot_rejects_mismatched_data_lengths() -> None:
    """Test that mismatched x and y data lengths are rejected."""
    with pytest.raises(ValueError, match="^x_data and y_data must have the same length$"):
        create_plot([1.0, 2.0], [3.0], "X", "Y", "Test Plot")


@pytest.mark.parametrize(
    "non_finite_value",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "positive_inf", "negative_inf"],
)
@pytest.mark.parametrize("affected_axis", ["x_data", "y_data"])
def test_create_plot_rejects_non_finite_values_on_either_axis(
    non_finite_value: float, affected_axis: str
) -> None:
    """Test that each kind of non-finite value is rejected on either plot axis."""
    x_data = [1.0, 2.0]
    y_data = [3.0, 4.0]
    if affected_axis == "x_data":
        x_data[1] = non_finite_value
    else:
        y_data[1] = non_finite_value

    with pytest.raises(ValueError) as exc_info:
        create_plot(x_data, y_data, "X", "Y", "Test Plot")
    message = str(exc_info.value)
    assert message == f"Plot axis data contains non-finite values: {affected_axis}"
    assert message.count(affected_axis) == 1


def test_create_plot_reports_each_non_finite_axis_once() -> None:
    """Test that both affected axes are each reported once."""
    with pytest.raises(ValueError) as exc_info:
        create_plot([float("nan")], [float("inf")], "X", "Y", "Test Plot")
    message = str(exc_info.value)
    assert message == "Plot axis data contains non-finite values: x_data, y_data"
    assert message.count("x_data") == 1
    assert message.count("y_data") == 1


def test_main_writes_plot_file(
    sample_csv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test the existing command-line path and generated output file."""
    output_path = tmp_path / "command_line_plot.png"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "plot_data.py",
            str(sample_csv),
            "x",
            "y",
            "--output",
            str(output_path),
            "--title",
            "CLI Plot",
        ],
    )

    main()

    assert output_path.is_file()
