//! Python's numeric semantics where the engine must match the Python reference exactly.

/// Python's `round(x, n)`: the float closest to x rounded half-even at n decimals, computed on the exact binary value
/// (as CPython does, via a correctly rounded decimal string).
pub fn round_to(x: f64, n: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{:.*}", n, x).parse::<f64>().unwrap_or(x)
}

/// Python's `round(x)` to an integer (half to even).
pub fn round_int(x: f64) -> i64 {
    x.round_ties_even() as i64
}

/// Python's `min(a, b)`: a unless b < a (so a NaN first argument is kept, a NaN second one ignored).
pub fn py_min(a: f64, b: f64) -> f64 {
    if b < a {
        b
    } else {
        a
    }
}

/// Python's `max(a, b)`: a unless b > a.
pub fn py_max(a: f64, b: f64) -> f64 {
    if b > a {
        b
    } else {
        a
    }
}

/// Python's built-in `sum()` of floats (3.12+: Neumaier's compensated summation, starting from the integer 0).
pub fn py_sum<I: IntoIterator<Item = f64>>(items: I) -> f64 {
    let mut it = items.into_iter();
    let Some(first) = it.next() else { return 0.0 };
    let mut total = 0.0 + first;
    let mut c = 0.0;
    for x in it {
        let t = total + x;
        if total.abs() >= x.abs() {
            c += (total - t) + x;
        } else {
            c += (x - t) + total;
        }
        total = t;
    }
    if c != 0.0 && c.is_finite() {
        total += c;
    }
    total
}

/// Python's truthiness of an optional float (None and 0.0 are false, NaN is true).
pub fn truthy(x: Option<f64>) -> bool {
    matches!(x, Some(v) if v != 0.0 || v.is_nan())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_dates() {
        assert_eq!(crate::engine::day_of(1970, 1, 1), 0);
        assert_eq!(crate::engine::day_of(2026, 8, 31), 20696);
    }

    #[test]
    fn rounding_matches_python() {
        assert_eq!(round_to(2.675, 2), 2.67); // 2.675 is 2.67499999... in binary
        assert_eq!(round_to(0.125, 2), 0.12); // exact half: to even
        assert_eq!(round_to(0.375, 2), 0.38);
        assert_eq!(round_int(2.5), 2);
        assert_eq!(round_int(-2.5), -2);
        assert_eq!(round_int(3.5), 4);
        assert_eq!(py_min(1.0, f64::NAN), 1.0);
        assert!(py_min(f64::NAN, 1.0).is_nan());
    }
}
