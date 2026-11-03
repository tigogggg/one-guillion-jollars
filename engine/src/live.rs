//! The reactive rounds (agents.react, live_exits, buy_window, round_buy_block) and agents stepping through slots of
//! market data in batches (the intraday lab's checkpoints now; the live rounds next).

use crate::data::F;
use crate::engine::{self, buy, equity, sell, Mkt, Order, Port, Trade, P};
use crate::pyround::{py_sum, truthy};

pub const NO_BUYS_AFTER_OPEN: f64 = 15.0;
pub const NO_BUYS_BEFORE_CLOSE: f64 = 30.0;
pub const DAY_LOSS_STOP: f64 = 0.03;

/// agents.trading_now: the exchange is in session and today's bar is already in the data.
pub fn trading_now(m: &Mkt, s: usize) -> bool {
    m.data.exchange(m.d, s).0 && m.data.part_bar(m.d, s).is_some()
}

/// agents.move_today: the price now against the last complete session's close.
fn move_today(m: &Mkt, s: usize) -> Option<f64> {
    let price = m.last_close(s)?;
    let prev = m.data.prev_close(m.d, s)?;
    if prev > 0.0 { Some(price / prev - 1.0) } else { None }
}

/// agents.buy_window: whether a reactive buy of this symbol waits (open or close too near, or not confirmed yet).
fn buy_window(m: &Mkt, s: usize, p: &P) -> bool {
    let level = p.get("reatividade").unwrap_or(2.0) as i64;
    if p.on("ignora_risco") && level == 2 {
        return false;
    }
    let (_, since_open, to_close) = m.data.exchange(m.d, s);
    let wait = if level == 2 {
        NO_BUYS_AFTER_OPEN
    } else {
        let w = p.get("espera_min").filter(|&x| x != 0.0).unwrap_or(NO_BUYS_AFTER_OPEN).trunc();
        if w > NO_BUYS_AFTER_OPEN { w } else { NO_BUYS_AFTER_OPEN }
    };
    if since_open < wait {
        return true;
    }
    let stretch = if level == 1 { p.get("esticado_max").unwrap_or(0.0) } else { 0.0 };
    if stretch != 0.0 {
        if let Some(mv) = move_today(m, s) {
            if mv > stretch {
                return true;
            }
        }
    }
    to_close < NO_BUYS_BEFORE_CLOSE
}

/// agents.round_buy_block: no new buys today after a DAY_LOSS_STOP loss since the last close.
fn round_buy_block(port: &Port, m: &Mkt, p: &P) -> bool {
    if p.on("ignora_risco") || port.hist.is_empty() {
        return false;
    }
    let n = port.hist.len();
    let before = if port.hist[n - 1].0 < m.day { Some(port.hist[n - 1].1) } else if n > 1 { Some(port.hist[n - 2].1) } else { None };
    match before {
        Some(b) if b != 0.0 => equity(port, m) / b - 1.0 <= -DAY_LOSS_STOP,
        _ => false,
    }
}

/// agents.live_exits: exits at the price of the round.
pub fn live_exits(port: &mut Port, m: &Mkt, targets: bool) -> Vec<Trade> {
    let mut done = Vec::new();
    let held: Vec<usize> = port.positions.keys().copied().collect();
    for s in held {
        let pos = port.positions[&s].clone();
        let bought_today = pos.checked >= m.day;
        if !(bought_today || targets) || !trading_now(m, s) {
            continue;
        }
        let Some(price) = m.last_close(s) else { continue };
        if bought_today && truthy(pos.stop) && price <= pos.stop.unwrap() {
            done.push(sell(port, m, s, price, m.day, "stop", 1.0));
        } else if truthy(pos.tp) && price >= pos.tp.unwrap() {
            done.push(sell(port, m, s, price, m.day, "alvo", 1.0));
        }
    }
    done
}

/// agents.react (live=False): one reactive decision at the prices of this moment.
pub fn react(strategy: &str, port: &mut Port, m: &Mkt, p: &P) -> Vec<Trade> {
    if port.positions.is_empty() && port.pending.is_empty() {
        port.last_rebalance = None;
    }
    let sold: Vec<usize> = port.trades.iter().filter(|t| t.side == 1 && t.day == m.day).filter_map(|t| t.sym).collect();
    let mut pending: Vec<usize> = port.pending.iter().map(|o| o.sym).collect();
    let reserved = py_sum(port.pending.iter().filter(|o| o.buy).map(|o| o.eur));
    port.cash -= reserved;
    let orders = engine::decide(strategy, port, m, p);
    port.cash += reserved;
    let mut done = Vec::new();
    let day_block = round_buy_block(port, m, p);
    let mut orders: Vec<Order> = orders;
    orders.sort_by_key(|o| o.buy); // sells first (stable)
    let planned_level = p.get("reatividade").unwrap_or(2.0) as i64 == 0;
    for o in orders {
        let s = o.sym;
        if pending.contains(&s) || (o.buy && sold.contains(&s)) {
            continue;
        }
        if o.buy && day_block {
            continue;
        }
        let planned = o.buy && planned_level;
        if trading_now(m, s) && !planned {
            if o.buy && buy_window(m, s, p) {
                continue;
            }
            let Some(price) = m.last_close(s) else { continue };
            if !o.buy {
                if port.positions.contains_key(&s) {
                    done.push(sell(port, m, s, price, m.day, &o.reason, o.fraction));
                }
            } else if let Some(t) = buy(port, m, &o, price, m.day) {
                port.positions.get_mut(&s).unwrap().checked = m.day;
                done.push(t);
            }
        } else if let Some(last) = m.data.last_bar_day(m.d, s) {
            let mut o = o;
            o.created = last;
            port.pending.push(o);
            pending.push(s);
        }
    }
    done
}

/// One agent stepping through slots of data.
#[derive(Clone)]
pub struct Agent {
    pub strategy: String,
    pub params: Vec<P>,
    pub port: Port,
    pub bankrupt: Option<i32>,
    pub n_trades: usize,
    pub final_equity: f64,
    pub tax_owed: f64,
    pub sells: Vec<(f64, i64)>,
}

impl Agent {
    pub fn new(strategy: &str, params: Vec<P>, capital: f64) -> Self {
        Agent { strategy: strategy.to_string(), params, port: Port::new(capital), bankrupt: None, n_trades: 0,
                final_equity: capital, tax_owed: 0.0, sells: Vec::new() }
    }

    fn count(&mut self, done: &[Trade]) {
        self.n_trades += done.len();
        self.sells.extend(done.iter().filter(|t| t.side == 1).map(|t| (t.ret.unwrap_or(0.0), t.held.unwrap_or(0))));
    }

    /// intraday_lab.run_reactive: a checkpoint of the session (partial slot), or the day's close.
    pub fn reactive_slot(&mut self, m: &Mkt) {
        let p = &self.params[m.zone].clone();
        if m.data.partial[m.d] {
            let mut done = engine::fill_pending(&mut self.port, m);
            done.extend(engine::check_exits(&mut self.port, m));
            done.extend(live_exits(&mut self.port, m, true));
            if self.bankrupt.is_none() {
                done.extend(react(&self.strategy, &mut self.port, m, p));
            }
            self.count(&done);
        } else {
            let mut done = engine::fill_pending(&mut self.port, m);
            done.extend(engine::check_exits(&mut self.port, m));
            self.count(&done);
            engine::trail_stops(&mut self.port, m, p);
            engine::pay_taxes(&mut self.port, m);
            engine::score_day(&mut self.port, m);
            if self.bankrupt.is_none() && engine::is_bankrupt(&self.port, m) {
                let done = engine::liquidate(&mut self.port, m, "falência");
                self.count(&done);
                self.bankrupt = Some(m.day);
            }
        }
        self.final_equity = equity(&self.port, m);
        self.tax_owed = engine::tax_owed_if_sold(&self.port, m);
    }
}

/// Every agent through slots [start, end), on all the cores. Scoped threads that end with the call (no pool left
/// running: the pre-training forks worker processes, and forking a process with live threads is unsafe).
pub fn step(agents: &mut [Agent], d: &crate::data::Data, f: &F, start: usize, end: usize) {
    let run = |a: &mut Agent| {
        for di in start..end {
            let m = Mkt { data: d, f, d: di, day: d.days[di], zone: d.zone[di] as usize };
            a.reactive_slot(&m);
        }
    };
    let threads = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1).min(agents.len().max(1));
    if threads <= 1 {
        agents.iter_mut().for_each(run);
        return;
    }
    let per = agents.len().div_ceil(threads);
    std::thread::scope(|scope| {
        for chunk in agents.chunks_mut(per) {
            scope.spawn(move || chunk.iter_mut().for_each(run));
        }
    });
}
