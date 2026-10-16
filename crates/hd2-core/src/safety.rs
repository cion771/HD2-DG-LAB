use crate::config::SafetyConfig;
use crate::rounding::round_ties_even;

#[derive(Debug, Clone)]
pub struct SafetyGate {
    pub cfg: SafetyConfig,
    armed: bool,
    mute_reason: String,
    session_seconds: f64,
    generation: u64,
    last_tick: Option<f64>,
}

impl SafetyGate {
    pub fn new(cfg: SafetyConfig) -> Self {
        Self {
            cfg,
            armed: true,
            mute_reason: String::new(),
            session_seconds: 0.0,
            generation: 0,
            last_tick: None,
        }
    }

    pub fn armed(&self) -> bool {
        self.armed
    }

    pub fn mute_reason(&self) -> &str {
        &self.mute_reason
    }

    pub fn session_seconds(&self) -> f64 {
        self.session_seconds
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }

    pub fn arm(&mut self) {
        self.armed = true;
        self.mute_reason.clear();
        self.last_tick = None;
    }

    pub fn disarm(&mut self, reason: impl Into<String>) {
        self.armed = false;
        self.mute_reason = reason.into();
    }

    pub fn trip(&mut self, reason: impl Into<String>) {
        self.disarm(reason);
        self.generation = self.generation.wrapping_add(1);
    }

    pub fn set_config(&mut self, cfg: SafetyConfig) {
        self.cfg = cfg;
    }

    pub fn effective_pct(&self, pct: f64, channel_multiplier: f64) -> f64 {
        if !self.armed {
            return 0.0;
        }
        let pct = pct.max(0.0) * self.cfg.master_multiplier.max(0.0) * channel_multiplier.max(0.0);
        pct.min(self.cfg.max_pct.max(0.0))
    }

    pub fn strength_for(&self, pct: f64, limit: i64, channel_multiplier: f64) -> i64 {
        let value =
            round_ties_even(limit as f64 * self.effective_pct(pct, channel_multiplier) / 100.0);
        value.max(0).min(limit).min(self.cfg.max_absolute)
    }

    pub fn strength(&self, pct: f64, channel: char, limit: u16) -> u16 {
        let multiplier = match channel.to_ascii_uppercase() {
            'A' => self.cfg.channel_a_multiplier,
            'B' => self.cfg.channel_b_multiplier,
            _ => 0.0,
        };
        self.strength_for(pct, limit as i64, multiplier) as u16
    }

    /// Account for output duration and return whether output remains armed.
    pub fn note_output(&mut self, seconds: f64, pct: f64) -> bool {
        if seconds > 0.0 && pct > 0.0 {
            self.session_seconds += seconds;
            if self.cfg.max_session_seconds > 0.0
                && self.session_seconds >= self.cfg.max_session_seconds
            {
                self.trip(format!(
                    "会话预算用尽（{:.0}s）",
                    self.cfg.max_session_seconds
                ));
                return false;
            }
        }
        self.armed
    }

    pub fn reset_session(&mut self) {
        self.session_seconds = 0.0;
    }

    /// Convenience helper for callers that account wall-clock output intervals.
    pub fn tick_budget(&mut self, now: f64, output_nonzero: bool) -> bool {
        let elapsed = self
            .last_tick
            .map(|last| (now - last).max(0.0))
            .unwrap_or(0.0);
        self.last_tick = Some(now);
        self.note_output(elapsed, if output_nonzero { 1.0 } else { 0.0 })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn starts_armed_and_rounds_even() {
        let gate = SafetyGate::new(SafetyConfig {
            max_pct: 100.0,
            max_absolute: 200,
            ..Default::default()
        });
        assert!(gate.armed());
        assert_eq!(gate.strength_for(25.0, 10, 1.0), 2);
        assert_eq!(gate.strength_for(35.0, 10, 1.0), 4);
    }

    #[test]
    fn caps_and_budget() {
        let mut gate = SafetyGate::new(SafetyConfig {
            max_session_seconds: 2.0,
            ..Default::default()
        });
        assert_eq!(gate.strength(100.0, 'A', 200), 40);
        assert!(gate.note_output(1.0, 1.0));
        assert!(!gate.note_output(1.0, 1.0));
        assert!(!gate.armed());
        assert_eq!(gate.mute_reason(), "会话预算用尽（2s）");
    }
}
