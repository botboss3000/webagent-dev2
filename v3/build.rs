fn main() {
    println!("cargo:rustc-check-cfg=cfg(diagnostics_plugin)");
    println!("cargo:rerun-if-changed=plugins/diagnostics");
    if std::path::Path::new("plugins/diagnostics/mod.rs").is_file() {
        println!("cargo:rustc-cfg=diagnostics_plugin");
    }
}
