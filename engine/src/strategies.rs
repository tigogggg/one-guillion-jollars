//! The strategies (agents.s_*), ported one at a time; `supported` lists the ones the engine runs.

use crate::engine::{due, rebalance, Mkt, Order, Port, P};

pub const SUPPORTED: &[&str] = &["foguetao"];

/// Strategies that trade only ETFs or other agents' portfolios (agents.NO_REGION).
const NO_REGION: &[&str] = &["indexador", "guardiao", "rotador", "pendulo", "joalheiro", "conselho", "maestro"];

pub fn uses_region(strategy: &str) -> bool {
    !NO_REGION.contains(&strategy)
}

/// The day's stocks the strategy sees (agents.in_region: the region's, plus held ones elsewhere), in order.
fn view<'a>(port: &'a Port, m: &'a Mkt, region: i64) -> impl Iterator<Item = usize> + 'a {
    let rows = &m.data.stocks;
    rows.range(m.d).filter(move |&r| {
        if region == 0 {
            return true;
        }
        let s = rows.sym[r] as usize;
        let us = m.data.symbols[s].is_us;
        let inside = if region == 1 { us } else { !us };
        inside || port.positions.contains_key(&s)
    })
}

pub fn run(strategy: &str, port: &mut Port, m: &Mkt, p: &P, region: i64) -> Vec<Order> {
    match strategy {
        "foguetao" => foguetao(port, m, p, region),
        other => panic!("estratégia ainda não portada para Rust: {other}"),
    }
}

/// agents.s_foguetao: the n strongest trends in stage 2, rebalanced every `cada` sessions.
fn foguetao(port: &mut Port, m: &Mkt, p: &P, region: i64) -> Vec<Order> {
    if !due(port, m, p.req("cada")) {
        return Vec::new();
    }
    let rows = &m.data.stocks;
    let mut ranked: Vec<usize> = view(port, m, region).filter(|&r| rows.stage2[r]).collect();
    ranked.sort_by(|&a, &b| rows.trend[b].partial_cmp(&rows.trend[a]).unwrap_or(std::cmp::Ordering::Equal));
    let ranked: Vec<usize> = ranked.into_iter().map(|r| rows.sym[r] as usize).collect();
    let n = p.req("n") as usize;
    let top: Vec<usize> = ranked.iter().take(n).copied().collect();
    let keep: Vec<usize> = ranked.iter().take(2 * n).copied().collect();
    let stop = p.get("stop_pct");
    rebalance(port, m, &top, m.exposure() / n as f64, &keep, stop, "top tendência")
}
