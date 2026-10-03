# v3_cargo_task

Run a fixed locked Cargo task in the current isolated V3 workspace through Rust-approved worker dispatch. It cannot choose a host checkout, Docker arguments, arbitrary shell command, or production target. Inspect the native schema for the supported task values. Coordinate shared build inputs when checking live source; workspace checks cannot prove the deployed image is current.
