from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from benchmark.analysis import analyze_run
from benchmark.evaluation.metrics import VARIANT_NAMES
from benchmark.evaluation.pipeline import evaluate_text_to_sql_outputs
from benchmark.questions import DEFAULT_REGISTRY, load_questions, resolve_questions
from benchmark.text_to_sql.openai_client import OpenAiSqlGenerator
from benchmark.text_to_sql.runner import (
    TextToSqlTarget,
    _validate_generated_results_directory,
    require_publishable_targets,
    resolve_targets,
    run_text_to_sql_experiment,
    write_results,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmark",
        description="Run higher-education fragmentation benchmark stages.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    analyze = commands.add_parser("analyze", help="Run reference SQL across variants.")
    _add_run_and_registry(analyze)
    analyze.add_argument("--query-id", action="append", default=[])
    analyze.set_defaults(handler=_run_analysis)

    generate = commands.add_parser(
        "text-to-sql",
        help="Generate and validate SQL with an OpenAI model.",
    )
    _add_run_and_registry(generate)
    generate.add_argument("--question", action="append", default=[])
    generate.add_argument("--variant", action="append")
    generate.add_argument("--target", action="append", default=[])
    generate.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5"))
    generate.add_argument("--max-retries", type=int, default=2)
    generate.add_argument("--output", type=Path)
    generate.add_argument("--generated-results-dir", type=Path)
    generate.set_defaults(handler=_run_text_to_sql)

    evaluate = commands.add_parser(
        "evaluate",
        help="Evaluate generated SQL result sets.",
    )
    _add_run_and_registry(evaluate)
    evaluate.add_argument("--generated-results-dir", type=Path)
    evaluate.add_argument("--output-dir", type=Path)
    evaluate.add_argument("--query-id", action="append", default=[])
    evaluate.add_argument("--variant", action="append")
    evaluate.add_argument("--target", action="append", default=[])
    evaluate.add_argument("--plot-format", default="png,pdf")
    evaluate.add_argument("--strict", action="store_true")
    evaluate.set_defaults(handler=_run_evaluation)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


def _add_run_and_registry(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", type=Path, default=Path("artifacts/runs/local"))
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)


def _run_analysis(args: argparse.Namespace) -> int:
    questions = _filter_questions(load_questions(args.registry), args.query_id)
    for label, path in analyze_run(args.run_dir, questions).items():
        print(f"{label}: {path}")
    return 0


def _run_text_to_sql(args: argparse.Namespace) -> int:
    questions = resolve_questions(
        registry_path=args.registry,
        inline_questions=args.question,
    )
    variants = args.variant or list(VARIANT_NAMES)
    targets = resolve_targets(
        run_dir=args.run_dir,
        variants=variants,
        explicit_targets=args.target,
    )
    # 在构造需要 API key 的客户端前拒绝 evaluation 无法消费的目标集合。
    require_publishable_targets(targets)
    generated_dir = args.generated_results_dir or (
        args.run_dir / "metrics" / "text_to_sql_generated_results"
    )
    generated_dir = _validate_generated_results_directory(
        generated_dir, targets=targets, run_dirs=[args.run_dir]
    )
    output = args.output or args.run_dir / "metrics" / "text_to_sql_experiments.csv"
    output = _validate_text_to_sql_summary_output(
        output,
        run_dir=args.run_dir,
        generated_results_dir=generated_dir,
        targets=targets,
    )
    results = run_text_to_sql_experiment(
        questions=questions,
        targets=targets,
        generator=OpenAiSqlGenerator(args.model),
        model=args.model,
        max_retries=args.max_retries,
        generated_results_dir=generated_dir,
    )
    write_results(output, results)
    failed = [result for result in results if not result.success]
    print(f"text_to_sql_results: {output}")
    print(f"successful={len(results) - len(failed)} failed={len(failed)}")
    return 1 if failed else 0


def _validate_text_to_sql_summary_output(
    output: Path,
    *,
    run_dir: Path,
    generated_results_dir: Path,
    targets: list[TextToSqlTarget],
) -> Path:
    raw = os.fspath(output)
    if not raw or raw == "." or output == Path("/") or ".." in output.parts:
        raise ValueError("text-to-sql output must be a specific safe CSV path")
    if output.is_symlink():
        raise ValueError("text-to-sql output must not be a symbolic link")
    destination = output.resolve(strict=False)
    if output.exists() and output.is_dir():
        raise ValueError("text-to-sql output must be a file")

    canonical_run_dir = run_dir.resolve(strict=False)
    protected = {
        generated_results_dir.resolve(strict=False),
        canonical_run_dir / "variants",
        canonical_run_dir / "manifests",
        canonical_run_dir / "config_snapshot",
    }
    target_dirs = {target.variant_dir.resolve(strict=False) for target in targets}
    protected.update(target_dirs)
    for target_dir in target_dirs:
        if target_dir.parent.name == "variants":
            run_root = target_dir.parent.parent
            protected.update(
                {
                    run_root / "variants",
                    run_root / "manifests",
                    run_root / "config_snapshot",
                }
            )
    for path in protected:
        if (
            path == destination
            or path.is_relative_to(destination)
            or destination.is_relative_to(path)
        ):
            raise ValueError(
                "text-to-sql output must not overlap generated or benchmark inputs"
            )
    return destination


def _run_evaluation(args: argparse.Namespace) -> int:
    questions = _filter_questions(load_questions(args.registry), args.query_id)
    generated_dir = args.generated_results_dir or (
        args.run_dir / "metrics" / "text_to_sql_generated_results"
    )
    output_dir = args.output_dir or args.run_dir / "evaluation" / "text_to_sql"
    targets = resolve_targets(
        run_dir=args.run_dir,
        variants=args.variant or list(VARIANT_NAMES),
        explicit_targets=args.target,
    )
    plot_formats = [
        value.strip()
        for value in args.plot_format.split(",")
        if value.strip() and value.strip().lower() != "none"
    ]
    for label, path in evaluate_text_to_sql_outputs(
        run_dir=args.run_dir,
        questions=questions,
        generated_results_dir=generated_dir,
        output_dir=output_dir,
        plot_formats=plot_formats,
        strict=args.strict,
        targets=[(target.label, target.variant_dir) for target in targets],
    ).items():
        print(f"{label}: {path}")
    return 0


def _filter_questions(questions, requested_ids: list[str]):
    if not requested_ids:
        return questions
    selected = set(requested_ids)
    available = {question.question_id for question in questions}
    unknown = sorted(selected - available)
    if unknown:
        raise ValueError(f"unknown query IDs: {', '.join(unknown)}")
    filtered = [question for question in questions if question.question_id in selected]
    return filtered
