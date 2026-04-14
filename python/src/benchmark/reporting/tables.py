from __future__ import annotations

from pathlib import Path
from typing import Any

TABLE_COLUMNS = [
    "variant",
    "fragmentation_score",
    "identified_student_count",
    "missed_count",
]


def write_results_tables(records: list[dict[str, Any]], markdown_path: Path | str, latex_path: Path | str) -> None:
    markdown = render_markdown_table(records)
    latex = render_latex_table(records)
    markdown_file = Path(markdown_path)
    latex_file = Path(latex_path)
    markdown_file.parent.mkdir(parents=True, exist_ok=True)
    latex_file.parent.mkdir(parents=True, exist_ok=True)
    markdown_file.write_text(markdown, encoding="utf-8")
    latex_file.write_text(latex, encoding="utf-8")


def render_markdown_table(records: list[dict[str, Any]]) -> str:
    header = "| Variant | Fragmentation score | Identified students | Missed vs baseline |"
    divider = "|---|---:|---:|---:|"
    rows = [header, divider]
    for record in records:
        rows.append(
            "| {variant} | {score:.4f} | {identified} | {missed} |".format(
                variant=record["variant"],
                score=float(record["fragmentation_score"]),
                identified=int(record["identified_student_count"]),
                missed=int(record["missed_count"]),
            )
        )
    return "\n".join(rows) + "\n"


def render_latex_table(records: list[dict[str, Any]]) -> str:
    lines = [
        "\\begin{tabular}{lrrr}",
        "\\toprule",
        "Variant & Fragmentation score & Identified students & Missed vs baseline \\\\",
        "\\midrule",
    ]
    for record in records:
        lines.append(
            "{variant} & {score:.4f} & {identified} & {missed} \\\\".format(
                variant=latex_escape(str(record["variant"])),
                score=float(record["fragmentation_score"]),
                identified=int(record["identified_student_count"]),
                missed=int(record["missed_count"]),
            )
        )
    lines.extend(["\\bottomrule", "\\end{tabular}", ""])
    return "\n".join(lines)


def latex_escape(value: str) -> str:
    return value.replace("_", "\\_")
