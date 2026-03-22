# Generator Modules (`src/`)

Rust source modules for synthetic higher-ed data generation.

## Module Map

- `main.rs`: CLI entrypoint; parses args and starts generation.
- `args.rs`: CLI options and generator knobs.
- `term.rs`: term parsing/sequence helpers (`YYYYFA`, `YYYYSU`, etc.).
- `catalogs.rs`: major/subject catalogs and weighted samplers.
- `models.rs`: output row structs (slim/wide variants).
- `generator.rs`: core data generation logic and file writing.
- `io_utils.rs`: CSV/JSON writing helpers.

## Output Behavior

The generator supports:

- schema output mode: `slim`, `wide`, `both`
- term code style in wide outputs: `packed`, `split`, `both`
- missingness/linkage controls for LMS, aid, and crosswalk quality

See `../higher_ed_schema.md` for field-level schema definitions and `../run.sh` for runtime defaults.
