"""
Query Result Container

Defines the Result class to hold query execution results.
"""

from typing import List, Optional


class Result:
    """Container for SQL execution results."""

    def __init__(
        self,
        columns: Optional[List[str]] = None,
        rows: Optional[List[List]] = None,
        rowcount: int = 0,
    ):
        """
        Initialize a Result object.

        Args:
            columns: List of column names for SELECT results.
            rows: List of rows, where each row is a list of values.
            rowcount: Number of rows affected (for INSERT, DELETE, etc.).
        """
        self.columns = columns or []
        self.rows = rows or []
        self.rowcount = rowcount

    def __repr__(self) -> str:
        return f"Result(columns={self.columns!r}, rows={self.rows!r}, rowcount={self.rowcount})"
