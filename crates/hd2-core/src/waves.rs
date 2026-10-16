use crate::rounding::round_ties_even;
pub type PulseUnit = String;
pub const FREQ_MIN: f64 = 10.0;
pub const FREQ_MAX: f64 = 240.0;
pub const UNIT_MS: f64 = 100.0;
pub fn clamp_frequency(v: f64) -> u8 {
    round_ties_even(v.clamp(FREQ_MIN, FREQ_MAX)) as u8
}
pub fn clamp_intensity(v: f64) -> u8 {
    round_ties_even(v.clamp(0.0, 100.0)) as u8
}
pub fn unit(freq: f64, intensity: f64) -> String {
    unit4([freq; 4], [intensity; 4])
}
pub fn unit4(freq: [f64; 4], intensity: [f64; 4]) -> String {
    let mut s = String::with_capacity(16);
    for x in freq {
        s.push_str(&format!("{:02X}", clamp_frequency(x)))
    }
    for x in intensity {
        s.push_str(&format!("{:02X}", clamp_intensity(x)))
    }
    s
}
pub fn units_for(ms: f64) -> usize {
    round_ties_even(ms / UNIT_MS).max(1) as usize
}
pub fn constant(freq: f64, intensity: f64, ms: f64) -> Vec<String> {
    vec![unit(freq, intensity); units_for(ms)]
}
pub fn ramp(freq: f64, i_from: f64, i_to: f64, ms: f64, freq_to: Option<f64>) -> Vec<String> {
    let n = units_for(ms);
    let ft = freq_to.unwrap_or(freq);
    (0..n)
        .map(|k| {
            let r = k as f64 / (n.saturating_sub(1).max(1) as f64);
            unit(freq + (ft - freq) * r, i_from + (i_to - i_from) * r)
        })
        .collect()
}
pub fn pinch(freq: f64, intensity: f64, ms: f64) -> Vec<String> {
    (0..units_for(ms))
        .map(|_| unit4([freq; 4], [intensity, 0.0, 0.0, 0.0]))
        .collect()
}
pub fn breath(freq: f64, peak: f64, ms: f64) -> Vec<String> {
    let n = units_for(ms);
    (0..n)
        .map(|k| {
            let r = k as f64 / (n.saturating_sub(1).max(1) as f64);
            let i = if r < 0.4 {
                peak * r / 0.4
            } else if r < 0.7 {
                peak
            } else {
                peak * (1.0 - (r - 0.7) / 0.3)
            };
            unit(freq, i)
        })
        .collect()
}
pub fn heartbeat(freq: f64, peak: f64, ms: f64, period_ms: f64) -> Vec<String> {
    let n = units_for(ms);
    let p = ((period_ms / UNIT_MS) as i64).max(3) as usize;
    (0..n)
        .map(|k| {
            let pos = k % p;
            let i = if pos == 0 {
                peak
            } else if pos == 1 {
                peak * 0.45
            } else if pos == 2 && p > 6 {
                peak * 0.75
            } else {
                0.0
            };
            unit(freq, i)
        })
        .collect()
}
pub fn sting(freq: f64, peak: f64, ms: f64) -> Vec<String> {
    (0..units_for(ms))
        .map(|k| {
            unit(
                freq,
                if k == 0 {
                    peak * 0.7
                } else if k == 1 {
                    peak
                } else {
                    0.0
                },
            )
        })
        .collect()
}
pub fn death_pattern(freq: f64, peak: f64, ms: f64) -> Vec<String> {
    let n = units_for(ms);
    let stroke = (n / 4).max(3);
    let gap = if n > stroke * 3 {
        ((n - stroke * 3) / 3).max(1)
    } else {
        0
    };
    let mut out = Vec::new();
    for scale in [0.55, 0.8, 1.0] {
        let head = (stroke / 3).max(1);
        let body = (stroke - head).max(1);
        for _ in 0..head {
            out.push(unit(freq, peak * scale * 0.35))
        }
        for k in 0..body {
            out.push(unit(
                freq * (1.0 - 0.25 * k as f64 / (body.saturating_sub(1).max(1) as f64)),
                peak * scale,
            ))
        }
        for _ in 0..gap {
            out.push(unit(freq, 0.0))
        }
    }
    while out.len() < n {
        out.push(unit(freq, 0.0))
    }
    out.truncate(n);
    out
}
pub fn is_preset(name: &str) -> bool {
    matches!(
        name,
        "pinch" | "sting" | "buzz" | "ramp_up" | "breath" | "heartbeat" | "death"
    )
}
pub fn build(
    name: &str,
    duration_ms: f64,
    peak: f64,
    freq: Option<f64>,
) -> Result<Vec<String>, String> {
    let key = if name.trim().is_empty() {
        "pinch"
    } else {
        name.trim()
    }
    .to_ascii_lowercase();
    let peak = clamp_intensity(peak) as f64;
    let f = freq.unwrap_or(match key.as_str() {
        "pinch" => 60.0,
        "sting" => 120.0,
        "buzz" => 30.0,
        "ramp_up" => 20.0,
        "breath" => 10.0,
        "heartbeat" => 10.0,
        "death" => 25.0,
        _ => 30.0,
    });
    Ok(match key.as_str() {
        "pinch" => pinch(f, peak, duration_ms),
        "sting" => sting(f, peak, duration_ms),
        "buzz" => constant(f, peak, duration_ms),
        "ramp_up" => ramp(f, peak * 0.15, peak, duration_ms, None),
        "breath" => breath(f, peak, duration_ms),
        "heartbeat" => heartbeat(f, peak, duration_ms, 900.0),
        "death" => death_pattern(f, peak, duration_ms),
        _ => return Err(format!("unknown wave preset: {name}")),
    })
}
pub fn chunks(units: &[String], maximum: usize) -> Vec<Vec<String>> {
    assert!(maximum > 0);
    units.chunks(maximum).map(|x| x.to_vec()).collect()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn encoding_and_rounding() {
        assert_eq!(unit(10., 100.), "0A0A0A0A64646464");
        assert_eq!(clamp_intensity(2.5), 2);
        assert_eq!(clamp_intensity(3.5), 4);
        assert_eq!(units_for(250.), 2);
        assert_eq!(units_for(350.), 4)
    }
    #[test]
    fn exact_ramp() {
        assert_eq!(
            build("ramp_up", 200., 100., None).unwrap(),
            vec!["141414140F0F0F0F", "1414141464646464"]
        )
    }
    #[test]
    fn lengths() {
        assert_eq!(build("death", 4000., 100., None).unwrap().len(), 40)
    }
}
