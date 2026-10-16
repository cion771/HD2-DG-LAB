use crate::config::{AppConfig, DetectConfig};
use crate::events::Event;
#[derive(Debug, Clone)]
pub struct HealthTracker {
    pub cfg: DetectConfig,
    prev: Option<f64>,
    pending: f64,
    pending_started: Option<f64>,
    pending_before: Option<f64>,
    low: bool,
    ignore_until: f64,
}
impl HealthTracker {
    pub fn new(cfg: DetectConfig) -> Self {
        Self {
            cfg,
            prev: None,
            pending: 0.,
            pending_started: None,
            pending_before: None,
            low: false,
            ignore_until: 0.,
        }
    }
    fn clear_pending(&mut self) {
        self.pending = 0.;
        self.pending_started = None;
        self.pending_before = None
    }
    fn flush_due(&mut self, now: f64, force: bool) -> Vec<Event> {
        let (Some(start), Some(before)) = (self.pending_started, self.pending_before) else {
            return vec![];
        };
        let window = self.cfg.damage_merge_window_ms.max(0.) / 1000.;
        if !force && now < start + window {
            return vec![];
        }
        let amount = self.pending;
        self.clear_pending();
        let severity = amount * 100.;
        if severity < self.cfg.damage_min_pct.max(0.) {
            vec![]
        } else {
            vec![Event::Damage {
                t: now,
                severity,
                hp_before: before,
                hp_after: (before - amount).max(0.),
            }]
        }
    }
    pub fn reset(&mut self) {
        self.prev = None;
        self.clear_pending();
        self.low = false
    }
    pub fn ignore_damage_for(&mut self, seconds: f64, now: f64) {
        self.ignore_until = now + seconds.max(0.);
        self.clear_pending()
    }
    pub fn update(&mut self, hp: Option<f64>, now: f64) -> Vec<Event> {
        let mut out = self.flush_due(now, false);
        let Some(hp) = hp else { return out };
        if self.prev.is_none() {
            self.prev = Some(hp);
            return out;
        }
        let prev = self.prev.unwrap();
        let delta = prev - hp;
        if delta > 0. && now >= self.ignore_until {
            if self.pending_started.is_none() {
                self.pending_started = Some(now);
                self.pending_before = Some(prev)
            }
            self.pending += delta;
            if self.cfg.damage_merge_window_ms.max(0.) == 0. {
                out.extend(self.flush_due(now, true))
            }
        }
        let pct = hp * 100.;
        if !self.low && hp > 0.02 && pct <= self.cfg.low_health_pct {
            self.low = true;
            out.push(Event::LowHealth { t: now, ratio: hp })
        } else if self.low && pct >= self.cfg.recover_pct {
            self.low = false;
            out.push(Event::Recovered { t: now, ratio: hp })
        }
        self.prev = Some(hp);
        out
    }
}
#[derive(Debug, Clone)]
pub struct InjuryTracker {
    pub cfg: DetectConfig,
    slots: usize,
    pub names: Vec<String>,
    active: Vec<bool>,
    last_hit: Vec<f64>,
}
impl InjuryTracker {
    pub fn new(cfg: DetectConfig, slots: usize, names: Option<Vec<String>>) -> Self {
        let slots = slots.max(1);
        Self {
            cfg,
            slots,
            names: names.unwrap_or_default(),
            active: vec![false; slots],
            last_hit: vec![0.; slots],
        }
    }
    pub fn clear(&mut self) {
        self.active.fill(false)
    }
    pub fn update(&mut self, scores: &[f64], bleeding: f64, now: f64) -> Vec<Event> {
        let mut out = vec![];
        let is_bleeding = bleeding >= self.cfg.bleeding_min_fraction;
        for (i, score) in scores.iter().take(self.slots).enumerate() {
            let hit = *score >= self.cfg.injury_min_fraction;
            if hit {
                self.last_hit[i] = now;
                if !self.active[i] {
                    self.active[i] = true;
                    out.push(Event::LimbInjury {
                        t: now,
                        slot: i,
                        bleeding: is_bleeding,
                        name: self
                            .names
                            .get(i)
                            .cloned()
                            .unwrap_or_else(|| format!("妲戒綅{i}")),
                    })
                }
            } else if self.active[i] && now - self.last_hit[i] >= self.cfg.injury_clear_s {
                self.active[i] = false
            }
        }
        out
    }
}
#[derive(Debug, Clone)]
pub struct DeathTracker {
    pub cfg: DetectConfig,
    pub dead: bool,
    since: Option<f64>,
}
impl DeathTracker {
    pub fn new(cfg: DetectConfig) -> Self {
        Self {
            cfg,
            dead: false,
            since: None,
        }
    }
    pub fn update(
        &mut self,
        hp: Option<f64>,
        death_score: f64,
        now: f64,
        threshold: f64,
    ) -> Vec<Event> {
        if !self.dead {
            if death_score >= threshold || hp.is_some_and(|x| x <= 0.02) {
                let since = *self.since.get_or_insert(now);
                if now - since >= self.cfg.dead_hold_s {
                    self.dead = true;
                    self.since = None;
                    return vec![Event::Death { t: now }];
                }
            } else {
                self.since = None
            }
        } else {
            let alive = hp.map_or(death_score < threshold * 0.8, |x| x >= 0.15);
            if alive {
                self.dead = false;
                return vec![Event::Revive { t: now }];
            }
        }
        vec![]
    }
}
#[derive(Debug, Clone)]
pub struct EventTrackers {
    pub cfg: AppConfig,
    pub health: HealthTracker,
    pub injury: InjuryTracker,
    pub death: DeathTracker,
    slots: usize,
}
impl EventTrackers {
    pub fn new(cfg: AppConfig) -> Self {
        let slots = cfg.hud.injury_slots.max(1);
        Self {
            health: HealthTracker::new(cfg.detect.clone()),
            injury: InjuryTracker::new(cfg.detect.clone(), slots, None),
            death: DeathTracker::new(cfg.detect.clone()),
            cfg,
            slots,
        }
    }
    pub fn reset(&mut self) {
        self.health = HealthTracker::new(self.cfg.detect.clone());
        self.injury = InjuryTracker::new(self.cfg.detect.clone(), self.slots, None);
        self.death = DeathTracker::new(self.cfg.detect.clone())
    }
    #[allow(clippy::too_many_arguments)]
    pub fn process(
        &mut self,
        hp: Option<f64>,
        scores: &[f64],
        bleeding: f64,
        death_score: f64,
        now: f64,
        threshold: Option<f64>,
        names: Option<&[String]>,
    ) -> Vec<Event> {
        if let Some(n) = names.filter(|x| !x.is_empty()) {
            self.injury.names = n.to_vec()
        }
        let mut out = self.death.update(
            hp,
            death_score,
            now,
            threshold.unwrap_or(self.cfg.detect.death_state_threshold),
        );
        let mut health = self.health.update(hp, now);
        if self.death.dead {
            health.retain(|x| !matches!(x, Event::Damage { .. }))
        }
        out.extend(health);
        if out.iter().any(|x| matches!(x, Event::Revive { .. })) {
            self.health
                .ignore_damage_for(self.cfg.detect.revive_ignore_damage_s, now);
            self.injury.clear()
        }
        out.extend(self.injury.update(scores, bleeding, now));
        out
    }
}
pub type HealthDetector = HealthTracker;
pub type InjuryDetector = InjuryTracker;
pub type DeathDetector = DeathTracker;
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fixed_window() {
        let mut h = HealthTracker::new(DetectConfig::default());
        assert!(h.update(Some(1.), 0.).is_empty());
        h.update(Some(0.98), 0.1);
        h.update(Some(0.94), 0.2);
        let e = h.update(Some(0.94), 0.36);
        assert!(matches!(e[0],Event::Damage{severity,..}if(severity-6.).abs()<1e-9))
    }
    #[test]
    fn injury_python_clear_semantics() {
        let mut i = InjuryTracker::new(DetectConfig::default(), 1, None);
        assert_eq!(i.update(&[1.], 0., 0.).len(), 1);
        assert!(i.update(&[1.], 0., 0.5).is_empty());
        assert!(i.update(&[0.], 0., 1.).is_empty());
        assert_eq!(i.update(&[1.], 0., 2.).len(), 1)
    }
}
