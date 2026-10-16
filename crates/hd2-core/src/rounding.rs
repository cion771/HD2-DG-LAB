/// Python-compatible round(x) for integral results: halfway cases go to even.
pub fn round_ties_even(value: f64) -> i64 {
    if !value.is_finite() {
        return 0;
    }
    let floor = value.floor();
    let frac = value - floor;
    if frac < 0.5 {
        floor as i64
    } else if frac > 0.5 {
        (floor + 1.0) as i64
    } else {
        let n = floor as i64;
        if n % 2 == 0 { n } else { n + 1 }
    }
}
pub fn bankers_round(value: f64) -> i64 {
    round_ties_even(value)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn python_ties() {
        for (x, y) in [
            (0.5, 0),
            (1.5, 2),
            (2.5, 2),
            (3.5, 4),
            (-0.5, 0),
            (-1.5, -2),
            (-2.5, -2),
        ] {
            assert_eq!(round_ties_even(x), y)
        }
    }
}
