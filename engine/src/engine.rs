//! The agent's portfolio and the day's bookkeeping, a line-by-line port of scripts/agents.py (the Python version is
//! the reference: the parity tests require the same trades, to the cent).

use std::collections::{BTreeMap, HashMap};

use indexmap::IndexMap;

use crate::data::{Data, Rows, F, EXPOSURE};
use crate::pyround::{py_max, py_min, py_sum, round_int, round_to, truthy};

pub const MIN_ORDER_EUR: f64 = 20.0;
pub const TAX_RATE: f64 = 0.28;
pub const PROFIT_BONUS: i64 = 2;
pub const DD_FREE: f64 = 0.10;
pub const DD_PENALTY: f64 = 50.0;
pub const BANKRUPTCY: f64 = 0.5;
pub const TARGET_VOL: f64 = 0.02;
pub const SECTOR_CAP: f64 = 0.40;
pub const DD_BRAKE: f64 = 0.20;
pub const BRAKE_DAYS: i32 = 14;
pub const PENDING_MAX_SESSIONS: i64 = 5;
pub const SUPPORT_MAX_RISK: f64 = 0.15;
pub const EARNINGS_AVOID_DAYS: i64 = 5;

/// Parameters in force (agents.effective for one zone): numbers, booleans as 0/1.
#[derive(Clone, Default)]
pub struct P(pub HashMap<String, f64>);

impl P {
    pub fn get(&self, k: &str) -> Option<f64> {
        self.0.get(k).copied()
    }
    /// Python's `p.get(k)` truthiness.
    pub fn on(&self, k: &str) -> bool {
        truthy(self.get(k))
    }
    pub fn req(&self, k: &str) -> f64 {
        self.get(k).unwrap_or_else(|| panic!("parâmetro em falta: {k}"))
    }
}

#[derive(Clone, Debug)]
pub struct Order {
    pub buy: bool,
    pub sym: usize,
    pub eur: f64,
    pub stop: Option<f64>,
    pub tp: Option<f64>,
    pub fraction: f64,
    pub reason: String,
    pub reference: Option<f64>,
    pub gap_max: Option<f64>,
    pub created: i32,
}

pub fn order_buy(sym: usize, eur: f64, reason: &str, stop: Option<f64>, tp: Option<f64>) -> Order {
    Order { buy: true, sym, eur: round_to(eur, 2), stop, tp, fraction: 1.0, reason: reason.to_string(), reference: None, gap_max: None, created: 0 }
}

pub fn order_sell(sym: usize, reason: &str) -> Order {
    Order { buy: false, sym, eur: 0.0, stop: None, tp: None, fraction: 1.0, reason: reason.to_string(), reference: None, gap_max: None, created: 0 }
}

#[derive(Clone, Debug)]
pub struct Pos {
    pub qty: f64,
    pub entry: f64,
    pub entry_date: i32,
    pub cost_eur: f64,
    pub stop: Option<f64>,
    pub tp: Option<f64>,
    pub checked: i32,
}

#[derive(Clone, Debug)]
pub struct Trade {
    pub day: i32,
    /// 0 buy, 1 sell, 2 tax
    pub side: u8,
    pub sym: Option<usize>,
    pub qty: f64,
    pub price: f64,
    pub eur: f64,
    pub pnl: f64,
    pub reason: String,
    pub ret: Option<f64>,
    pub held: Option<i64>,
}

#[derive(Clone, Default)]
pub struct Port {
    pub cash: f64,
    pub deposits: f64,
    pub positions: IndexMap<usize, Pos>,
    pub pending: Vec<Order>,
    pub hist: Vec<(i32, f64)>,
    pub points: i64,
    pub peak: Option<f64>,
    pub max_dd: f64,
    pub brake_until: Option<i32>,
    pub brake_peak: Option<f64>,
    pub last_rebalance: Option<i32>,
    pub realized: BTreeMap<i32, f64>,
    pub taxed: Vec<i32>,
    pub zone_stats: [(f64, i64); 4],
    pub trades: Vec<Trade>,
}

impl Port {
    pub fn new(capital: f64) -> Self {
        Port { cash: capital, deposits: capital, ..Default::default() }
    }
}

/// The world as one simulation sees it on day `d`.
pub struct Mkt<'a> {
    pub data: &'a Data,
    pub f: &'a F,
    pub d: usize,
    pub day: i32,
    pub zone: usize,
}

impl<'a> Mkt<'a> {
    pub fn last_close(&self, s: usize) -> Option<f64> {
        self.data.close(self.d, s)
    }
    pub fn stock(&self, s: usize) -> Option<usize> {
        self.data.find(&self.data.stocks, self.d, s)
    }
    pub fn etf(&self, s: usize) -> Option<usize> {
        self.data.find(&self.data.etfs, self.d, s)
    }
    /// `mkt.stocks.get(s) or mkt.etfs.get(s)`: (rows, row).
    pub fn metrics(&self, s: usize) -> Option<(&'a Rows, usize)> {
        if let Some(r) = self.stock(s) {
            return Some((&self.data.stocks, r));
        }
        self.etf(s).map(|r| (&self.data.etfs, r))
    }
    pub fn v(&self, rows: &Rows, r: usize, field: usize) -> f64 {
        self.data.val(rows, r, field)
    }
    /// Python's `m.get(field)`.
    pub fn g(&self, rows: &Rows, r: usize, field: usize) -> Option<f64> {
        self.data.get(rows, r, field)
    }
    pub fn exposure(&self) -> f64 {
        EXPOSURE[self.zone]
    }
}

pub fn position_value(m: &Mkt, s: usize, pos: &Pos) -> f64 {
    match m.last_close(s) {
        None => pos.cost_eur,
        Some(price) => pos.qty * m.data.to_eur(s, price, m.day),
    }
}

pub fn equity(port: &Port, m: &Mkt) -> f64 {
    port.cash + py_sum(port.positions.iter().map(|(&s, p)| position_value(m, s, p)))
}

fn year_of(day: i32) -> i32 {
    // days since 1970-01-01 to the calendar year (civil-from-days, Howard Hinnant)
    let z = day as i64 + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    (if month <= 2 { y + 1 } else { y }) as i32
}

pub fn sell(port: &mut Port, m: &Mkt, s: usize, price: f64, on: i32, reason: &str, fraction: f64) -> Trade {
    let sym = &m.data.symbols[s];
    let (qty, cost, entry_date) = {
        let pos = &port.positions[&s];
        (pos.qty * fraction, pos.cost_eur * fraction, pos.entry_date)
    };
    let fill = price * (1.0 - sym.fee);
    let proceeds = qty * m.data.to_eur(s, fill, on);
    port.cash += proceeds;
    let year = year_of(on);
    let before = port.realized.get(&year).copied().unwrap_or(0.0);
    port.realized.insert(year, round_to(before + proceeds - cost, 2));
    if fraction >= 0.999 {
        port.positions.shift_remove(&s);
    } else {
        let pos = port.positions.get_mut(&s).unwrap();
        pos.qty -= qty;
        pos.cost_eur -= cost;
    }
    let t = Trade {
        day: on, side: 1, sym: Some(s), qty: round_to(qty, 6), price: round_to(fill, 4), eur: round_to(proceeds, 2),
        pnl: round_to(proceeds - cost, 2), reason: reason.to_string(),
        ret: Some(if cost != 0.0 { round_to(proceeds / cost - 1.0, 4) } else { 0.0 }), held: Some((on - entry_date) as i64),
    };
    port.trades.push(t.clone());
    t
}

pub fn buy(port: &mut Port, m: &Mkt, o: &Order, price: f64, on: i32) -> Option<Trade> {
    let eur = py_min(o.eur, port.cash);
    if eur < MIN_ORDER_EUR {
        return None;
    }
    let sym = &m.data.symbols[o.sym];
    let fill = price * (1.0 + sym.fee + sym.buy_tax);
    let qty = eur / m.data.to_eur(o.sym, fill, on);
    port.cash -= eur;
    if let Some(pos) = port.positions.get_mut(&o.sym) {
        pos.entry = (pos.entry * pos.qty + fill * qty) / (pos.qty + qty);
        pos.qty += qty;
        pos.cost_eur += eur;
    } else {
        port.positions.insert(o.sym, Pos { qty, entry: fill, entry_date: on, cost_eur: eur, stop: o.stop, tp: o.tp, checked: on - 1 });
    }
    let t = Trade { day: on, side: 0, sym: Some(o.sym), qty: round_to(qty, 6), price: round_to(fill, 4), eur: round_to(eur, 2),
                    pnl: 0.0, reason: o.reason.clone(), ret: None, held: None };
    port.trades.push(t.clone());
    Some(t)
}

/// agents.fill_pending (complete days only): the previous decision's orders at the open of the next session.
pub fn fill_pending(port: &mut Port, m: &Mkt) -> Vec<Trade> {
    let mut done = Vec::new();
    let mut still = Vec::new();
    let mut orders = std::mem::take(&mut port.pending);
    orders.sort_by_key(|o| o.buy); // sells first (stable)
    for o in orders {
        let bars = &m.data.bars[o.sym];
        let i0 = bars.day.partition_point(|&x| x <= o.created);
        let i1 = bars.day.partition_point(|&x| x <= m.day);
        let mut found: Option<(i32, f64)> = None;
        for i in i0..i1 {
            let valid = |x: f64| !x.is_nan() && x > 0.0;
            let price = if valid(bars.open[i]) { Some(bars.open[i]) } else if valid(bars.close[i]) { Some(bars.close[i]) } else { None };
            if let Some(p) = price {
                found = Some((bars.day[i], p));
                break;
            }
        }
        let Some((on, price)) = found else {
            if m.data.sessions_since(m.d, Some(o.created)) <= PENDING_MAX_SESSIONS {
                still.push(o);
            }
            continue;
        };
        if !o.buy {
            if port.positions.contains_key(&o.sym) {
                done.push(sell(port, m, o.sym, price, on, &o.reason, o.fraction));
            }
        } else {
            if let (Some(r), Some(limit)) = (o.reference, o.gap_max) {
                if r != 0.0 && limit != 0.0 && (price / r - 1.0).abs() > limit {
                    continue; // opened too far from the decision's close: cancelled
                }
            }
            if let Some(t) = buy(port, m, &o, price, on) {
                done.push(t);
            }
        }
    }
    port.pending = still;
    done
}

/// agents.check_exits (complete days): stops, then targets, bar by bar since each position was last checked.
pub fn check_exits(port: &mut Port, m: &Mkt) -> Vec<Trade> {
    let mut done = Vec::new();
    let held: Vec<usize> = port.positions.keys().copied().collect();
    for s in held {
        let bars = &m.data.bars[s];
        let Some(pos) = port.positions.get(&s).cloned() else { continue };
        let i0 = bars.day.partition_point(|&x| x <= pos.checked);
        let i1 = bars.day.partition_point(|&x| x <= m.day);
        for i in i0..i1 {
            let on = bars.day[i];
            let stop = port.positions[&s].stop;
            let tp = port.positions[&s].tp;
            if truthy(stop) && bars.low[i] <= stop.unwrap() {
                let st = stop.unwrap();
                done.push(sell(port, m, s, py_min(bars.open[i], st), on, "stop", 1.0));
                break;
            }
            if truthy(tp) && bars.high[i] >= tp.unwrap() {
                let t = tp.unwrap();
                done.push(sell(port, m, s, py_max(bars.open[i], t), on, "alvo", 1.0));
                break;
            }
            port.positions.get_mut(&s).unwrap().checked = on;
        }
    }
    done
}

/// agents.trail_stops
pub fn trail_stops(port: &mut Port, m: &Mkt, p: &P) {
    if !p.on("trail_atr") {
        return;
    }
    let k = p.req("trail_atr");
    let keys: Vec<usize> = port.positions.keys().copied().collect();
    for s in keys {
        if let Some((rows, r)) = m.metrics(s) {
            let atr = m.g(rows, r, m.f.atr14).unwrap_or(0.0);
            if atr != 0.0 {
                let level = round_to(m.v(rows, r, m.f.price) - k * atr, 4);
                let pos = port.positions.get_mut(&s).unwrap();
                if level > pos.stop.unwrap_or(0.0) {
                    pos.stop = Some(level);
                }
            }
        }
    }
}

/// agents.pay_taxes
pub fn pay_taxes(port: &mut Port, m: &Mkt) -> Option<Trade> {
    let &(last, _) = port.hist.last()?;
    if year_of(last) >= year_of(m.day) {
        return None;
    }
    let year = year_of(last);
    let gain = port.realized.get(&year).copied().unwrap_or(0.0);
    if gain <= 0.0 || port.taxed.contains(&year) {
        return None;
    }
    let tax = round_to(gain * TAX_RATE, 2);
    port.cash -= tax;
    port.taxed.push(year);
    let t = Trade { day: m.day, side: 2, sym: None, qty: 0.0, price: 0.0, eur: tax, pnl: -tax,
                    reason: format!("{}% sobre {:.0} € de mais-valias de {}", (TAX_RATE * 100.0).round(), gain, year), ret: None, held: None };
    port.trades.push(t.clone());
    Some(t)
}

/// agents.benchmark_return between the ends of two market days (the benchmark's close at each).
fn benchmark_return(m: &Mkt, since_d: usize) -> f64 {
    let a = m.data.bench[since_d];
    let b = m.data.bench[m.d];
    if a.is_nan() || b.is_nan() || a == 0.0 {
        0.0
    } else {
        b / a - 1.0
    }
}

/// agents.score_day (one new day).
pub fn score_day(port: &mut Port, m: &Mkt) {
    let eq = equity(port, m);
    let ratio = if port.deposits != 0.0 { eq / port.deposits } else { 1.0 };
    let peak = py_max(port.peak.unwrap_or(1.0), ratio);
    port.peak = Some(peak);
    let dd = py_max(0.0, 1.0 - ratio / peak);
    port.max_dd = round_to(py_max(port.max_dd, dd), 4);
    if let Some(&(prev_day, prev_eq)) = port.hist.last() {
        let day = if prev_eq != 0.0 { eq / prev_eq - 1.0 } else { 0.0 };
        let since = *m.data.day_index.get(&prev_day).expect("dia anterior fora dos dados");
        let excess = day - benchmark_return(m, since);
        let z = &mut port.zone_stats[m.zone];
        z.0 = round_to(z.0 + excess, 6);
        z.1 += 1;
        let pts = round_int(excess * 1000.0).clamp(-10, 10) + if eq > port.deposits { PROFIT_BONUS } else { 0 }
            - round_int(py_max(0.0, dd - DD_FREE) * DD_PENALTY);
        port.points += pts;
    }
    port.hist.push((m.day, round_to(eq, 2)));
}

pub fn is_bankrupt(port: &Port, m: &Mkt) -> bool {
    !port.hist.is_empty() && equity(port, m) < port.deposits * (1.0 - BANKRUPTCY)
}

pub fn liquidate(port: &mut Port, m: &Mkt, reason: &str) -> Vec<Trade> {
    port.pending.clear();
    let keys: Vec<usize> = port.positions.keys().copied().collect();
    let mut done = Vec::new();
    for s in keys {
        if let Some(price) = m.last_close(s) {
            if price != 0.0 {
                done.push(sell(port, m, s, price, m.day, reason, 1.0));
            }
        }
    }
    done
}

/// agents.tax_owed_if_sold
pub fn tax_owed_if_sold(port: &Port, m: &Mkt) -> f64 {
    let this_year = year_of(m.day);
    let open_gain = py_sum(port.positions.iter().map(|(&s, p)| position_value(m, s, p) - p.cost_eur));
    let mut owed = py_max(0.0, port.realized.get(&this_year).copied().unwrap_or(0.0) + open_gain);
    owed += py_sum(port.realized.iter().filter(|(&y, _)| y < this_year && !port.taxed.contains(&y) && y == this_year - 1)
        .map(|(_, &g)| py_max(0.0, g)));
    owed * TAX_RATE
}

// --------------------------------------------------------------------------
// The shared rules (agents.decide after the strategy)
// --------------------------------------------------------------------------

fn lerp(x: f64, best: f64, worst: f64, points: f64) -> f64 {
    if best < worst {
        return if x <= best { points } else if x >= worst { 0.0 } else { points * (worst - x) / (worst - best) };
    }
    if x >= best { points } else if x <= worst { 0.0 } else { points * (x - worst) / (best - worst) }
}

/// agents.tech_score
fn tech_score(m: &Mkt, rows: &Rows, r: usize) -> f64 {
    let f = m.f;
    let price = m.v(rows, r, f.price);
    let sma50 = m.v(rows, r, f.sma50);
    let sma200 = m.v(rows, r, f.sma200);
    let rising = m.v(rows, r, f.sma200_rising) != 0.0;
    let trend = (if price > sma50 { 10.0 } else { 0.0 }) + (if sma50 > sma200 { 10.0 } else { 0.0 }) + (if rising { 5.0 } else { 0.0 });
    let vol = m.v(rows, r, f.vol20);
    trend + lerp(vol, 0.015, 0.04, 20.0) + lerp(m.v(rows, r, f.pct_from_high), 0.05, 0.30, 15.0)
}

fn ctx_gt(m: &Mkt, key: &str, limit: f64) -> bool {
    m.data.context(m.d, key).unwrap_or(0.0) > limit
}

/// agents.context_gate (the backtest's context: no Claude geopolitics, no macro calendar)
fn context_gate(m: &Mkt, p: &P) -> bool {
    (p.on("vix_max") && ctx_gt(m, "vix", p.req("vix_max")))
        || (p.on("epu_max") && ctx_gt(m, "epu_pct", p.req("epu_max")))
        || (p.on("credito_max") && ctx_gt(m, "credit_dd", p.req("credito_max")))
        || (p.on("juros_max") && ctx_gt(m, "rate_chg", p.req("juros_max")))
        || (p.on("filtro_spy") && m.data.context(m.d, "spy_trend").unwrap_or(0.0) < 0.0)
        || (p.on("gpr_max") && ctx_gt(m, "gpr_pct", p.req("gpr_max")))
        || (p.on("petroleo_max") && ctx_gt(m, "oil_chg", p.req("petroleo_max")))
        || (p.on("ouro_max") && ctx_gt(m, "gold_chg", p.req("ouro_max")))
}

/// agents.stock_filter: true when the stock fails a rule.
fn stock_filter(m: &Mkt, rows: &Rows, r: usize, p: &P) -> bool {
    let f = m.f;
    let sector = rows.sector[r];
    if p.on("filtro_setor") && sector >= 0 {
        let (ti, ri) = (m.data.sector_trend[sector as usize], m.data.sector_rs[sector as usize]);
        let get = |i: i32| if i < 0 { None } else { let x = m.data.ctx_val[m.d * m.data.n_ctx + i as usize]; if x.is_nan() { None } else { Some(x) } };
        if let Some(t) = get(ti) {
            if t < 0.0 {
                return true;
            }
        }
        if p.req("filtro_setor") >= 2.0 {
            if let Some(x) = get(ri) {
                if x < 0.0 {
                    return true;
                }
            }
        }
    }
    if p.on("ema_filtro") {
        let get = |field: Option<usize>| field.and_then(|i| m.g(rows, r, i));
        if let (Some(a), Some(b)) = (get(f.ema21), get(f.ema50)) {
            let price = m.v(rows, r, f.price);
            if a != 0.0 && b != 0.0 && !(price > a && a > b) {
                return true;
            }
        }
    }
    let nullable = |field: Option<usize>| field.and_then(|i| m.g(rows, r, i));
    if p.on("qualidade_fund_min") {
        if let Some(q) = nullable(f.fund_q) {
            if q < p.req("qualidade_fund_min") {
                return true;
            }
        }
    }
    if p.on("valor_fund_min") {
        if let Some(v) = nullable(f.fund_v) {
            if v < p.req("valor_fund_min") {
                return true;
            }
        }
    }
    false
}

/// agents.shared_rules; returns whether any buy was held back (a "note").
fn shared_rules(orders: Vec<Order>, port: &Port, m: &Mkt, p: &P) -> (Vec<Order>, bool) {
    let mut kept = Vec::new();
    let mut noted = false;
    let gate = context_gate(m, p);
    let f = m.f;
    for mut o in orders {
        if !o.buy {
            kept.push(o);
            continue;
        }
        if gate {
            noted = true;
            continue;
        }
        if p.on("usd_max") && m.data.symbols[o.sym].currency_usd && ctx_gt(m, "usd_chg", p.req("usd_max")) {
            noted = true;
            continue;
        }
        if p.on("vix_meio") && ctx_gt(m, "vix", p.req("vix_meio")) {
            o.eur = round_to(o.eur * 0.5, 2);
        }
        if let Some((rows, r)) = m.metrics(o.sym) {
            let price = m.v(rows, r, f.price);
            if p.on("max_ext") && price > m.v(rows, r, f.sma50) * (1.0 + p.req("max_ext")) {
                noted = true;
                continue;
            }
            if p.on("min_tecnico") && tech_score(m, rows, r) < p.req("min_tecnico") {
                noted = true;
                continue;
            }
            if stock_filter(m, rows, r, p) {
                noted = true;
                continue;
            }
            let atr = m.g(rows, r, f.atr14).unwrap_or(0.0);
            if p.on("stop_atr") && atr != 0.0 {
                let atr_stop = round_to(price - p.req("stop_atr") * atr, 4);
                if atr_stop > 0.0 && (!truthy(o.stop) || atr_stop > o.stop.unwrap()) {
                    o.stop = Some(atr_stop);
                }
            }
            if p.on("stop_suporte") {
                if let Some(support) = f.support.and_then(|i| m.g(rows, r, i)) {
                    if support != 0.0 && atr != 0.0 {
                        let level = round_to(support - p.req("stop_suporte") * atr, 4);
                        if 0.0 < level && level < price && (price - level) / price <= SUPPORT_MAX_RISK
                            && (!truthy(o.stop) || level > o.stop.unwrap())
                        {
                            o.stop = Some(level);
                        }
                    }
                }
            }
        }
        kept.push(o);
    }
    if p.on("tempo_max") {
        let tempo = p.req("tempo_max");
        let selling: Vec<usize> = kept.iter().filter(|o| !o.buy).map(|o| o.sym).collect();
        for (&s, pos) in port.positions.iter() {
            if selling.contains(&s) {
                continue;
            }
            if let Some(price) = m.last_close(s) {
                if price != 0.0 && (m.data.sessions_since(m.d, Some(pos.entry_date)) as f64) >= tempo && price < pos.entry {
                    kept.push(order_sell(s, &format!("não andou em {} sessões", tempo as i64)));
                }
            }
        }
    }
    (kept, noted)
}

/// agents.skip_earnings with the past earnings dates.
fn skip_earnings(orders: Vec<Order>, m: &Mkt, days: i64) -> (Vec<Order>, bool) {
    let Some(dates) = &m.data.earnings else { return (orders, false) };
    let mut kept = Vec::new();
    let mut noted = false;
    for o in orders {
        if o.buy && !m.data.symbols[o.sym].is_etf && m.etf(o.sym).is_none() {
            let list = &dates[o.sym];
            let i = list.partition_point(|&x| x < m.day);
            if i < list.len() && days != 0 {
                let n = (list[i] - m.day) as i64;
                if (0..=days).contains(&n) {
                    noted = true;
                    continue;
                }
            }
        }
        kept.push(o);
    }
    (kept, noted)
}

/// agents.brake
fn brake(port: &mut Port, m: &Mkt, eq: f64) -> bool {
    if let Some(until) = port.brake_until {
        if m.day <= until {
            return true;
        }
    }
    let ratio = if port.deposits != 0.0 { eq / port.deposits } else { 1.0 };
    if port.brake_until.is_some() {
        port.brake_until = None;
        port.brake_peak = Some(ratio);
    }
    let peak = py_max(port.brake_peak.unwrap_or(1.0), ratio);
    port.brake_peak = Some(peak);
    if 1.0 - ratio / peak >= DD_BRAKE {
        port.brake_until = Some(m.day + BRAKE_DAYS);
        return true;
    }
    false
}

/// agents.risk_filter
fn risk_filter(orders: Vec<Order>, port: &mut Port, m: &Mkt) -> (Vec<Order>, bool) {
    let eq = equity(port, m);
    let braking = brake(port, m, eq);
    let mut sector_value: HashMap<i32, f64> = HashMap::new();
    for (&s, pos) in port.positions.iter() {
        if let Some(r) = m.stock(s) {
            let sec = m.data.stocks.sector[r];
            let v = position_value(m, s, pos);
            *sector_value.entry(sec).or_insert(0.0) += v;
        }
    }
    let mut kept = Vec::new();
    let mut noted = false;
    for mut o in orders {
        if !o.buy {
            kept.push(o);
            continue;
        }
        if braking {
            noted = true;
            continue;
        }
        if let Some(r) = m.stock(o.sym) {
            let rows = &m.data.stocks;
            let vol = m.g(rows, r, m.f.vol20).unwrap_or(0.0);
            if vol != 0.0 {
                o.eur = round_to(o.eur * py_max(0.5, py_min(1.0, TARGET_VOL / vol)), 2);
            }
            let sec = rows.sector[r];
            let have = sector_value.get(&sec).copied().unwrap_or(0.0);
            if have + o.eur > SECTOR_CAP * eq && sec != m.data.sector_unknown {
                noted = true;
                continue;
            }
            sector_value.insert(sec, have + o.eur);
        }
        kept.push(o);
    }
    (kept, noted)
}

/// agents.decide in a backtest (live=False): the strategy, then the shared rules.
pub fn decide(strategy: &str, port: &mut Port, m: &Mkt, p: &P) -> Vec<Order> {
    let before = port.last_rebalance;
    let region = if crate::strategies::uses_region(strategy) { p.get("regiao").unwrap_or(0.0) as i64 } else { 0 };
    let mut orders = crate::strategies::run(strategy, port, m, p, region);
    let mut noted = false;
    if strategy != "indexador" {
        let (o, n) = shared_rules(orders, port, m, p);
        orders = o;
        noted |= n;
    }
    if m.data.earnings.is_some() && !p.on("ignora_resultados") {
        let days = p.get("dias_resultados").map(|x| x as i64).unwrap_or(EARNINGS_AVOID_DAYS);
        let (o, n) = skip_earnings(orders, m, days);
        orders = o;
        noted |= n;
    }
    if !p.on("ignora_risco") {
        let (o, n) = risk_filter(orders, port, m);
        orders = o;
        noted |= n;
    }
    orders.retain(|o| !o.buy || o.eur >= MIN_ORDER_EUR);
    if noted && !orders.iter().any(|o| o.buy) && port.last_rebalance != before {
        port.last_rebalance = before;
    }
    let gap = if strategy != "indexador" { p.get("gap_max").unwrap_or(0.0) } else { 0.0 };
    if gap != 0.0 {
        for o in orders.iter_mut().filter(|o| o.buy) {
            o.reference = m.last_close(o.sym);
            o.gap_max = Some(gap);
        }
    }
    orders
}

/// agents.due
pub fn due(port: &mut Port, m: &Mkt, every: f64) -> bool {
    if (m.data.sessions_since(m.d, port.last_rebalance) as f64) >= every {
        port.last_rebalance = Some(m.day);
        return true;
    }
    false
}

/// agents.rebalance
pub fn rebalance(port: &Port, m: &Mkt, targets: &[usize], weight: f64, keep: &[usize], stop_pct: Option<f64>, reason: &str) -> Vec<Order> {
    let mut orders: Vec<Order> = port.positions.keys().filter(|s| !targets.contains(s) && !keep.contains(s))
        .map(|&s| order_sell(s, "saiu da lista")).collect();
    let eq = equity(port, m);
    for &s in targets {
        if port.positions.contains_key(&s) {
            continue;
        }
        let price = m.last_close(s);
        let stop = match (stop_pct, price) {
            (Some(k), Some(pr)) if k != 0.0 && pr != 0.0 => Some(round_to(pr * (1.0 - k), 4)),
            _ => None,
        };
        orders.push(order_buy(s, eq * weight, reason, stop, None));
    }
    orders
}
