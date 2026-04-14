from __future__ import annotations

from pathlib import Path
from typing import Any

from benchmark.evaluation.metrics import fragmentation_level_sort_key

BAR_COLORS = {
    "baseline": "#8d99ae",
    "low": "#f4a261",
    "medium": "#e76f51",
    "high": "#bc4749",
}

CAUSE_COLORS = {
    "missing_record": "#bc4749",
    "null_critical_field": "#f4a261",
    "join_failure": "#6a994e",
    "stale_record": "#577590",
    "semantic_mismatch": "#7b2cbf",
    "unknown": "#8d99ae",
}

CAUSE_ORDER = [
    "missing_record",
    "null_critical_field",
    "join_failure",
    "stale_record",
    "semantic_mismatch",
    "unknown",
]


def plot_miss_rate_bars(
    records: list[dict[str, Any]],
    output_path: Path | str,
    *,
    title: str,
    y_key: str = "miss_rate",
    y_label: str = "Missed students (%)",
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ordered = sorted(records, key=lambda row: fragmentation_level_sort_key(str(row["fragmentation_level"])))
    labels = [display_fragmentation_label(str(row["fragmentation_level"])) for row in ordered]
    values = [
        0.0 if row.get(y_key) in (None, "") else float(row[y_key]) * 100.0
        for row in ordered
    ]
    colors = [BAR_COLORS.get(str(row["fragmentation_level"]), "#8d99ae") for row in ordered]

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(7.5, 4.8))
    bars = ax.bar(labels, values, color=colors, width=0.62)
    ax.set_title(title)
    ax.set_ylabel(y_label)
    ax.set_xlabel("Fragmentation level")
    ax.set_ylim(0, max(values + [5.0]) * 1.18)
    ax.grid(axis="y", alpha=0.25)
    ax.set_axisbelow(True)

    for bar, row, value in zip(bars, ordered, values):
        if row.get(y_key) in (None, ""):
            annotation = "NA"
        else:
            fn = int(row.get("fn", 0))
            baseline_count = int(row.get("baseline_count", 0))
            annotation = f"{value:.1f}%\n{fn}/{baseline_count} missed"
        ax.annotate(
            annotation,
            xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
            xytext=(0, 5),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=8,
        )

    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def plot_cause_breakdown(
    records: list[dict[str, Any]],
    output_path: Path | str,
    *,
    title: str,
    y_label: str = "Missed students (count)",
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    filtered = [
        row
        for row in records
        if str(row["fragmentation_level"]) != "baseline"
    ]
    ordered = sorted(filtered, key=lambda row: fragmentation_level_sort_key(str(row["fragmentation_level"])))
    if not ordered:
        raise ValueError("No non-baseline records available for cause breakdown plot")

    labels = [display_fragmentation_label(str(row["fragmentation_level"])) for row in ordered]
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(7.5, 4.8))
    bottoms = [0.0 for _ in ordered]

    for cause in CAUSE_ORDER:
        values = [float(row.get(cause, 0) or 0) for row in ordered]
        ax.bar(
            labels,
            values,
            bottom=bottoms,
            label=display_cause_label(cause),
            color=CAUSE_COLORS[cause],
            width=0.62,
        )
        bottoms = [bottom + value for bottom, value in zip(bottoms, values)]

    ax.set_title(title)
    ax.set_ylabel(y_label)
    ax.set_xlabel("Fragmentation level")
    ax.grid(axis="y", alpha=0.25)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def display_fragmentation_label(level: str) -> str:
    mapping = {
        "baseline": "Baseline",
        "low": "Low",
        "medium": "Medium",
        "high": "High",
    }
    return mapping.get(level, level.replace("_", " ").title())


def display_cause_label(cause: str) -> str:
    return cause.replace("_", " ").title()
