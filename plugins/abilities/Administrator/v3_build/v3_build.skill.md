# V3 Build

Use for compiling/testing the repository's v3 Rust application and building its development image. Run v3_build_status first in the execution environment that will do the build. A plugin supplies structured operations, not missing infrastructure.

Native tasks require Rust 1.90, Cargo, a C compiler for bundled SQLite, dependency-download access (or a populated cache), and writable temporary/cache space. Image builds require a reachable Docker daemon and registry access; the pinned Dockerfile supplies Rust and runtime dependencies. Builds can consume substantial disk, memory, and time. Cargo and Docker execute repository build code, so grant this ability only to trusted agents/callers.

No arbitrary commands, paths, image tags, installation, Docker socket mounting, service lifecycle changes, or deployments. This ability runs on the WebAgent server, not a user's device or an SSH host. If infrastructure is missing, report the exact prerequisite; arrange a trusted build host separately rather than widening privileges. A successful image build is not deployment or a working chat verification.
