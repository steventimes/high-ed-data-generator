Rust tests live with their crates so `cargo test --workspace` discovers them.

Current coverage includes:

- GPA clipping
- aid zero-inflation logic
- corruption operator semantics
- deterministic corruption seeding
- fragmentation score computation
- invariant checks that academic records do not change across variants
- infrastructure checks for schema verification labels and manifest output
