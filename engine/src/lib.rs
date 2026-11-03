//! One Guillion Jollars: the agents' engine in Rust, used from Python (scripts/engine.py builds the data).

pub mod data;
pub mod engine;
pub mod pyround;
pub mod strategies;

use std::collections::HashMap;

use numpy::PyReadonlyArray1;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use data::{Bars, Data, Rates, Rows, Symbol};
use engine::{P, Port};

fn arr<'py, T: numpy::Element + Copy>(d: &Bound<'py, PyDict>, k: &str) -> PyResult<Vec<T>> {
    let v = d.get_item(k)?.ok_or_else(|| PyValueError::new_err(format!("falta {k}")))?;
    let a: PyReadonlyArray1<T> = v.extract()?;
    Ok(a.as_slice()?.to_vec())
}

fn list<'py, T: for<'a> FromPyObjectOwned<'a>>(d: &Bound<'py, PyDict>, k: &str) -> PyResult<Vec<T>> {
    let v = d.get_item(k)?.ok_or_else(|| PyValueError::new_err(format!("falta {k}")))?;
    v.extract()
}

use pyo3::conversion::FromPyObjectOwned;

fn rows(d: &Bound<'_, PyDict>, prefix: &str, n_days: usize, n_syms: usize) -> PyResult<Rows> {
    let mut r = Rows {
        off: arr::<i64>(d, &format!("{prefix}_off"))?.into_iter().map(|x| x as usize).collect(),
        sym: arr::<u32>(d, &format!("{prefix}_sym"))?,
        sector: arr::<i32>(d, &format!("{prefix}_sector"))?,
        val: arr::<f32>(d, &format!("{prefix}_val"))?,
        trend: arr::<f64>(d, &format!("{prefix}_trend"))?,
        stage2: arr::<u8>(d, &format!("{prefix}_stage2"))?.into_iter().map(|x| x != 0).collect(),
        present: arr::<u64>(d, &format!("{prefix}_present"))?,
        at: Vec::new(),
    };
    Data::index_rows(n_days, n_syms, &mut r);
    Ok(r)
}

/// The market data of one backtest (see scripts/engine.py for the layout).
#[pyclass(frozen)]
pub struct EngineData {
    inner: Data,
}

#[pymethods]
impl EngineData {
    #[new]
    fn new(d: &Bound<'_, PyDict>) -> PyResult<Self> {
        let fields: Vec<String> = list(d, "fields")?;
        let names: Vec<String> = list(d, "sym_names")?;
        let ccy = arr::<i32>(d, "sym_ccy")?;
        let pence = arr::<u8>(d, "sym_pence")?;
        let usd = arr::<u8>(d, "sym_usd")?;
        let fee = arr::<f64>(d, "sym_fee")?;
        let buy_tax = arr::<f64>(d, "sym_buy_tax")?;
        let is_us = arr::<u8>(d, "sym_is_us")?;
        let is_etf = arr::<u8>(d, "sym_is_etf")?;
        let symbols: Vec<Symbol> = (0..names.len()).map(|i| Symbol {
            name: names[i].clone(), ccy: if ccy[i] < 0 { None } else { Some(ccy[i] as usize) }, pence: pence[i] != 0,
            currency_usd: usd[i] != 0, fee: fee[i], buy_tax: buy_tax[i], is_us: is_us[i] != 0, is_etf: is_etf[i] != 0,
        }).collect();
        let n_syms = symbols.len();
        let days = arr::<i32>(d, "days")?;
        let n_days = days.len();
        let bar_off = arr::<i64>(d, "bar_off")?;
        let (bd, bo, bh, bl, bc) = (arr::<i32>(d, "bar_day")?, arr::<f64>(d, "bar_open")?, arr::<f64>(d, "bar_high")?,
                                    arr::<f64>(d, "bar_low")?, arr::<f64>(d, "bar_close")?);
        let bars = (0..n_syms).map(|i| {
            let (a, b) = (bar_off[i] as usize, bar_off[i + 1] as usize);
            Bars { day: bd[a..b].to_vec(), open: bo[a..b].to_vec(), high: bh[a..b].to_vec(), low: bl[a..b].to_vec(), close: bc[a..b].to_vec() }
        }).collect();
        let fx_off = arr::<i64>(d, "fx_off")?;
        let (fns, frate, ffirst) = (arr::<i64>(d, "fx_ns")?, arr::<f64>(d, "fx_rate")?, arr::<f64>(d, "fx_first")?);
        let fx = (0..ffirst.len()).map(|i| {
            let (a, b) = (fx_off[i] as usize, fx_off[i + 1] as usize);
            Rates { ns: fns[a..b].to_vec(), rate: frate[a..b].to_vec(), first: ffirst[i] }
        }).collect();
        let ctx_fields: Vec<String> = list(d, "ctx_fields")?;
        let earnings = if d.get_item("earn_off")?.is_some() {
            let off = arr::<i64>(d, "earn_off")?;
            let day = arr::<i32>(d, "earn_day")?;
            Some((0..n_syms).map(|i| day[off[i] as usize..off[i + 1] as usize].to_vec()).collect())
        } else {
            None
        };
        let data = Data {
            n_fields: fields.len(),
            fields: fields.iter().enumerate().map(|(i, f)| (f.clone(), i)).collect(),
            nullable: arr::<u8>(d, "nullable")?.into_iter().map(|x| x != 0).collect(),
            symbols,
            sectors: list(d, "sectors")?,
            sector_unknown: d.get_item("sector_unknown")?.map(|x| x.extract()).transpose()?.unwrap_or(-1),
            sector_trend: arr::<i32>(d, "sector_trend")?,
            sector_rs: arr::<i32>(d, "sector_rs")?,
            zone: arr::<u8>(d, "zone")?,
            sessions: arr::<i32>(d, "sessions")?,
            stocks: rows(d, "stock", n_days, n_syms)?,
            etfs: rows(d, "etf", n_days, n_syms)?,
            n_syms,
            closes: arr::<f64>(d, "closes")?,
            bars,
            fx,
            bench: arr::<f64>(d, "bench")?,
            n_ctx: ctx_fields.len(),
            ctx: ctx_fields.iter().enumerate().map(|(i, f)| (f.clone(), i)).collect(),
            ctx_val: arr::<f64>(d, "ctx_val")?,
            earnings,
            day_index: days.iter().enumerate().map(|(i, &x)| (x, i)).collect(),
            days,
        };
        if data.closes.len() != n_days * n_syms || data.bench.len() != n_days || data.zone.len() != n_days {
            return Err(PyValueError::new_err("dimensões inconsistentes"));
        }
        Ok(EngineData { inner: data })
    }

    #[getter]
    fn n_days(&self) -> usize {
        self.inner.days.len()
    }
}

fn parse_params(zones: Vec<String>) -> PyResult<Vec<P>> {
    zones.iter().map(|z| {
        let v: serde_json::Value = serde_json::from_str(z).map_err(|e| PyValueError::new_err(e.to_string()))?;
        let mut out = HashMap::new();
        if let serde_json::Value::Object(map) = v {
            for (k, x) in map {
                let n = match x {
                    serde_json::Value::Number(n) => n.as_f64(),
                    serde_json::Value::Bool(b) => Some(if b { 1.0 } else { 0.0 }),
                    serde_json::Value::Null => None,
                    _ => None,
                };
                if let Some(n) = n {
                    out.insert(k, n);
                }
            }
        }
        Ok(P(out))
    }).collect()
}

/// The day loop of pretrain.simulate over days [start, end) of the data: returns what the Python wrapper needs to
/// grade the run (trades, the equity history, points, deepest fall, tax owed if sold at the end, ...).
#[pyfunction]
#[pyo3(signature = (data, strategy, zone_params, start, end, capital=1000.0))]
fn simulate<'py>(py: Python<'py>, data: &EngineData, strategy: &str, zone_params: Vec<String>, start: usize, end: usize,
                 capital: f64) -> PyResult<Bound<'py, PyDict>> {
    if !strategies::SUPPORTED.contains(&strategy) {
        return Err(PyValueError::new_err(format!("estratégia não portada: {strategy}")));
    }
    let params = parse_params(zone_params)?;
    let d = &data.inner;
    if start >= end || end > d.days.len() {
        return Err(PyValueError::new_err("intervalo de dias inválido"));
    }
    let f = d.f();
    let r = py.detach(|| run(d, &f, strategy, &params, start, end, capital));
    let out = PyDict::new(py);
    let trades = PyList::empty(py);
    for t in &r.port.trades {
        let sym = t.sym.map(|s| d.symbols[s].name.clone());
        trades.append((t.day, t.side, sym, t.qty, t.price, t.eur, t.pnl, t.reason.clone(), t.ret, t.held))?;
    }
    out.set_item("trades", trades)?;
    out.set_item("n_trades", r.n_trades)?;
    out.set_item("history", r.port.hist.iter().map(|&(a, b)| (a, b)).collect::<Vec<_>>())?;
    out.set_item("points", r.port.points)?;
    out.set_item("max_dd", r.port.max_dd)?;
    out.set_item("final", r.final_equity)?;
    out.set_item("tax_owed", r.tax_owed)?;
    out.set_item("bankrupt", r.bankrupt)?;
    out.set_item("zone_stats", r.port.zone_stats.iter().map(|&(a, b)| (a, b)).collect::<Vec<_>>())?;
    Ok(out)
}

pub struct RunResult {
    pub port: Port,
    pub n_trades: usize,
    pub final_equity: f64,
    pub tax_owed: f64,
    pub bankrupt: Option<i32>,
}

/// The day loop (pretrain.simulate).
pub fn run(d: &Data, f: &data::F, strategy: &str, params: &[P], start: usize, end: usize, capital: f64) -> RunResult {
    let mut port = Port::new(capital);
    let mut n_trades = 0usize;
    let mut bankrupt: Option<i32> = None;
    for di in start..end {
        let zone = d.zone[di] as usize;
        let m = engine::Mkt { data: d, f, d: di, day: d.days[di], zone };
        let p = &params[zone];
        let mut done = engine::fill_pending(&mut port, &m).len();
        done += engine::check_exits(&mut port, &m).len();
        engine::trail_stops(&mut port, &m, p);
        n_trades += done;
        engine::pay_taxes(&mut port, &m);
        engine::score_day(&mut port, &m);
        if bankrupt.is_none() && strategy != "indexador" && engine::is_bankrupt(&port, &m) {
            n_trades += engine::liquidate(&mut port, &m, "falência").len();
            bankrupt = Some(m.day);
        }
        if bankrupt.is_none() && port.pending.is_empty() {
            let orders = engine::decide(strategy, &mut port, &m, p);
            port.pending = orders.into_iter().map(|mut o| { o.created = m.day; o }).collect();
        }
    }
    let last = end - 1;
    let m = engine::Mkt { data: d, f, d: last, day: d.days[last], zone: d.zone[last] as usize };
    let final_equity = engine::equity(&port, &m);
    let tax_owed = engine::tax_owed_if_sold(&port, &m);
    RunResult { port, n_trades, final_equity, tax_owed, bankrupt }
}

#[pymodule]
fn ogj_engine(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<EngineData>()?;
    m.add_function(wrap_pyfunction!(simulate, m)?)?;
    m.add("SUPPORTED", strategies::SUPPORTED.to_vec())?;
    Ok(())
}
