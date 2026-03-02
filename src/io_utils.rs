use anyhow::{Context, Result};
use csv::WriterBuilder;
use serde::Serialize;
use std::fs;
use std::io::BufWriter;
use std::path::Path;

pub fn ensure_dir(path: &Path) -> Result<()> {
    fs::create_dir_all(path).with_context(|| format!("failed to create dir {}", path.display()))
}

pub fn write_csv<T: Serialize>(path: &Path, rows: &[T]) -> Result<()> {
    let mut writer = WriterBuilder::new()
        .has_headers(true)
        .from_path(path)
        .with_context(|| format!("failed to create csv {}", path.display()))?;
    for row in rows {
        writer.serialize(row)?;
    }
    writer.flush()?;
    Ok(())
}

pub fn write_json<T: Serialize>(path: &Path, value: &T, pretty: bool) -> Result<()> {
    let file =
        fs::File::create(path).with_context(|| format!("failed to create {}", path.display()))?;
    let writer = BufWriter::new(file);
    if pretty {
        serde_json::to_writer_pretty(writer, value)?;
    } else {
        serde_json::to_writer(writer, value)?;
    }
    Ok(())
}