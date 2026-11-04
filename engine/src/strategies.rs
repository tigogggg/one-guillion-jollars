//! The strategies (agents.s_*), each a line-by-line port of the Python one; `SUPPORTED` lists those the engine runs.

use crate::data::Rows;
use crate::engine::{due, equity, order_buy, order_sell, position_value, rebalance, Mkt, Order, Port, P, MIN_ORDER_EUR};
use crate::pyround::{py_max, py_min, round_to};

pub const SUPPORTED: &[&str] = &[
    "indexador", "guardiao", "rotador", "foguetao", "analista", "quant", "monge", "abutre", "pendulo", "cacador", "sniper",
    "campeao", "pescador", "deriva", "kamikaze", "estratega", "navegador",
];

/// Strategies that trade only ETFs or other agents' portfolios (agents.NO_REGION).
const NO_REGION: &[&str] = &["indexador", "guardiao", "rotador", "pendulo", "joalheiro", "conselho", "maestro", "navegador"];

pub fn uses_region(strategy: &str) -> bool {
    !NO_REGION.contains(&strategy)
}

/// A parameter printed as Python prints it (ints without decimals).
fn num(x: f64) -> String {
    if x.fract() == 0.0 && x.abs() < 1e15 {
        format!("{}", x as i64)
    } else {
        format!("{x}")
    }
}

/// The view the strategy sees (agents.in_region): the day's stocks of the region, plus held ones elsewhere.
struct View<'a> {
    m: &'a Mkt<'a>,
    rows: &'a Rows,
    list: Vec<usize>,
}

impl<'a> View<'a> {
    fn new(port: &Port, m: &'a Mkt<'a>, region: i64) -> Self {
        let rows = &m.data.stocks;
        let list = rows.range(m.d).filter(|&r| {
            if region == 0 {
                return true;
            }
            let s = rows.sym[r] as usize;
            let us = m.data.symbols[s].is_us;
            (if region == 1 { us } else { !us }) || port.positions.contains_key(&s)
        }).collect();
        View { m, rows, list }
    }
    fn sym(&self, r: usize) -> usize {
        self.rows.sym[r] as usize
    }
    fn v(&self, r: usize, field: usize) -> f64 {
        self.m.v(self.rows, r, field)
    }
    fn g(&self, r: usize, field: usize) -> Option<f64> {
        self.m.g(self.rows, r, field)
    }
    fn field(&self, name: &str) -> usize {
        *self.m.data.fields.get(name).unwrap_or_else(|| panic!("campo em falta: {name}"))
    }
    fn opt(&self, name: &str) -> Option<usize> {
        self.m.data.fields.get(name).copied()
    }
    /// mkt.stocks.get(s) within the view.
    fn row_of(&self, s: usize) -> Option<usize> {
        let r = self.m.stock(s)?;
        if self.list.binary_search(&r).is_ok() { Some(r) } else { None }
    }
}

/// Sorts symbols (rows) by a key, descending or ascending, keeping ties in their order (Python's stable sorted).
fn sort_by_key(items: &mut [usize], key: impl Fn(usize) -> f64, descending: bool) {
    items.sort_by(|&a, &b| {
        let (x, y) = (key(a), key(b));
        let o = if descending { y.partial_cmp(&x) } else { x.partial_cmp(&y) };
        o.unwrap_or(std::cmp::Ordering::Equal)
    });
}

pub fn run(strategy: &str, port: &mut Port, m: &Mkt, p: &P, region: i64) -> Vec<Order> {
    match strategy {
        "indexador" => indexador(port, m),
        "guardiao" => guardiao(port, m, p),
        "rotador" => rotador(port, m, p),
        "foguetao" => foguetao(port, m, p, region),
        "analista" => analista(port, m, p, region),
        "quant" => quant(port, m, p, region),
        "monge" => monge(port, m, p, region),
        "abutre" => abutre(port, m, p, region),
        "pendulo" => pendulo(port, m, p),
        "cacador" => cacador(port, m, p, region),
        "sniper" => sniper(port, m, p, region),
        "campeao" => campeao(port, m, p, region),
        "estratega" => estratega(port, m, p, region),
        "navegador" => navegador(port, m, p),
        "pescador" => pescador(port, m, p, region),
        "deriva" => deriva(port, m, p, region),
        "kamikaze" => kamikaze(port, m, p, region),
        other => panic!("estratégia ainda não portada para Rust: {other}"),
    }
}

fn free_slots(port: &Port, n: f64) -> i64 {
    n as i64 - port.positions.len() as i64
}

/// agents.risk_sized
fn risk_sized(port: &Port, m: &Mkt, s: usize, stop: f64, risk_pct: f64, max_pos: f64) -> f64 {
    let price = m.last_close(s);
    let risk = match price {
        Some(pr) if pr != 0.0 => (pr - stop) / pr,
        _ => 1.0,
    };
    if !(0.0 < risk && risk <= 0.15) {
        return 0.0;
    }
    let eq = equity(port, m);
    py_min(eq * risk_pct / risk, eq * max_pos)
}

fn indexador(port: &Port, m: &Mkt) -> Vec<Order> {
    let Some(&s) = m.data.by_name.get("SXR8.DE") else { return Vec::new() };
    if port.cash >= MIN_ORDER_EUR { vec![order_buy(s, port.cash, "investir", None, None)] } else { Vec::new() }
}

fn guardiao(port: &mut Port, m: &Mkt, p: &P) -> Vec<Order> {
    if !port.positions.is_empty() && !due(port, m, 5.0) {
        return Vec::new();
    }
    port.last_rebalance = Some(m.day);
    let Some(&s) = m.data.by_name.get("VWCE.DE") else { return Vec::new() };
    let eq = equity(port, m);
    let target = m.exposure() * p.req("max_exposicao");
    let pos = port.positions.get(&s);
    let current = match pos { Some(pos) => position_value(m, s, pos) / eq, None => 0.0 };
    let zone = crate::data::ZONES[m.zone];
    if current < target - p.req("folga") {
        return vec![order_buy(s, (target - current) * eq, &format!("zona {zone}"), None, None)];
    }
    if pos.is_some() && current > target + p.req("folga") {
        let mut o = order_sell(s, &format!("zona {zone}"));
        o.fraction = (current - target) / current;
        return vec![o];
    }
    Vec::new()
}

fn rotador(port: &mut Port, m: &Mkt, p: &P) -> Vec<Order> {
    if !due(port, m, p.req("cada")) {
        return Vec::new();
    }
    let rows = &m.data.etfs;
    let f = m.f;
    let mut ranked: Vec<usize> = rows.range(m.d).filter(|&r| m.v(rows, r, f.price) > m.v(rows, r, f.sma200)).collect();
    sort_by_key(&mut ranked, |r| rows.trend[r], true);
    let ranked: Vec<usize> = ranked.into_iter().map(|r| rows.sym[r] as usize).collect();
    let n = p.req("n") as usize;
    rebalance(port, m, &ranked[..n.min(ranked.len())], m.exposure() / n as f64, &ranked[..(n + 1).min(ranked.len())], None, "top ETFs")
}

fn top_rebalance(port: &Port, m: &Mkt, ranked: Vec<usize>, view: &View, n: usize, keep: usize, weight: f64, stop: Option<f64>,
                 reason: &str) -> Vec<Order> {
    let syms: Vec<usize> = ranked.into_iter().map(|r| view.sym(r)).collect();
    rebalance(port, m, &syms[..n.min(syms.len())], weight, &syms[..keep.min(syms.len())], stop, reason)
}

fn foguetao(port: &mut Port, m: &Mkt, p: &P, region: i64) -> Vec<Order> {
    if !due(port, m, p.req("cada")) {
        return Vec::new();
    }
    let v = View::new(port, m, region);
    let mut ranked: Vec<usize> = v.list.iter().copied().filter(|&r| v.rows.stage2[r]).collect();
    sort_by_key(&mut ranked, |r| v.rows.trend[r], true);
    let n = p.req("n") as usize;
    top_rebalance(port, m, ranked, &v, n, 2 * n, m.exposure() / n as f64, p.get("stop_pct"), "top tendência")
}

fn analista(port: &mut Port, m: &Mkt, p: &P, region: i64) -> Vec<Order> {
    if !due(port, m, p.req("cada")) {
        return Vec::new();
    }
    let v = View::new(port, m, region);
    let (fq, fv) = (v.opt("fund_q"), v.opt("fund_v"));
    let get = |r: usize, f: Option<usize>| f.and_then(|i| v.g(r, i));
    let (qmin, vmin) = (p.req("qualidade_min"), p.req("valor_min"));
    let mut pool: Vec<usize> = v.list.iter().copied().filter(|&r| {
        v.rows.stage2[r] && matches!((get(r, fq), get(r, fv)), (Some(q), Some(x)) if q >= qmin && x >= vmin)
    }).collect();
    let (wq, wv, wt) = (p.get("peso_qualidade").unwrap_or(1.0), p.get("peso_valor").unwrap_or(1.0), p.get("peso_tendencia").unwrap_or(1.0));
    sort_by_key(&mut pool, |r| wq * get(r, fq).unwrap() + wv * get(r, fv).unwrap() + wt * v.rows.trend[r], true);
    let n = p.req("n") as usize;
    top_rebalance(port, m, pool, &v, n, 2 * n, m.exposure() / n as f64, p.get("stop_pct"), "qualidade, valor e tendência")
}

/// agents.ranks: percentile rank 0-1 within the group (ties in the order they come in).
fn ranks(keys: &[f64]) -> Vec<f64> {
    let mut order: Vec<usize> = (0..keys.len()).collect();
    sort_by_key(&mut order, |i| keys[i], false);
    let n = py_max((keys.len() as f64) - 1.0, 1.0);
    let mut out = vec![0.0; keys.len()];
    for (pos, &i) in order.iter().enumerate() {
        out[i] = pos as f64 / n;
    }
    out
}

fn quant(port: &mut Port, m: &Mkt, p: &P, region: i64) -> Vec<Order> {
    if !due(port, m, p.req("cada")) {
        return Vec::new();
    }
    let v = View::new(port, m, region);
    let only_trend = p.on("so_tendencia");
    let pool: Vec<usize> = v.list.iter().copied().filter(|&r| !only_trend || v.rows.stage2[r]).collect();
    if pool.is_empty() {
        return rebalance(port, m, &[], 0.0, &[], p.get("stop_pct"), "multi-fator");
    }
    let (r6, r3, vol) = (v.field("ret_6m"), v.field("ret_3m"), v.field("vol20"));
    let (fq, fv) = (v.opt("fund_q"), v.opt("fund_v"));
    let mom = ranks(&pool.iter().map(|&r| 0.6 * v.v(r, r6) + 0.4 * v.v(r, r3)).collect::<Vec<_>>());
    let low_vol = ranks(&pool.iter().map(|&r| {
        let x = v.g(r, vol).filter(|&x| x != 0.0).unwrap_or(0.03);
        -x
    }).collect::<Vec<_>>());
    let fund = |r: usize, f: Option<usize>| f.and_then(|i| v.g(r, i)).map(|x| x / 100.0).unwrap_or(0.5);
    let w = |k: &str| p.get(k).unwrap_or(1.0);
    let scores: Vec<f64> = pool.iter().enumerate().map(|(i, &r)| {
        w("peso_momentum") * mom[i] + w("peso_tendencia") * v.rows.trend[r] / 100.0 + w("peso_qualidade") * fund(r, fq)
            + w("peso_valor") * fund(r, fv) + w("peso_baixa_vol") * low_vol[i]
    }).collect();
    let mut order: Vec<usize> = (0..pool.len()).collect();
    sort_by_key(&mut order, |i| scores[i], true);
    let ranked: Vec<usize> = order.into_iter().map(|i| pool[i]).collect();
    let n = p.req("n") as usize;
    top_rebalance(port, m, ranked, &v, n, 2 * n, m.exposure() / n as f64, p.get("stop_pct"), "multi-fator")
}

fn monge(port: &mut Port, m: &Mkt, p: &P, region: i64) -> Vec<Order> {
    if !due(port, m, p.req("cada")) {
        return Vec::new();
    }
    let v = View::new(port, m, region);
    let f = m.f;
    let fq = v.opt("fund_q");
    let qmin = p.req("qualidade_min");
    let mut pool: Vec<usize> = v.list.iter().copied().filter(|&r| {
        v.v(r, f.price) > v.v(r, f.sma200) && v.g(r, f.vol20).map_or(false, |x| x != 0.0)
            && fq.and_then(|i| v.g(r, i)).map_or(true, |q| q >= qmin)
    }).collect();
    sort_by_key(&mut pool, |r| v.v(r, f.vol20), false);
    let n = p.req("n") as usize;
    top_rebalance(port, m, pool, &v, n, 3 * n, m.exposure() / n as f64, p.get("stop_pct"), "baixa volatilidade")
}

fn abutre(port: &mut Port, m: &Mkt, p: &P, region: i64) -> Vec<Order> {
    let v = View::new(port, m, region);
    let f = m.f;
    let max_days = p.req("max_dias");
    let mut orders = Vec::new();
    for (&s, pos) in port.positions.iter() {
        if (m.data.sessions_since(m.d, Some(pos.entry_date)) as f64) >= max_days {
            orders.push(order_sell(s, &format!("{} sessões", num(max_days))));
        } else if let Some(r) = v.row_of(s) {
            if v.v(r, f.pct_from_high) <= 0.05 {
                orders.push(order_sell(s, "alvo: voltou perto do máximo"));
            }
        }
    }
    if m.zone == 3 {
        return orders;
    }
    let (ema21, fq, fv) = (v.opt("ema21"), v.opt("fund_q"), v.opt("fund_v"));
    let get = |r: usize, f: Option<usize>| f.and_then(|i| v.g(r, i));
    let mut picks: Vec<usize> = v.list.iter().copied().filter(|&r| {
        let s = v.sym(r);
        !port.positions.contains_key(&s) && v.v(r, f.pct_from_high) >= p.req("queda_min")
            && get(r, ema21).map_or(false, |e| e != 0.0 && v.v(r, f.price) > e)
            && get(r, fq).map_or(false, |q| q >= p.req("qualidade_min"))
            && get(r, fv).map_or(false, |x| x >= p.req("valor_min"))
    }).collect();
    let eq = equity(port, m);
    sort_by_key(&mut picks, |r| get(r, fq).unwrap() + get(r, fv).unwrap(), true);
    let slots = py_max(free_slots(port, p.req("n")) as f64, 0.0) as usize;
    for &r in picks.iter().take(slots) {
        let price = v.v(r, f.price);
        let why = format!("{:.0}% abaixo do máximo, qualidade e valor altos", v.v(r, f.pct_from_high) * 100.0);
        orders.push(order_buy(v.sym(r), eq / p.req("n"), &why, Some(round_to(price * (1.0 - p.req("stop_pct")), 4)), None));
    }
    orders
}

fn pendulo(port: &mut Port, m: &Mkt, p: &P) -> Vec<Order> {
    if !due(port, m, p.req("cada")) {
        return Vec::new();
    }
    let rows = &m.data.etfs;
    let (r6, r3) = (m.data.fields["ret_6m"], m.data.fields["ret_3m"]);
    let all: Vec<usize> = rows.range(m.d).collect();
    let mom = |r: usize| 0.5 * m.v(rows, r, r6) + 0.5 * m.v(rows, r, r3);
    let defensive = |r: usize| m.data.symbols[rows.sym[r] as usize].defensive;
    let mut equity_etfs: Vec<usize> = all.iter().copied().filter(|&r| !defensive(r)).collect();
    sort_by_key(&mut equity_etfs, mom, true);
    let mut safe: Vec<usize> = all.iter().copied().filter(|&r| defensive(r)).collect();
    sort_by_key(&mut safe, mom, true);
    let n = p.req("n") as usize;
    let (targets, why): (Vec<usize>, &str) = if !equity_etfs.is_empty() && mom(equity_etfs[0]) > p.req("limiar_mom") {
        (equity_etfs.iter().take(n).copied().collect(), "momentum: ações")
    } else if !safe.is_empty() {
        (safe[..1].to_vec(), "momentum: refúgio (obrigações ou ouro)")
    } else {
        (Vec::new(), "momentum: dinheiro")
    };
    let syms: Vec<usize> = targets.iter().map(|&r| rows.sym[r] as usize).collect();
    rebalance(port, m, &syms, 1.0 / py_max(syms.len() as f64, 1.0), &[], None, why)
}

fn cacador(port: &mut Port, m: &Mkt, p: &P, region: i64) -> Vec<Order> {
    if m.zone == 3 {
        return Vec::new();
    }
    let v = View::new(port, m, region);
    let f = m.f;
    let (h20, volr, l10) = (v.field("high20_prev"), v.field("vol_ratio"), v.field("low10_prev"));
    let mut picks: Vec<usize> = v.list.iter().copied().filter(|&r| {
        let (price, high) = (v.v(r, f.price), v.v(r, h20));
        v.rows.stage2[r] && high < price && price <= high * 1.03 && v.v(r, volr) >= 1.5 && !port.positions.contains_key(&v.sym(r))
    }).collect();
    sort_by_key(&mut picks, |r| v.v(r, volr), true);
    let slots = py_max(free_slots(port, p.req("n")) as f64, 0.0) as usize;
    let mut orders = Vec::new();
    for &r in picks.iter().take(slots) {
        let s = v.sym(r);
        let stop = v.v(r, l10);
        let eur = risk_sized(port, m, s, stop, p.req("risco_pct"), p.req("max_pos"));
        if eur >= MIN_ORDER_EUR {
            let price = v.v(r, f.price);
            let tp = price + p.req("alvo_r") * (price - stop);
            orders.push(order_buy(s, eur, "rompimento", Some(round_to(stop, 4)), Some(round_to(tp, 4))));
        }
    }
    orders
}

fn sniper(port: &mut Port, m: &Mkt, p: &P, region: i64) -> Vec<Order> {
    let v = View::new(port, m, region);
    let f = m.f;
    let mut orders: Vec<Order> = Vec::new();
    for &s in port.positions.keys() {
        // metrics(mkt, s) within the view: its stocks, else the ETFs
        let hit = v.row_of(s).map(|r| (v.rows, r)).or_else(|| m.etf(s).map(|r| (&m.data.etfs, r)));
        if let Some((rows, r)) = hit {
            if m.v(rows, r, f.price) < m.v(rows, r, f.sma50) {
                orders.push(order_sell(s, "perdeu a média 50d"));
            }
        }
    }
    if m.zone == 3 {
        return orders;
    }
    let (r10, h20, l10) = (v.field("range10"), v.field("high20_prev"), v.field("low10_prev"));
    let mut picks: Vec<usize> = v.list.iter().copied().filter(|&r| {
        let own = v.rows.stage2[r] && v.v(r, r10) <= p.req("aperto_pct") && v.v(r, f.price) >= v.v(r, h20) * 0.97
            && v.rows.trend[r] >= 80.0;
        (own || m.data.vcp[v.sym(r)]) && !port.positions.contains_key(&v.sym(r))
    }).collect();
    sort_by_key(&mut picks, |r| v.rows.trend[r], true);
    let slots = py_max(free_slots(port, p.req("n")) as f64, 0.0) as usize;
    for &r in picks.iter().take(slots) {
        let s = v.sym(r);
        let stop = v.v(r, l10);
        let eur = risk_sized(port, m, s, stop, p.req("risco_pct"), p.req("max_pos"));
        if eur >= MIN_ORDER_EUR {
            orders.push(order_buy(s, eur, if m.data.vcp[s] { "VCP" } else { "contração" }, Some(round_to(stop, 4)), None));
        }
    }
    orders
}

fn campeao(port: &mut Port, m: &Mkt, p: &P, region: i64) -> Vec<Order> {
    if !due(port, m, p.req("cada")) {
        return Vec::new();
    }
    let v = View::new(port, m, region);
    let mut sectors: Vec<i32> = Vec::new();
    let mut members: Vec<Vec<usize>> = Vec::new();
    for &r in &v.list {
        let sec = v.rows.sector[r];
        match sectors.iter().position(|&x| x == sec) {
            Some(i) => members[i].push(r),
            None => {
                sectors.push(sec);
                members.push(vec![r]);
            }
        }
    }
    let unknown = m.data.sector_unknown;
    let mut strong: Vec<(usize, f64)> = Vec::new();
    for (i, rows) in members.iter().enumerate() {
        if rows.len() >= 5 && sectors[i] != unknown {
            let up = rows.iter().filter(|&&r| v.rows.stage2[r]).count() as f64;
            strong.push((i, up / rows.len() as f64));
        }
    }
    let mut order: Vec<usize> = (0..strong.len()).collect();
    sort_by_key(&mut order, |k| strong[k].1, true);
    let top: Vec<usize> = order.into_iter().take(p.req("setores") as usize).map(|k| strong[k].0).collect();
    let mut targets: Vec<usize> = Vec::new();
    for &i in &top {
        let mut ranked: Vec<usize> = members[i].iter().copied().filter(|&r| v.rows.stage2[r]).collect();
        sort_by_key(&mut ranked, |r| v.rows.trend[r], true);
        targets.extend(ranked.into_iter().take(p.req("por_setor") as usize).map(|r| v.sym(r)));
    }
    if targets.is_empty() {
        return rebalance(port, m, &[], 0.0, &[], None, "");
    }
    let names: Vec<String> = top.iter().map(|&i| {
        let sec = sectors[i];
        if sec >= 0 { m.data.sectors_pt[sec as usize].clone() } else { String::new() }
    }).collect();
    rebalance(port, m, &targets, m.exposure() / targets.len() as f64, &[], p.get("stop_pct"), &format!("setor forte: {}", names.join(", ")))
}

/// agents.agenda_scores: the political-agenda score of each of these sectors.
fn agenda_scores(m: &Mkt, sectors: &[(i32, bool)], p: &P) -> Vec<f64> {
    let d = m.data;
    // (sector, US?): the American stocks read the US columns, the European ones their STOXX Europe 600 sector's
    let get = |field: usize, (sec, us): (i32, bool)| -> Option<f64> {
        let cols = match (field, us) {
            (0, true) => &d.sector_rs, (1, true) => &d.sector_gov, (2, true) => &d.sector_epub, (3, true) => &d.sector_capex,
            (0, false) => &d.sector_eu_rs, (1, false) => &d.sector_eu_gov, (2, false) => &d.sector_eu_epub, _ => &d.sector_eu_capex,
        };
        let i = cols[sec as usize];
        if i < 0 { return None; }
        let x = d.ctx_val[m.d * d.n_ctx + i as usize];
        if x.is_nan() { None } else { Some(x) }
    };
    let rs: Vec<Option<f64>> = sectors.iter().map(|&g| get(0, g)).collect();
    let known: Vec<f64> = rs.iter().filter_map(|x| *x).collect();
    let mean = if known.is_empty() { 0.0 } else { crate::pyround::py_sum(known.iter().copied()) / known.len() as f64 };
    let sd = if known.is_empty() { 0.0 } else {
        let sq: Vec<f64> = known.iter().map(|&x| (x - mean) * (x - mean)).collect();
        (crate::pyround::py_sum(sq.iter().copied()) / known.len() as f64).sqrt()
    };
    sectors.iter().zip(rs.iter()).map(|(&g, r)| {
        let mom = match r { Some(x) if sd > 0.0 => (x - mean) / sd, _ => 0.0 };
        p.req("peso_gasto") * get(1, g).unwrap_or(0.0)
            + p.req("peso_incerteza") * get(2, g).unwrap_or(0.0)
            + p.req("peso_momentum") * mom
            - p.req("peso_capex") * get(3, g).unwrap_or(0.0)
    }).collect()
}

fn estratega(port: &mut Port, m: &Mkt, p: &P, region: i64) -> Vec<Order> {
    if !due(port, m, p.req("cada")) {
        return Vec::new();
    }
    let v = View::new(port, m, region);
    let mut sectors: Vec<(i32, bool)> = Vec::new();
    let mut members: Vec<Vec<usize>> = Vec::new();
    for &r in &v.list {
        let sec = v.rows.sector[r];
        let us = m.data.symbols[v.sym(r)].is_us;
        let has = if us { &m.data.sector_has_key } else { &m.data.sector_eu_has_key };
        if sec < 0 || has[sec as usize] == 0 {
            continue;
        }
        match sectors.iter().position(|&x| x == (sec, us)) {
            Some(i) => members[i].push(r),
            None => {
                sectors.push((sec, us));
                members.push(vec![r]);
            }
        }
    }
    let min_stocks = p.get("min_acoes_setor").unwrap_or(5.0);
    let keep: Vec<usize> = (0..sectors.len()).filter(|&i| members[i].len() as f64 >= min_stocks).collect();
    let secs: Vec<(i32, bool)> = keep.iter().map(|&i| sectors[i]).collect();
    let score = agenda_scores(m, &secs, p);
    let mut order: Vec<usize> = (0..secs.len()).collect();
    sort_by_key(&mut order, |k| score[k], true);
    let top: Vec<usize> = order.into_iter().filter(|&k| score[k] > 0.0).take(p.req("setores") as usize).collect();
    let mut targets: Vec<usize> = Vec::new();
    for &k in &top {
        let mut ranked = members[keep[k]].clone();
        sort_by_key(&mut ranked, |r| 1000.0 * (v.rows.stage2[r] as i32 as f64) + v.rows.trend[r], true);
        targets.extend(ranked.into_iter().take(p.req("por_setor") as usize).map(|r| v.sym(r)));
    }
    let Some(&core) = m.data.by_name.get("SXR8.DE") else { return Vec::new() };
    let nucleo = p.req("nucleo");
    let zone = if p.on("segue_zona") { m.exposure() } else { 1.0 };
    let weight = (1.0 - nucleo) * zone / py_max(targets.len() as f64, 1.0);
    let names: Vec<String> = top.iter().map(|&k| {
        let (sec, us) = secs[k];
        format!("{}{}", m.data.sectors_pt[sec as usize], if us { "" } else { " (Europa)" })
    }).collect();
    let mut orders = rebalance(port, m, &targets, weight, &[core], p.get("stop_pct"), &format!("agenda: {}", names.join(", ")));
    let eq = equity(port, m);
    let pos = port.positions.get(&core);
    let current = match pos { Some(pos) => position_value(m, core, pos) / eq, None => 0.0 };
    let target = nucleo + (1.0 - nucleo) * (if targets.is_empty() { 1.0 } else { 0.0 });
    let folga = p.req("folga");
    if current < target - folga {
        orders.push(order_buy(core, (target - current) * eq, "núcleo S&P 500", None, None));
    } else if pos.is_some() && current > target + folga {
        let mut o = order_sell(core, "núcleo S&P 500");
        o.fraction = (current - target) / current;
        orders.push(o);
    }
    orders
}

/// agents.REGIME_ALLOC
const REGIME_ALLOC: &[(&str, &[(&str, f64)])] = &[
    ("Concentração", &[("SXRV.DE", 0.6), ("SXR8.DE", 0.4)]),
    ("Alargamento", &[("SXR8.DE", 0.4), ("ZPRR.DE", 0.3), ("IS3S.DE", 0.3)]),
    ("Contração", &[("SXR8.DE", 0.4), ("EUNH.DE", 0.3), ("4GLD.DE", 0.3)]),
    ("Inflação", &[("SXR8.DE", 0.5), ("4GLD.DE", 0.25), ("XDW0.DE", 0.25)]),
    ("Transição", &[("SXR8.DE", 1.0)]),
];

/// agents.regime_of
fn regime_of(m: &Mkt, p: &P) -> &'static str {
    let c = |k: &str| m.data.context(m.d, k);
    let (conc, size, credit, cycle, corr) = (c("reg_conc"), c("reg_size"), c("reg_credit"), c("reg_cycle"), c("reg_corr"));
    if credit.is_some_and(|x| x < -p.req("limiar_credito")) || cycle.is_some_and(|x| x < -p.req("limiar_ciclo")) {
        return "Contração";
    }
    if corr.is_some_and(|x| x > p.req("limiar_corr")) {
        return "Inflação";
    }
    if conc.is_some_and(|x| x < -p.req("limiar_conc")) {
        return "Concentração";
    }
    if conc.is_some_and(|x| x > p.req("limiar_conc")) && size.is_some_and(|x| x > 0.0) {
        return "Alargamento";
    }
    "Transição"
}

fn navegador(port: &mut Port, m: &Mkt, p: &P) -> Vec<Order> {
    if !due(port, m, p.req("cada")) {
        return Vec::new();
    }
    let Some(&core) = m.data.by_name.get("SXR8.DE") else { return Vec::new() };
    if m.etf(core).is_none() {
        return Vec::new();
    }
    let regime = regime_of(m, p);
    let plan = REGIME_ALLOC.iter().find(|(r, _)| *r == regime).map(|(_, a)| *a).unwrap_or(&[]);
    let mut alloc: Vec<(usize, f64)> = Vec::new();
    for &(name, w) in plan {
        let key = match m.data.by_name.get(name) { Some(&s) if m.etf(s).is_some() => s, _ => core };
        match alloc.iter_mut().find(|(s, _)| *s == key) {
            Some(e) => e.1 += w,
            None => alloc.push((key, w)),
        }
    }
    let reason = format!("regime {regime}");
    let eq = equity(port, m);
    let mut orders: Vec<Order> = port.positions.keys().filter(|s| !alloc.iter().any(|(a, _)| a == *s))
        .map(|&s| order_sell(s, &reason)).collect();
    let folga = p.req("folga");
    for &(s, w) in &alloc {
        let pos = port.positions.get(&s);
        let current = match pos { Some(pos) => position_value(m, s, pos) / eq, None => 0.0 };
        if w - current > folga {
            orders.push(order_buy(s, (w - current) * eq, &reason, None, None));
        } else if pos.is_some() && current - w > folga {
            let mut o = order_sell(s, &reason);
            o.fraction = (current - w) / current;
            orders.push(o);
        }
    }
    orders
}

fn time_sells(port: &Port, m: &Mkt, max_days: f64) -> Vec<Order> {
    port.positions.iter()
        .filter(|(_, pos)| (m.data.sessions_since(m.d, Some(pos.entry_date)) as f64) >= max_days)
        .map(|(&s, _)| order_sell(s, &format!("{} sessões", num(max_days))))
        .collect()
}

fn pescador(port: &mut Port, m: &Mkt, p: &P, region: i64) -> Vec<Order> {
    let mut orders = time_sells(port, m, p.req("max_dias"));
    if m.zone == 3 {
        return orders;
    }
    let v = View::new(port, m, region);
    let f = m.f;
    let w1 = v.field("ret_1w");
    let mut picks: Vec<usize> = v.list.iter().copied().filter(|&r| {
        v.v(r, f.price) > v.v(r, f.sma200) && v.v(r, f.sma50) > v.v(r, f.sma200) && v.v(r, w1) <= -p.req("queda_pct")
            && v.rows.trend[r] >= 60.0 && !port.positions.contains_key(&v.sym(r))
    }).collect();
    let eq = equity(port, m);
    sort_by_key(&mut picks, |r| v.v(r, w1), false);
    let slots = py_max(free_slots(port, p.req("n")) as f64, 0.0) as usize;
    for &r in picks.iter().take(slots) {
        let price = v.v(r, f.price);
        let why = format!("caiu {:.0}% na semana", v.v(r, w1) * 100.0);
        orders.push(order_buy(v.sym(r), eq / p.req("n"), &why, Some(round_to(price * (1.0 - p.req("stop_pct")), 4)),
                              Some(round_to(price * (1.0 + p.req("alvo_pct")), 4))));
    }
    orders
}

fn deriva(port: &mut Port, m: &Mkt, p: &P, region: i64) -> Vec<Order> {
    let mut orders = time_sells(port, m, p.req("max_dias"));
    if m.zone == 3 {
        return orders;
    }
    let v = View::new(port, m, region);
    let f = m.f;
    let (gs, ga, gl) = (v.field("gap_size"), v.field("gap_age"), v.field("gap_low"));
    let mut picks: Vec<usize> = v.list.iter().copied().filter(|&r| {
        let age = v.v(r, ga).trunc();
        v.v(r, gs) >= p.req("salto_pct") && (0.0..=3.0).contains(&age) && v.v(r, f.price) > v.v(r, gl)
            && v.v(r, f.price) > v.v(r, f.sma50) && !port.positions.contains_key(&v.sym(r))
    }).collect();
    let eq = equity(port, m);
    sort_by_key(&mut picks, |r| v.v(r, gs), true);
    let slots = py_max(free_slots(port, p.req("n")) as f64, 0.0) as usize;
    for &r in picks.iter().take(slots) {
        let why = format!("salto de {:.0}% com volume", v.v(r, gs) * 100.0);
        orders.push(order_buy(v.sym(r), eq / p.req("n"), &why, Some(round_to(v.v(r, gl) * 0.99, 4)), None));
    }
    orders
}

fn kamikaze(port: &mut Port, m: &Mkt, p: &P, region: i64) -> Vec<Order> {
    if !due(port, m, p.req("cada")) {
        return Vec::new();
    }
    let v = View::new(port, m, region);
    let f = m.f;
    let r3 = v.field("ret_3m");
    let mut ranked: Vec<usize> = v.list.iter().copied().filter(|&r| v.v(r, f.price) > v.v(r, f.sma50)).collect();
    sort_by_key(&mut ranked, |r| v.v(r, r3), true);
    let n = p.req("n") as usize;
    top_rebalance(port, m, ranked, &v, n, 0, 1.0 / n as f64, p.get("stop_pct"), "maior subida a 3 meses")
}
