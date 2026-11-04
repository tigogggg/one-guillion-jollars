//! The strategies are private (tigogggg/one-guillion-jollars-dados, checked out at ../agents): with that checkout the
//! engine is built with them (cfg ogj_receita), without it with an empty stand-in (src/strategies_stub.rs).
use std::path::Path;

fn main() {
    let recipe = Path::new(env!("CARGO_MANIFEST_DIR")).join("../agents/receita/engine/strategies.rs");
    println!("cargo:rustc-check-cfg=cfg(ogj_receita)");
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed={}", recipe.display());
    if recipe.is_file() {
        println!("cargo:rustc-cfg=ogj_receita");
    }
}
