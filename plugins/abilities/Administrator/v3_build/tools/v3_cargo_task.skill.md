# v3_cargo_task

Choose check, test, or release. Uses the fixed v3 manifest, --locked, two jobs, and an isolated temporary target directory; never rewrites the lockfile. Requires Rust 1.90 and C compiler. Timeout is bounded; inspect exit_code and timed_out, not just output. Artifacts are temporary and removed after the operation. Cargo may download dependencies to its usual cache and executes repository code. No test filters or arbitrary flags.
