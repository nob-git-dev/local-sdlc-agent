"""Result container returned by SQL execution.

Holds column names and row data so callers can inspect query output
without reaching into storage internals.
"""

from typing import Any, List


class Result:
    """A query result with column names and rows.

    Attributes:
        columns: Column names in output order.
        rows: Row values as lists, one list per row.
    """

    def __init__(self, columns: List[str], rows: List[List[Any]], rows_affected: int = 0) -> None:
        self.columns = list(columns)
        self.rows = [list(row) for row in rows]
        self.rows_affected = rows_affected

    def __eq__(self, other: object) -> bool:
        """Compare two results by columns and rows."""
        if not isinstance(other, Result):
            return NotImplemented
        return (
            self.columns == other.columns
            and self.rows == other.rows
            and self.rows_affected == other.rows_affected
        )

    def __repr__(self) -> str:
        """Return a readable representation for debugging."""
        return "Result(columns={!r}, rows={!r}, rows_affected={!r})".format(
            self.columns, self.rows, self.rows_affected
        )
