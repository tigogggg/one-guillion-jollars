//! The market data of a backtest, in columns: built once (from Python) and shared by every simulation.

use std::collections::HashMap;

pub const ZONES: [&str; 4] = ["Alta", "Neutra", "Cautelosa", "Queda"];
pub const EXPOSURE: [f64; 4] = [1.0, 0.7, 0.4, 0.1];

/// One listing.
pub struct Symbol {
    pub name: String,
    /// Index into `Data::fx` (None: quoted in euros).
    pub ccy: Option<usize>,
    /// Quoted in pence: divide by 100 after conversion.
    pub pence: bool,
    pub currency_usd: bool,
    pub fee: f64,
    pub buy_tax: f64,
    pub is_us: bool,
    pub is_etf: bool,
}

/// Daily bars of one symbol (its own trading days).
#[derive(Default)]
pub struct Bars {
    pub day: Vec<i32>,
    pub open: Vec<f64>,
    pub high: Vec<f64>,
    pub low: Vec<f64>,
    pub close: Vec<f64>,
}

/// Units of a currency per euro over time (NaN dropped), and the series' first value.
pub struct Rates {
    pub ns: Vec<i64>,
    pub rate: Vec<f64>,
    pub first: f64,
}

/// The stocks (or ETFs) in play on each day: rows of metrics, in the Python dict's order.
#[derive(Default)]
pub struct Rows {
    pub off: Vec<usize>,
    pub sym: Vec<u32>,
    pub sector: Vec<i32>,
    pub val: Vec<f32>,
    /// trend_score and stage2, kept apart: Python holds them as full floats and bools, not in the float32 table.
    pub trend: Vec<f64>,
    pub stage2: Vec<bool>,
    /// Fields each symbol's table has (bit i = field i); a missing one reads as None, like Row.get.
    pub present: Vec<u64>,
    /// Row of each symbol on each day (days x symbols, -1: not in play).
    pub at: Vec<i32>,
}

impl Rows {
    pub fn range(&self, d: usize) -> std::ops::Range<usize> {
        self.off[d]..self.off[d + 1]
    }
}

pub struct Data {
    pub fields: HashMap<String, usize>,
    pub n_fields: usize,
    /// Fields whose NaN means "unknown" (None): the fundamentals.
    pub nullable: Vec<bool>,
    pub symbols: Vec<Symbol>,
    pub sectors: Vec<String>,
    pub sector_unknown: i32,
    /// Context columns of each sector's trend and relative strength (filtro_setor), -1 when unknown.
    pub sector_trend: Vec<i32>,
    pub sector_rs: Vec<i32>,
    pub days: Vec<i32>,
    pub zone: Vec<u8>,
    pub sessions: Vec<i32>,
    pub stocks: Rows,
    pub etfs: Rows,
    pub n_syms: usize,
    /// Close of every symbol on each day, forward-filled (NaN: no price).
    pub closes: Vec<f64>,
    pub bars: Vec<Bars>,
    pub fx: Vec<Rates>,
    /// The benchmark's close at the end of each day (NaN: none).
    pub bench: Vec<f64>,
    pub ctx: HashMap<String, usize>,
    pub n_ctx: usize,
    pub ctx_val: Vec<f64>,
    /// Past earnings dates per symbol (sorted), when the markets carry them.
    pub earnings: Option<Vec<Vec<i32>>>,
    pub day_index: HashMap<i32, usize>,
}

/// Field indexes the engine reads, resolved once.
pub struct F {
    pub price: usize,
    pub sma50: usize,
    pub sma200: usize,
    pub sma200_rising: usize,
    pub pct_from_high: usize,
    pub vol20: usize,
    pub atr14: usize,
    pub support: Option<usize>,
    pub ema21: Option<usize>,
    pub ema50: Option<usize>,
    pub fund_q: Option<usize>,
    pub fund_v: Option<usize>,
}

impl Data {
    pub fn f(&self) -> F {
        let req = |k: &str| *self.fields.get(k).unwrap_or_else(|| panic!("campo em falta: {k}"));
        let opt = |k: &str| self.fields.get(k).copied();
        F {
            price: req("price"),
            sma50: req("sma50"),
            sma200: req("sma200"),
            sma200_rising: req("sma200_rising"),
            pct_from_high: req("pct_from_high"),
            vol20: req("vol20"),
            atr14: req("atr14"),
            support: opt("support"),
            ema21: opt("ema21"),
            ema50: opt("ema50"),
            fund_q: opt("fund_q"),
            fund_v: opt("fund_v"),
        }
    }

    /// A metric of a row (as Python reads it from the float32 table).
    #[inline]
    pub fn val(&self, rows: &Rows, row: usize, field: usize) -> f64 {
        rows.val[row * self.n_fields + field] as f64
    }

    /// `m.get(field)` as Python's Row: None when the symbol's table lacks it or a nullable value is NaN.
    #[inline]
    pub fn get(&self, rows: &Rows, row: usize, field: usize) -> Option<f64> {
        if rows.present[rows.sym[row] as usize] & (1u64 << field) == 0 {
            return None;
        }
        let x = self.val(rows, row, field);
        if x.is_nan() && self.nullable[field] {
            None
        } else {
            Some(x)
        }
    }

    /// The close of a symbol on a day (None: no price, as PriceRow.get).
    #[inline]
    pub fn close(&self, d: usize, s: usize) -> Option<f64> {
        let x = self.closes[d * self.n_syms + s];
        if x.is_nan() {
            None
        } else {
            Some(x)
        }
    }

    /// A context value (None when missing or NaN).
    pub fn context(&self, d: usize, key: &str) -> Option<f64> {
        let i = *self.ctx.get(key)?;
        let x = self.ctx_val[d * self.n_ctx + i];
        if x.is_nan() {
            None
        } else {
            Some(x)
        }
    }

    /// agents.to_eur: a price in the symbol's currency, in euros on `day`.
    pub fn to_eur(&self, s: usize, price: f64, day: i32) -> f64 {
        let sym = &self.symbols[s];
        let Some(c) = sym.ccy else { return price };
        let r = &self.fx[c];
        let at = day as i64 * 86_400_000_000_000;
        let i = r.ns.partition_point(|&t| t <= at);
        let rate = if i > 0 && !r.rate.is_empty() { r.rate[i - 1] } else { r.first };
        let eur = price / rate;
        if sym.pence {
            eur / 100.0
        } else {
            eur
        }
    }

    /// Sessions after `since` up to the day (agents.sessions_since, with mkt.sessions up to that day).
    pub fn sessions_since(&self, d: usize, since: Option<i32>) -> i64 {
        let Some(since) = since else { return 1_000_000 };
        let upto = self.sessions.partition_point(|&x| x <= self.days[d]);
        let after = self.sessions.partition_point(|&x| x <= since);
        upto as i64 - after.min(upto) as i64
    }

    /// The row of a symbol among the day's stocks (or ETFs).
    #[inline]
    pub fn find(&self, rows: &Rows, d: usize, s: usize) -> Option<usize> {
        let r = rows.at[d * self.n_syms + s];
        if r < 0 {
            None
        } else {
            Some(r as usize)
        }
    }

    /// Fills `Rows::at` (after the rows are loaded).
    pub fn index_rows(n_days: usize, n_syms: usize, rows: &mut Rows) {
        rows.at = vec![-1; n_days * n_syms];
        for d in 0..n_days {
            for r in rows.off[d]..rows.off[d + 1] {
                let i = d * n_syms + rows.sym[r] as usize;
                if rows.at[i] < 0 {
                    rows.at[i] = r as i32;
                }
            }
        }
    }
}
