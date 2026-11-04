//! Stand-in for the strategies when the private recipe (agents/receita/engine/strategies.rs) is not checked out: the
//! engine builds and runs no strategy (SUPPORTED is empty), so scripts/engine.py leaves everything to Python.

use crate::engine::{Mkt, Order, Port, P};

pub const SUPPORTED: &[&str] = &[];

pub fn uses_region(_strategy: &str) -> bool {
    false
}

pub fn run(_strategy: &str, _port: &mut Port, _m: &Mkt, _p: &P, _region: i64) -> Vec<Order> {
    Vec::new()
}
