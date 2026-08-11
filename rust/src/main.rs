use anyhow::Result;
use clap::Parser;
use high_ed_data_generator::output::{execute_run, RunOptions};
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(about = "Generate deterministic higher-education fragmentation benchmark data")]
struct Cli {
    #[arg(long, default_value = "configs/benchmark.yaml")]
    config: PathBuf,
    #[arg(long, default_value = "artifacts/runs/local")]
    output: PathBuf,
    #[arg(long)]
    overwrite: bool,
}

fn main() -> Result<()> {
    let args = Cli::parse();
    let run_dir = execute_run(&RunOptions {
        config_path: args.config,
        output_dir: args.output,
        overwrite: args.overwrite,
    })?;
    println!("{}", run_dir.display());
    Ok(())
}
