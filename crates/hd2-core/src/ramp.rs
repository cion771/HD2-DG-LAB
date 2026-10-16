use crate::config::RampConfig;
use crate::events::Event;

#[derive(Debug, Clone)]
pub struct PunishmentRamp {
    pub cfg: RampConfig,
    acc: f64,
    hp: Option<f64>,
    last_event: f64,
    last_tick: f64,
    pub events: u64,
    pub resets: u64,
}

impl PunishmentRamp {
    pub fn new(cfg: RampConfig, now: f64) -> Self {
        Self {
            cfg,
            acc: 0.0,
            hp: None,
            last_event: 0.0,
            last_tick: now,
            events: 0,
            resets: 0,
        }
    }
    pub fn reset(&mut self, now: f64) {
        self.acc = 0.0;
        self.last_event = now;
    }
    pub fn note_hp(&mut self, hp: Option<f64>) {
        self.hp = hp.filter(|x| x.is_finite()).map(|x| x.clamp(0.0, 1.0));
    }
    pub fn on_event(&mut self, event: &Event, now: f64) {
        self.last_tick = now;
        if !self.cfg.enabled {
            return;
        }
        if matches!(event, Event::Death { .. } | Event::Revive { .. }) && self.cfg.reset_on_death {
            if self.acc > 0.0 {
                self.resets += 1;
            }
            self.acc = 0.0;
            return;
        }
        if !self
            .cfg
            .apply_to
            .iter()
            .any(|name| name == rule_name(event))
        {
            return;
        }
        self.acc = (self.acc + self.cfg.per_event.max(0.0)).min(self.cfg.ceiling_pct.max(0.0));
        self.last_event = now;
        self.events += 1;
    }
    pub fn tick(&mut self, now: f64) {
        let dt = (now - self.last_tick).max(0.0);
        self.last_tick = now;
        if !self.cfg.enabled
            || dt <= 0.0
            || self.last_event == 0.0
            || now - self.last_event < self.cfg.decay_after_s.max(0.0)
        {
            return;
        }
        self.acc = (self.acc - self.cfg.decay_per_s.max(0.0) * dt).max(0.0);
    }
    pub fn accumulated(&self) -> f64 {
        self.acc
    }
    pub fn hp_component(&self) -> f64 {
        self.hp
            .map(|hp| self.cfg.hp_missing_pct.max(0.0) * (1.0 - hp))
            .unwrap_or(0.0)
    }
    pub fn bonus(&self) -> f64 {
        if !self.cfg.enabled {
            0.0
        } else {
            (self.acc + self.hp_component()).min(self.cfg.ceiling_pct.max(0.0))
        }
    }
    pub fn describe(&self) -> String {
        if !self.cfg.enabled {
            "关闭".into()
        } else {
            format!(
                "累积 {:.1}% + 血量 {:.1}% = +{:.1}%（上限 {:.0}%）",
                self.acc,
                self.hp_component(),
                self.bonus(),
                self.cfg.ceiling_pct
            )
        }
    }
}

pub fn rule_name(event: &Event) -> &'static str {
    event.kind()
}
pub type RampState = PunishmentRamp;

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn accumulation_decay_reset() {
        let cfg = RampConfig {
            enabled: true,
            ..RampConfig::default()
        };
        let mut ramp = PunishmentRamp::new(cfg, 100.0);
        for i in 0..3 {
            ramp.on_event(
                &Event::Damage {
                    t: 0.0,
                    severity: 1.0,
                    hp_before: 1.0,
                    hp_after: 0.9,
                },
                100.0 + i as f64,
            );
        }
        assert_eq!(ramp.accumulated(), 6.0);
        ramp.tick(105.0);
        assert_eq!(ramp.accumulated(), 6.0);
        ramp.tick(106.0);
        assert_eq!(ramp.accumulated(), 4.5);
        ramp.on_event(&Event::Death { t: 0.0 }, 107.0);
        assert_eq!(ramp.accumulated(), 0.0);
        assert_eq!(ramp.resets, 1);
    }
    #[test]
    fn hp_and_cap() {
        let cfg = RampConfig {
            enabled: true,
            ..RampConfig::default()
        };
        let mut ramp = PunishmentRamp::new(cfg, 0.0);
        ramp.note_hp(Some(0.25));
        assert_eq!(ramp.hp_component(), 15.0);
        assert_eq!(ramp.bonus(), 15.0);
    }
}
