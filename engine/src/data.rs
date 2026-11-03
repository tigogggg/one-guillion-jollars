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
    /// The broker's commission per order (agents.commission): a share of the value in euros with a minimum and a
    /// maximum, or (com_share > 0) per share in the quote currency with a minimum and a maximum share of the value.
    pub com_pct: f64,
    pub com_min: f64,
    pub com_max: f64,
    pub com_share: f64,
    pub com_share_min: f64,
    pub com_share_max_pct: f64,
    pub is_us: bool,
    pub is_etf: bool,
    /// agents.DEFENSIVE_ETFS (bonds, gold)
    pub defensive: bool,
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
    /// agents.SECTORS_PT of each sector (for the trades' reasons)
    pub sectors_pt: Vec<String>,
    pub by_name: HashMap<String, usize>,
    pub sector_unknown: i32,
    /// Context columns of each sector's trend and relative strength (filtro_setor), -1 when unknown.
    pub sector_trend: Vec<i32>,
    pub sector_rs: Vec<i32>,
    /// The political-agenda fields of each sector (policy.py): context column or -1; whether it has a sector ETF.
    pub sector_gov: Vec<i32>,
    pub sector_epub: Vec<i32>,
    pub sector_capex: Vec<i32>,
    pub sector_has_key: Vec<u8>,
    /// The same for a sector's European stocks (their STOXX Europe 600 sector against the STOXX Europe 600).
    pub sector_eu_trend: Vec<i32>,
    pub sector_eu_rs: Vec<i32>,
    pub sector_eu_gov: Vec<i32>,
    pub sector_eu_epub: Vec<i32>,
    pub sector_eu_capex: Vec<i32>,
    pub sector_eu_has_key: Vec<u8>,
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
    /// Slots taken during a session (the intraday rounds and the lab's checkpoints): the day so far.
    pub partial: Vec<bool>,
    /// Today's bar so far in a partial slot (slot x symbol; NaN: no bar today yet): its open and its low.
    pub part_open: Vec<f64>,
    pub part_low: Vec<f64>,
    /// Each symbol's exchange, and each exchange's session in each slot (agents.market_open, session_minutes).
    pub sym_ex: Vec<u16>,
    pub n_ex: usize,
    pub ex_open: Vec<bool>,
    pub ex_since: Vec<f64>,
    pub ex_to_close: Vec<f64>,
    /// The news signal's score of each symbol (news.py; 0 when none) and the VCP watchlist (live only).
    pub news: Vec<i32>,
    pub vcp: Vec<bool>,
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

    /// Today's bar so far of a symbol in a partial slot: (open, low).
    pub fn part_bar(&self, d: usize, s: usize) -> Option<(f64, f64)> {
        if !self.partial.get(d).copied().unwrap_or(false) || self.part_open.is_empty() {
            return None;
        }
        let i = d * self.n_syms + s;
        let o = self.part_open[i];
        if o.is_nan() && self.part_low[i].is_nan() {
            None
        } else {
            Some((o, self.part_low[i]))
        }
    }

    /// The bars a slot sees for a symbol after `after` (exclusive) up to the slot's day: the complete days, and in a
    /// partial slot today's bar so far instead of the complete one. (day, open, high, low, close); a partial bar has
    /// no high or close (NaN): nothing reads them during the session.
    pub fn bars_after(&self, d: usize, s: usize, after: i32) -> Vec<(i32, f64, f64, f64, f64)> {
        let b = &self.bars[s];
        let day = self.days[d];
        let partial = self.partial.get(d).copied().unwrap_or(false);
        let i0 = b.day.partition_point(|&x| x <= after);
        let i1 = if partial { b.day.partition_point(|&x| x < day) } else { b.day.partition_point(|&x| x <= day) };
        let mut out: Vec<(i32, f64, f64, f64, f64)> =
            (i0..i1.max(i0)).map(|i| (b.day[i], b.open[i], b.high[i], b.low[i], b.close[i])).collect();
        if partial && day > after {
            if let Some((o, l)) = self.part_bar(d, s) {
                out.push((day, o, f64::NAN, l, f64::NAN));
            }
        }
        out
    }

    /// The date of the last bar a slot sees for a symbol (today's when its session has a bar already), or None.
    pub fn last_bar_day(&self, d: usize, s: usize) -> Option<i32> {
        if self.part_bar(d, s).is_some() {
            return Some(self.days[d]);
        }
        let b = &self.bars[s];
        let day = self.days[d];
        let partial = self.partial.get(d).copied().unwrap_or(false);
        let i = if partial { b.day.partition_point(|&x| x < day) } else { b.day.partition_point(|&x| x <= day) };
        if i == 0 { None } else { Some(b.day[i - 1]) }
    }

    /// The close of the last complete session before today's bar (agents.move_today's reference), with how many bars
    /// the slot sees (Python needs at least two).
    pub fn prev_close(&self, d: usize, s: usize) -> Option<f64> {
        let b = &self.bars[s];
        let day = self.days[d];
        let partial = self.partial.get(d).copied().unwrap_or(false);
        let complete = if partial { b.day.partition_point(|&x| x < day) } else { b.day.partition_point(|&x| x <= day) };
        let today = self.part_bar(d, s).is_some() || (!partial && complete > 0 && b.day[complete - 1] == day);
        let seen = complete + usize::from(self.part_bar(d, s).is_some());
        if seen < 2 {
            return None;
        }
        // the last bar is today's: the one before it; otherwise the last one
        let idx = if today && !partial { complete - 2 } else { complete - 1 };
        Some(b.close[idx])
    }

    pub fn exchange(&self, d: usize, s: usize) -> (bool, f64, f64) {
        let e = d * self.n_ex + self.sym_ex[s] as usize;
        (self.ex_open[e], self.ex_since[e], self.ex_to_close[e])
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
