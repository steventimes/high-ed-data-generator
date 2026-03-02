mod args;
mod catalogs;
mod generator;
mod io_utils;
mod models;
mod term;

use anyhow::Result;
use args::Args;
use clap::Parser;
use generator::generate;
use term::build_term_sequence;

fn main() -> Result<()> {
    let args = Args::parse();
    let terms = build_term_sequence(&args.start_term, args.terms)?;
    generate(&args, &terms)
}