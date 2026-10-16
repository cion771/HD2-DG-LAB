use serde::{Deserialize, Serialize};
use serde_json::Value;
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Event {
    Damage {
        #[serde(default)]
        t: f64,
        severity: f64,
        hp_before: f64,
        hp_after: f64,
    },
    LimbInjury {
        #[serde(default)]
        t: f64,
        slot: usize,
        #[serde(default)]
        bleeding: bool,
        #[serde(default)]
        name: String,
    },
    Death {
        #[serde(default)]
        t: f64,
    },
    Revive {
        #[serde(default)]
        t: f64,
    },
    LowHealth {
        #[serde(default)]
        t: f64,
        ratio: f64,
    },
    Recovered {
        #[serde(default)]
        t: f64,
        ratio: f64,
    },
    Feedback {
        #[serde(default)]
        t: f64,
        index: usize,
    },
    DeviceState {
        #[serde(default)]
        t: f64,
        connected: bool,
        #[serde(default)]
        detail: String,
    },
}
impl Event {
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Damage { .. } => "damage",
            Self::LimbInjury { .. } => "limb_injury",
            Self::Death { .. } => "death",
            Self::Revive { .. } => "revive",
            Self::LowHealth { .. } => "low_health",
            Self::Recovered { .. } => "recovered",
            Self::Feedback { .. } => "feedback",
            Self::DeviceState { .. } => "device_state",
        }
    }
    pub fn t(&self) -> f64 {
        match self {
            Self::Damage { t, .. }
            | Self::LimbInjury { t, .. }
            | Self::Death { t }
            | Self::Revive { t }
            | Self::LowHealth { t, .. }
            | Self::Recovered { t, .. }
            | Self::Feedback { t, .. }
            | Self::DeviceState { t, .. } => *t,
        }
    }
}
pub fn positive_float(value: &Value, default: f64) -> f64 {
    match value {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.parse().ok(),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        _ => None,
    }
    .filter(|x| x.is_finite())
    .unwrap_or(default)
}
pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Bool(x) => *x,
        Value::Number(x) => x.as_f64().is_some_and(|n| n != 0.0),
        Value::String(x) => matches!(
            x.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
        _ => false,
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn wire_fields() {
        let e = Event::Damage {
            t: 1.0,
            severity: 5.0,
            hp_before: 1.0,
            hp_after: 0.95,
        };
        let v = serde_json::to_value(e).unwrap();
        assert_eq!(v["kind"], "damage");
        assert_eq!(v["severity"], 5.0);
    }
}
