//! A live portfolio in and out of the engine, as compact JSON (scripts/engine.py writes and reads it; symbols are
//! indexes in the round's data, dates are days since 1970-01-01).

use serde_json::{json, Value};

use crate::engine::{Order, Peer, Port, Pos, Trade};

fn f(v: &Value) -> f64 {
    v.as_f64().unwrap_or(f64::NAN)
}
fn of(v: &Value) -> Option<f64> {
    v.as_f64()
}
fn oi(v: &Value) -> Option<i32> {
    v.as_i64().map(|x| x as i32)
}

pub fn read(text: &str) -> Result<Port, String> {
    let v: Value = serde_json::from_str(text).map_err(|e| e.to_string())?;
    let mut p = Port::new(f(&v["cash"]));
    p.deposits = f(&v["deposits"]);
    for x in v["positions"].as_array().into_iter().flatten() {
        let a = x.as_array().ok_or("posição inválida")?;
        p.positions.insert(a[0].as_u64().ok_or("símbolo")? as usize, Pos {
            qty: f(&a[1]), entry: f(&a[2]), entry_date: oi(&a[3]).unwrap_or(0), cost_eur: f(&a[4]), stop: of(&a[5]),
            tp: of(&a[6]), checked: oi(&a[7]).unwrap_or(0),
        });
    }
    for x in v["pending"].as_array().into_iter().flatten() {
        let a = x.as_array().ok_or("ordem inválida")?;
        p.pending.push(Order {
            buy: a[0].as_bool().unwrap_or(false), sym: a[1].as_u64().ok_or("símbolo")? as usize, eur: a[2].as_f64().unwrap_or(0.0),
            stop: of(&a[3]), tp: of(&a[4]), fraction: a[5].as_f64().unwrap_or(1.0), reason: a[6].as_str().unwrap_or("").to_string(),
            reference: of(&a[7]), gap_max: of(&a[8]), created: oi(&a[9]).unwrap_or(0),
        });
    }
    for x in v["hist"].as_array().into_iter().flatten() {
        p.hist.push((oi(&x[0]).unwrap_or(0), f(&x[1])));
    }
    p.points = v["points"].as_i64().unwrap_or(0);
    p.peak = of(&v["peak"]);
    p.max_dd = v["max_dd"].as_f64().unwrap_or(0.0);
    p.brake_until = oi(&v["brake_until"]);
    p.brake_peak = of(&v["brake_peak"]);
    p.last_rebalance = oi(&v["last_rebalance"]);
    for x in v["realized"].as_array().into_iter().flatten() {
        p.realized.insert(oi(&x[0]).unwrap_or(0), f(&x[1]));
    }
    p.taxed = v["taxed"].as_array().into_iter().flatten().filter_map(oi).collect();
    for x in v["sold_today"].as_array().into_iter().flatten() {  // react: no buying back what was sold today
        p.trades.push(Trade { day: oi(&x[1]).unwrap_or(0), side: 1, sym: x[0].as_u64().map(|s| s as usize), qty: 0.0,
                              price: 0.0, eur: 0.0, pnl: 0.0, reason: String::new(), ret: None, held: None });
    }
    p.last_bench = v["last_bench"].as_f64().unwrap_or(f64::NAN);
    for x in v["league"].as_array().into_iter().flatten() {  // a meta-agent's view of the others (scripts/engine.py)
        let mut port = Port::new(f(&x["cash"]));
        for a in x["positions"].as_array().into_iter().flatten() {
            let a = a.as_array().ok_or("posição inválida")?;
            port.positions.insert(a[0].as_u64().ok_or("símbolo")? as usize, Pos {
                qty: f(&a[1]), entry: f(&a[2]), entry_date: oi(&a[3]).unwrap_or(0), cost_eur: f(&a[4]), stop: of(&a[5]),
                tp: of(&a[6]), checked: oi(&a[7]).unwrap_or(0),
            });
        }
        p.league.push(Peer {
            id: x["id"].as_str().unwrap_or("").to_string(), name: x["name"].as_str().unwrap_or("").to_string(),
            score: of(&x["score"]), weight: x["weight"].as_f64().unwrap_or(0.0), port,
            buys: x["buys"].as_array().into_iter().flatten().filter_map(|s| s.as_u64().map(|s| s as usize)).collect(),
        });
    }
    Ok(p)
}

pub fn write(p: &Port) -> String {
    let positions: Vec<Value> = p.positions.iter()
        .map(|(&s, x)| json!([s, x.qty, x.entry, x.entry_date, x.cost_eur, x.stop, x.tp, x.checked])).collect();
    let pending: Vec<Value> = p.pending.iter()
        .map(|o| json!([o.buy, o.sym, o.eur, o.stop, o.tp, o.fraction, o.reason, o.reference, o.gap_max, o.created])).collect();
    let realized: Vec<Value> = p.realized.iter().map(|(y, g)| json!([y, g])).collect();
    json!({
        "cash": p.cash, "positions": positions, "pending": pending, "realized": realized, "taxed": p.taxed,
        "last_rebalance": p.last_rebalance, "brake_until": p.brake_until, "brake_peak": p.brake_peak,
    }).to_string()
}

pub fn trades(ts: &[Trade]) -> String {
    let v: Vec<Value> = ts.iter()
        .map(|t| json!([t.day, t.side, t.sym, t.qty, t.price, t.eur, t.pnl, t.reason])).collect();
    Value::Array(v).to_string()
}
