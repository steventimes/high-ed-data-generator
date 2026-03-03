"""
load_data.py
-----------------

This script reads the output of the high‑ed data generator and loads
its CSV and JSON files into a DuckDB database.  It relies on
`pandas` for parsing and uses DuckDB’s Python API for storage.

Usage example:

    python load_data.py --input ./out --db edu.duckdb --clear

The script walks the `--input` directory and its `terms/` subfolders,
creating a single table for each file type.  Subsequent runs will
append to existing tables unless `--clear` is specified, in which
case existing tables are dropped and recreated.

Composite keys: if a column contains values separated by the `|`
character (e.g. `S001|CS101`), the script splits the field into
multiple new columns named `<col>_1`, `<col>_2`, etc.  The original
column is preserved.

"""

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import duckdb
import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load generator outputs into DuckDB")
    parser.add_argument(
        "--input",
        type=str,
        required=True,
        help="Path to the generator output directory (e.g. ./out)",
    )
    parser.add_argument(
        "--db",
        type=str,
        required=True,
        help="Path to the DuckDB database file.  Use :memory: for an in‑memory DB",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Drop and recreate existing tables before loading",
    )
    return parser.parse_args()


def split_composite_keys(df: pd.DataFrame) -> pd.DataFrame:
    """Detects columns with values containing '|' and splits them into separate parts.

    For each column that contains at least one pipe character, a set of
    new columns named `<col>_1`, `<col>_2`, … is appended to the
    DataFrame containing the split parts.  The original column is
    retained.  Columns that are not string‑typed are ignored.

    Args:
        df: Input DataFrame

    Returns:
        DataFrame with additional split columns (modifies in place).
    """
    for col in df.select_dtypes(include=["object"]).columns:
        if df[col].astype(str).str.contains("|", regex=False).any():
            # Determine max number of parts in this column
            max_parts = (
                df[col]
                .astype(str)
                .dropna()
                .apply(lambda x: x.count("|") + 1)
                .max()
            )
            # Split into new columns
            new_cols = df[col].astype(str).str.split("|", expand=True, n=int(max_parts - 1))
            for i in range(max_parts):
                part_col = f"{col}_{i+1}"
                df[part_col] = new_cols[i]
    return df


def load_json_file(path: Path) -> pd.DataFrame:
    """Read a JSON file that contains either an array of objects or a single object.

    The JSON outputs from the generator are arrays of objects (pretty
    printed or compact).  This function loads the entire file and
    converts it to a pandas DataFrame.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # If it's a single object, wrap it in a list
    if isinstance(data, dict):
        data = [data]
    df = pd.json_normalize(data)
    return df


def load_csv_file(path: Path) -> pd.DataFrame:
    """Read a CSV file into a pandas DataFrame using DuckDB’s CSV parser via pandas.

    Using pandas avoids needing to specify types up front; DuckDB
    will handle casting on import.
    """
    return pd.read_csv(path)


def table_name_for_file(path: Path) -> str:
    """Derive a table name from a file path.

    Example: `/tmp/out/students_master.json` -> `students_master`.  The
    parent term directory is not included in the name.  File suffixes
    like `.csv` and `.json` are removed.
    """
    return path.stem


def append_dataframe_to_table(con: duckdb.DuckDBPyConnection, df: pd.DataFrame, table: str):
    """Append a DataFrame to a DuckDB table.  Create the table if it does not exist.

    DuckDB will automatically create the schema based on the pandas
    DataFrame’s dtypes.  Subsequent inserts will attempt to cast
    incoming data to the existing schema.
    """
    # Register the DataFrame as a temporary view
    con.register("tmp_df", df)
    # Create table if it does not exist
    # We need to quote the table name because it may contain uppercase letters
    create_sql = f"CREATE TABLE IF NOT EXISTS \"{table}\" AS SELECT * FROM tmp_df LIMIT 0"
    con.execute(create_sql)
    # Insert data
    insert_sql = f"INSERT INTO \"{table}\" SELECT * FROM tmp_df"
    con.execute(insert_sql)
    con.unregister("tmp_df")


def drop_table_if_exists(con: duckdb.DuckDBPyConnection, table: str):
    """Drop a table if it exists in the database."""
    con.execute(f"DROP TABLE IF EXISTS \"{table}\"")


def process_file(con: duckdb.DuckDBPyConnection, path: Path, clear: bool):
    """Load a single CSV or JSON file into DuckDB."""
    table = table_name_for_file(path)
    if clear:
        drop_table_if_exists(con, table)
    if path.suffix.lower() == ".json":
        df = load_json_file(path)
    else:
        df = load_csv_file(path)
    # Split any composite keys
    df = split_composite_keys(df)
    append_dataframe_to_table(con, df, table)


def process_directory(con: duckdb.DuckDBPyConnection, input_dir: Path, clear: bool):
    """Walk the generator output directory and load each file.

    This function loads top‑level files (e.g. students_master.json,
    identity_crosswalk.csv, metadata.json) as well as files under the
    `terms/` subdirectory.  Term subdirectories are expected to
    contain CSV and JSON files which will be appended to the same
    tables across terms.
    """
    # Top‑level files
    for child in input_dir.iterdir():
        if child.is_file() and child.suffix.lower() in {".csv", ".json"}:
            # Skip metadata.json (not loaded into DB)
            if child.name == "metadata.json":
                continue
            process_file(con, child, clear)
    # Term files
    terms_dir = input_dir / "terms"
    if terms_dir.exists() and terms_dir.is_dir():
        for term_subdir in sorted(terms_dir.iterdir()):
            if not term_subdir.is_dir():
                continue
            for file in term_subdir.iterdir():
                if file.is_file() and file.suffix.lower() in {".csv", ".json"}:
                    process_file(con, file, clear=False)


def main() -> None:
    args = parse_args()
    input_dir = Path(args.input).resolve()
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")
    # Establish DuckDB connection
    con = duckdb.connect(args.db)
    # Enable JSON extension for storing receipts later
    con.execute("PRAGMA create_or_replace_journal_mode = 'wal'")
    # Process files
    process_directory(con, input_dir, args.clear)
    # Commit and close
    con.close()
    print(f"Loaded data from {input_dir} into {args.db}")


if __name__ == "__main__":
    main()