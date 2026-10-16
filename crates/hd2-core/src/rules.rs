use crate::config::{AppConfig, RuleConfig};
use crate::events::Event;
use crate::wave_lib::WaveLibrary;
use crate::waves;
use std::collections::BTreeMap;
#[derive(Debug, Clone, PartialEq)]
pub struct Action {
    pub name: String,
    pub pct: f64,
    pub units: Vec<String>,
    pub duration_ms: f64,
    pub attack_ms: f64,
    pub release_ms: f64,
    pub channel: String,
    pub priority: i32,
    pub repeat_ms: f64,
    pub period_ms: f64,
    pub created: f64,
}
#[derive(Debug, Default)]
pub struct RuleEngine {
    last: BTreeMap<String, f64>,
    alternate: u8,
}
impl RuleEngine {
    pub fn reset_cooldowns(&mut self) {
        self.last.clear()
    }
    fn cooldown_ok(&mut self, key: &str, ms: f64, now: f64) -> bool {
        if self
            .last
            .get(key)
            .is_some_and(|last| (now - last) * 1000. < ms)
        {
            return false;
        }
        self.last.insert(key.into(), now);
        true
    }
    fn channel(&mut self, cfg: &AppConfig, r: &RuleConfig) -> String {
        match r.channel.as_str() {
            "default" => cfg.device.channel.clone(),
            "alternate" => {
                self.alternate ^= 1;
                if self.alternate == 1 {
                    "A".into()
                } else {
                    "B".into()
                }
            }
            x => x.into(),
        }
    }
    fn make(&mut self, cfg: &AppConfig, name: &str, r: &RuleConfig, pct: f64, now: f64) -> Action {
        let duration = r.duration_ms.clamp(100., cfg.safety.max_event_ms);
        let lib = WaveLibrary::new(&cfg.waves);
        let units = lib
            .build(&r.wave, duration, 100., r.freq)
            .or_else(|| waves::build(&r.wave, duration, 100., r.freq).ok())
            .unwrap_or_else(|| waves::build("pinch", duration, 100., r.freq).unwrap());
        Action {
            name: name.into(),
            pct: pct.max(0.),
            units,
            duration_ms: duration,
            attack_ms: cfg.safety.attack_ms,
            release_ms: cfg.safety.release_ms,
            channel: self.channel(cfg, r),
            priority: r.priority,
            repeat_ms: r.repeat_ms,
            period_ms: r.period_ms,
            created: now,
        }
    }
    pub fn actions(&mut self, cfg: &AppConfig, e: &Event, now: f64) -> Vec<Action> {
        let (name, pct, key) = match e {
            Event::Damage { severity, .. } => {
                let r = cfg.rules.get("damage");
                if r.is_none() || *severity < cfg.detect.damage_min_pct {
                    return vec![];
                }
                let r = r.unwrap();
                (
                    "damage",
                    r.base_pct + r.per_10hp * (*severity / 10.),
                    "damage".into(),
                )
            }
            Event::LimbInjury { slot, bleeding, .. } => {
                let Some(r) = cfg.rules.get("limb_injury") else {
                    return vec![];
                };
                (
                    "limb_injury",
                    r.base_pct * if *bleeding { 1.2 } else { 1. },
                    format!("limb_injury:{slot}"),
                )
            }
            Event::Death { .. } => {
                let Some(r) = cfg.rules.get("death") else {
                    return vec![];
                };
                ("death", r.base_pct, "death".into())
            }
            Event::LowHealth { .. } => {
                let Some(r) = cfg.rules.get("low_health") else {
                    return vec![];
                };
                ("low_health", r.base_pct, "low_health".into())
            }
            _ => return vec![],
        };
        let r = &cfg.rules[name];
        if !r.enabled || !self.cooldown_ok(&key, r.cooldown_ms, now) {
            return vec![];
        }
        vec![self.make(cfg, name, r, pct, now)]
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn parity() {
        let c = AppConfig::default();
        let mut r = RuleEngine::default();
        let a = r.actions(
            &c,
            &Event::Damage {
                t: 0.,
                severity: 10.,
                hp_before: 1.,
                hp_after: 0.9,
            },
            100.,
        );
        assert_eq!(a[0].pct, 23.);
        assert_eq!(a[0].channel, "A");
        assert_eq!(a[0].units.len(), 4);
        assert!(
            r.actions(
                &c,
                &Event::Damage {
                    t: 0.,
                    severity: 10.,
                    hp_before: 1.,
                    hp_after: 0.9
                },
                100.2
            )
            .is_empty()
        )
    }
    #[test]
    fn cooldown_per_limb() {
        let c = AppConfig::default();
        let mut r = RuleEngine::default();
        assert_eq!(
            r.actions(
                &c,
                &Event::LimbInjury {
                    t: 0.,
                    slot: 1,
                    bleeding: false,
                    name: String::new()
                },
                1.
            )
            .len(),
            1
        );
        assert_eq!(
            r.actions(
                &c,
                &Event::LimbInjury {
                    t: 0.,
                    slot: 2,
                    bleeding: false,
                    name: String::new()
                },
                1.1
            )
            .len(),
            1
        )
    }
}
