from pathlib import Path

import pandas as pd
import pytest

from starter_repo.plot_data import create_plot, read_csv_data


@pytest.fixture
def sample_csv(tmp_path: Path) -> Path:
    """Create a sample CSV file for testing."""
    df = pd.DataFrame({"x": [1.0, 2.0, 3.0, 4.0, 5.0], "y": [2.0, 4.0, 6.0, 8.0, 10.0]})
    file_path = tmp_path / "test.csv"
    df.to_csv(file_path, index=False)
    return file_path


def test_read_csv_data(sample_csv: Path) -> None:
    """Test reading data from CSV file."""
    x_data, y_data = read_csv_data(sample_csv, "x", "y")
    assert x_data == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert y_data == [2.0, 4.0, 6.0, 8.0, 10.0]


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
