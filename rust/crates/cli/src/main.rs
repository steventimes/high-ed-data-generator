use anyhow::Result;
use clap::{Parser, Subcommand};
use fragmentation_infrastructure::{execute_run, RunOptions};
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(name = "fragmentation-cli")]
#[command(about = "Generate the two-table fragmentation benchmark artifacts.")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    Generate(GenerateArgs),
}

#[derive(Debug, Parser)]
struct GenerateArgs {
    #[arg(long, default_value = "configs/schema_registry.yaml")]
    schema: PathBuf,
    #[arg(long, default_value = "configs/experiment.yaml")]
    experiment: PathBuf,
    #[arg(long, default_value = "artifacts/runs")]
    out_root: PathBuf,
    #[arg(long)]
    run_id: Option<String>,
    #[arg(long)]
    overwrite: bool,
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Generate(args) => {
            let completed = execute_run(&RunOptions {
                schema_path: args.schema,
                experiment_path: args.experiment,
                out_root: args.out_root,
                run_id: args.run_id,
                overwrite: args.overwrite,
            })?;
            println!("{}", completed.run_dir.display());
        }
    }
    Ok(())
}
