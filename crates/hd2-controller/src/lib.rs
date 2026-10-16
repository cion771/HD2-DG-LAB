//! Deterministic controller joining event sources, core rules, safety, and an output device.
//!
//! Adapter methods are synchronous and must be non-blocking. The threaded Controller
//! polls them every TICK_INTERVAL; cancellation waits only for the caller's bound.

use hd2_core::config::AppConfig;
use hd2_core::detectors::EventTrackers;
use hd2_core::events::Event;
use hd2_core::ramp::PunishmentRamp;
use hd2_core::rules::{Action, RuleEngine};
use hd2_core::safety::SafetyGate;
use std::error::Error as StdError;
use std::fmt;
use std::sync::{Arc, Mutex, mpsc};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use thiserror::Error;

pub const TICK_INTERVAL: Duration = Duration::from_millis(20);
pub type AdapterError = Box<dyn StdError + Send + Sync + 'static>;
pub type AdapterResult<T = ()> = Result<T, AdapterError>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Channel {
    A,
    B,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DeviceLimits {
    pub a: i64,
    pub b: i64,
}
impl Default for DeviceLimits {
    fn default() -> Self {
        Self { a: 200, b: 200 }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct StateFrame {
    pub hp: Option<f64>,
    pub injury_scores: Vec<f64>,
    pub bleeding_score: f64,
    pub death_score: f64,
    pub death_threshold: Option<f64>,
    pub injury_names: Option<Vec<String>>,
}
impl Default for StateFrame {
    fn default() -> Self {
        Self {
            hp: None,
            injury_scores: vec![],
            bleeding_score: 0.0,
            death_score: 0.0,
            death_threshold: None,
            injury_names: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum SourceItem {
    Event(Event),
    State(StateFrame),
}

/// Ordered, pull-based adapter. Lifecycle and poll methods must return promptly.
pub trait Source: Send {
    fn name(&self) -> &str;
    fn critical(&self) -> bool {
        false
    }
    fn start(&mut self) -> AdapterResult<()>;
    fn poll(&mut self, now: f64) -> AdapterResult<Vec<SourceItem>>;
    fn alive(&self, now: f64) -> bool;
    fn stop(&mut self) -> AdapterResult<()>;
}

/// Synchronous output adapter. set_strength(0, 0) should be immediate.
pub trait Device: Send {
    fn start(&mut self) -> AdapterResult<()>;
    fn connected(&self) -> bool;
    fn limits(&self) -> DeviceLimits;
    fn set_strength(&mut self, a: i64, b: i64) -> AdapterResult<()>;
    fn clear(&mut self, channel: Channel) -> AdapterResult<()>;
    fn send_wave(&mut self, channel: Channel, units: &[String]) -> AdapterResult<()>;
    fn stop(&mut self) -> AdapterResult<()>;
}

#[derive(Debug, Error)]
pub enum ControllerError {
    #[error("adapter {adapter} failed: {message}")]
    Adapter { adapter: String, message: String },
    #[error("cannot arm: {0}")]
    CannotArm(String),
    #[error("controller worker has stopped")]
    WorkerStopped,
    #[error("controller cancellation exceeded {0:?}")]
    StopTimeout(Duration),
}
fn adapter_error(adapter: impl Into<String>, error: impl fmt::Display) -> ControllerError {
    ControllerError::Adapter {
        adapter: adapter.into(),
        message: error.to_string(),
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct EngineStatus {
    pub running: bool,
    pub armed: bool,
    pub trip_reason: String,
    pub device_connected: bool,
    pub output_a: i64,
    pub output_b: i64,
    pub output_pct: f64,
    pub active_effects: usize,
    pub processed_events: u64,
}
impl Default for EngineStatus {
    fn default() -> Self {
        Self {
            running: false,
            armed: false,
            trip_reason: "startup disarmed".into(),
            device_connected: false,
            output_a: 0,
            output_b: 0,
            output_pct: 0.0,
            active_effects: 0,
            processed_events: 0,
        }
    }
}

#[derive(Debug, Clone)]
struct ActiveEffect {
    action: Action,
    ends: f64,
    next_repeat: Option<f64>,
}

/// Deterministic state machine; embedders may drive it with explicit monotonic seconds.
pub struct Engine {
    cfg: AppConfig,
    sources: Vec<Box<dyn Source>>,
    source_started: Vec<bool>,
    device: Box<dyn Device>,
    rules: RuleEngine,
    ramp: PunishmentRamp,
    safety: SafetyGate,
    trackers: EventTrackers,
    effects: Vec<ActiveEffect>,
    current_a: f64,
    current_b: f64,
    last_tick: f64,
    started: bool,
    device_was_connected: bool,
    status: EngineStatus,
}

impl Engine {
    pub fn new(cfg: AppConfig, sources: Vec<Box<dyn Source>>, device: Box<dyn Device>) -> Self {
        let mut safety = SafetyGate::new(cfg.safety.clone());
        safety.disarm("startup disarmed");
        let source_started = vec![false; sources.len()];
        Self {
            rules: RuleEngine::default(),
            ramp: PunishmentRamp::new(cfg.ramp.clone(), 0.0),
            trackers: EventTrackers::new(cfg.clone()),
            cfg,
            sources,
            source_started,
            device,
            safety,
            effects: vec![],
            current_a: 0.0,
            current_b: 0.0,
            last_tick: 0.0,
            started: false,
            device_was_connected: false,
            status: EngineStatus::default(),
        }
    }

    pub fn status(&self) -> &EngineStatus {
        &self.status
    }
    pub fn is_armed(&self) -> bool {
        self.safety.armed()
    }

    pub fn start(&mut self, now: f64) -> Result<(), ControllerError> {
        if self.started {
            return Ok(());
        }
        self.device
            .start()
            .map_err(|e| adapter_error("device start", e))?;
        let mut started_indices = Vec::new();
        for index in 0..self.sources.len() {
            let name = self.sources[index].name().to_owned();
            match self.sources[index].start() {
                Ok(()) => {
                    self.source_started[index] = true;
                    started_indices.push(index);
                }
                Err(error) if self.sources[index].critical() => {
                    let message = error.to_string();
                    self.trip(format!("critical source {name} failed to start: {message}"));
                    for index in started_indices.into_iter().rev() {
                        let _ = self.sources[index].stop();
                        self.source_started[index] = false;
                    }
                    let _ = self.device.stop();
                    return Err(adapter_error(name, message));
                }
                Err(_) => {}
            }
        }
        self.last_tick = now;
        self.started = true;
        self.device_was_connected = self.device.connected();
        self.status.running = true;
        self.refresh_status();
        Ok(())
    }

    pub fn arm(&mut self, now: f64) -> Result<(), ControllerError> {
        if !self.started {
            return Err(ControllerError::CannotArm("engine is not running".into()));
        }
        if !self.device.connected() {
            return Err(ControllerError::CannotArm("device is disconnected".into()));
        }
        for source in &self.sources {
            if source.critical() && !source.alive(now) {
                return Err(ControllerError::CannotArm(format!(
                    "critical source {} is not alive",
                    source.name()
                )));
            }
        }
        self.effects.clear();
        self.rules.reset_cooldowns();
        self.ramp.reset(now);
        self.safety.arm();
        self.last_tick = now;
        self.refresh_status();
        Ok(())
    }

    pub fn trip(&mut self, reason: impl Into<String>) {
        self.effects.clear();
        self.safety.trip(reason.into());
        self.current_a = 0.0;
        self.current_b = 0.0;
        self.force_zero();
        self.refresh_status();
    }

    pub fn inject(&mut self, event: Event, now: f64) -> Result<(), ControllerError> {
        let result = self.handle_event(event, now);
        self.refresh_status();
        result
    }

    pub fn tick(&mut self, now: f64) -> Result<(), ControllerError> {
        if !self.started {
            return Ok(());
        }
        let connected = self.device.connected();
        if self.device_was_connected && !connected {
            self.trip("device disconnected");
        }
        self.device_was_connected = connected;

        for index in 0..self.sources.len() {
            if !self.source_started[index] {
                continue;
            }
            if self.sources[index].critical() && !self.sources[index].alive(now) {
                self.trip(format!(
                    "critical source {} failed",
                    self.sources[index].name()
                ));
                break;
            }
            let name = self.sources[index].name().to_owned();
            let items = match self.sources[index].poll(now) {
                Ok(items) => items,
                Err(error) => {
                    let message = error.to_string();
                    if self.sources[index].critical() {
                        self.trip(format!("critical source {name} poll failed: {message}"));
                        return Err(adapter_error(name, message));
                    }
                    continue;
                }
            };
            for item in items {
                match item {
                    SourceItem::Event(event) => self.handle_event(event, now)?,
                    SourceItem::State(frame) => {
                        self.ramp.note_hp(frame.hp);
                        let events = self.trackers.process(
                            frame.hp,
                            &frame.injury_scores,
                            frame.bleeding_score,
                            frame.death_score,
                            now,
                            frame.death_threshold,
                            frame.injury_names.as_deref(),
                        );
                        for event in events {
                            self.handle_event(event, now)?;
                        }
                    }
                }
            }
        }
        self.advance(now)
    }

    fn handle_event(&mut self, event: Event, now: f64) -> Result<(), ControllerError> {
        self.status.processed_events += 1;
        if let Event::DeviceState {
            connected: false, ..
        } = &event
        {
            self.trip("device disconnected");
            return Ok(());
        }
        self.ramp.on_event(&event, now);
        let actions = self.rules.actions(&self.cfg, &event, now);
        if self.safety.armed() {
            for mut action in actions {
                action.pct = (action.pct + self.ramp.bonus()).min(100.0);
                self.activate(action, now)?;
            }
        }
        self.refresh_status();
        Ok(())
    }

    fn activate(&mut self, action: Action, now: f64) -> Result<(), ControllerError> {
        self.effects.retain(|effect| {
            !(effect.action.name == action.name && effect.action.channel == action.channel)
        });
        let repeat = (action.repeat_ms > 0.0).then(|| now + action.repeat_ms / 1000.0);
        let ends = now + action.duration_ms / 1000.0;
        self.send_intent(&action)?;
        self.effects.push(ActiveEffect {
            action,
            ends,
            next_repeat: repeat,
        });
        Ok(())
    }

    fn send_intent(&mut self, action: &Action) -> Result<(), ControllerError> {
        for channel in channels(&action.channel) {
            self.device
                .clear(channel)
                .map_err(|e| adapter_error("device clear", e))?;
            self.device
                .send_wave(channel, &action.units)
                .map_err(|e| adapter_error("device wave", e))?;
        }
        Ok(())
    }

    fn advance(&mut self, now: f64) -> Result<(), ControllerError> {
        self.ramp.tick(now);
        let mut repeats = vec![];
        for effect in &mut self.effects {
            if effect
                .next_repeat
                .is_some_and(|next| now >= next && now < effect.ends)
            {
                repeats.push(effect.action.clone());
                effect.next_repeat = Some(now + effect.action.repeat_ms / 1000.0);
            }
        }
        for action in repeats {
            self.send_intent(&action)?;
        }
        self.effects.retain(|effect| now < effect.ends);

        let (mut target_a, mut target_b): (f64, f64) = (0.0, 0.0);
        if self.safety.armed() {
            for effect in &self.effects {
                if matches!(effect.action.channel.as_str(), "A" | "both") {
                    target_a = target_a.max(effect.action.pct);
                }
                if matches!(effect.action.channel.as_str(), "B" | "both") {
                    target_b = target_b.max(effect.action.pct);
                }
            }
        }
        let dt = (now - self.last_tick).max(0.0);
        self.last_tick = now;
        self.current_a = approach(
            self.current_a,
            target_a,
            dt,
            self.cfg.safety.attack_ms,
            self.cfg.safety.release_ms,
        );
        self.current_b = approach(
            self.current_b,
            target_b,
            dt,
            self.cfg.safety.attack_ms,
            self.cfg.safety.release_ms,
        );
        let limits = self.device.limits();
        let a = self.safety.strength_for(
            self.current_a,
            limits.a,
            self.cfg.safety.channel_a_multiplier,
        );
        let b = self.safety.strength_for(
            self.current_b,
            limits.b,
            self.cfg.safety.channel_b_multiplier,
        );
        if a != self.status.output_a || b != self.status.output_b {
            if let Err(error) = self.device.set_strength(a, b) {
                let message = error.to_string();
                self.trip(format!("device output failed: {message}"));
                return Err(adapter_error("device output", message));
            }
            self.status.output_a = a;
            self.status.output_b = b;
        }
        if !self
            .safety
            .note_output(dt, self.current_a.max(self.current_b))
        {
            self.effects.clear();
            self.current_a = 0.0;
            self.current_b = 0.0;
            self.force_zero();
        }
        self.refresh_status();
        Ok(())
    }

    /// Stops in deterministic order and attempts zero and queue clears even after an error.
    pub fn stop(&mut self) -> Result<(), ControllerError> {
        self.effects.clear();
        self.current_a = 0.0;
        self.current_b = 0.0;
        let mut first_error = None;
        if let Err(e) = self.device.set_strength(0, 0) {
            first_error = Some(adapter_error("device zero", e));
        }
        for channel in [Channel::A, Channel::B] {
            if let Err(e) = self.device.clear(channel) {
                if first_error.is_none() {
                    first_error = Some(adapter_error("device clear", e));
                }
            }
        }
        for index in (0..self.sources.len()).rev() {
            if !self.source_started[index] {
                continue;
            }
            let source = &mut self.sources[index];
            let name = source.name().to_owned();
            if let Err(e) = source.stop() {
                if first_error.is_none() {
                    first_error = Some(adapter_error(name, e));
                }
            }
            self.source_started[index] = false;
        }
        if let Err(e) = self.device.stop() {
            if first_error.is_none() {
                first_error = Some(adapter_error("device stop", e));
            }
        }
        self.started = false;
        self.status.running = false;
        self.status.output_a = 0;
        self.status.output_b = 0;
        self.status.output_pct = 0.0;
        self.refresh_status();
        first_error.map_or(Ok(()), Err)
    }

    fn force_zero(&mut self) {
        let _ = self.device.set_strength(0, 0);
        for channel in [Channel::A, Channel::B] {
            let _ = self.device.clear(channel);
        }
        self.status.output_a = 0;
        self.status.output_b = 0;
        self.status.output_pct = 0.0;
    }
    fn refresh_status(&mut self) {
        self.status.armed = self.safety.armed();
        self.status.trip_reason = self.safety.mute_reason().to_owned();
        self.status.device_connected = self.device.connected();
        self.status.output_pct = self.current_a.max(self.current_b);
        self.status.active_effects = self.effects.len();
    }
}

impl Drop for Engine {
    fn drop(&mut self) {
        if self.started {
            let _ = self.stop();
        }
    }
}

fn channels(channel: &str) -> Vec<Channel> {
    match channel {
        "A" => vec![Channel::A],
        "B" => vec![Channel::B],
        "both" => vec![Channel::A, Channel::B],
        _ => vec![],
    }
}
fn approach(current: f64, target: f64, dt: f64, attack_ms: f64, release_ms: f64) -> f64 {
    if (target - current).abs() < 0.01 {
        return target;
    }
    let ms = if target > current {
        attack_ms
    } else {
        release_ms
    };
    let delta = 100.0 / (ms / 1000.0).max(0.05) * dt;
    if target > current {
        (current + delta).min(target)
    } else {
        (current - delta).max(target)
    }
}

#[derive(Debug)]
enum Command {
    Arm(mpsc::Sender<Result<(), ControllerError>>),
    Trip(String),
    Inject(Event),
    Stop(mpsc::Sender<Result<(), ControllerError>>),
}

/// 20ms worker wrapper with command ownership and bounded cancellation.
pub struct Controller {
    tx: mpsc::Sender<Command>,
    status: Arc<Mutex<EngineStatus>>,
    worker: Option<JoinHandle<()>>,
    origin: Instant,
}
impl Controller {
    pub fn spawn(mut engine: Engine) -> Result<Self, ControllerError> {
        let origin = Instant::now();
        engine.start(0.0)?;
        let status = Arc::new(Mutex::new(engine.status().clone()));
        let worker_status = Arc::clone(&status);
        let (tx, rx) = mpsc::channel();
        let worker = thread::Builder::new()
            .name("hd2-controller".into())
            .spawn(move || {
                let started = Instant::now();
                let mut next_tick = started;
                loop {
                    let wait = next_tick.saturating_duration_since(Instant::now());
                    match rx.recv_timeout(wait) {
                        Ok(Command::Arm(reply)) => {
                            let _ = reply.send(engine.arm(started.elapsed().as_secs_f64()));
                        }
                        Ok(Command::Trip(reason)) => engine.trip(reason),
                        Ok(Command::Inject(event)) => {
                            let _ = engine.inject(event, started.elapsed().as_secs_f64());
                        }
                        Ok(Command::Stop(reply)) => {
                            let result = engine.stop();
                            let _ = reply.send(result);
                            *lock(&worker_status) = engine.status().clone();
                            break;
                        }
                        Err(mpsc::RecvTimeoutError::Disconnected) => {
                            let _ = engine.stop();
                            break;
                        }
                        Err(mpsc::RecvTimeoutError::Timeout) => {
                            let now = started.elapsed().as_secs_f64();
                            if let Err(error) = engine.tick(now) {
                                engine.trip(error.to_string());
                            }
                            next_tick += TICK_INTERVAL;
                            if next_tick < Instant::now() {
                                next_tick = Instant::now() + TICK_INTERVAL;
                            }
                        }
                    }
                    *lock(&worker_status) = engine.status().clone();
                }
            })
            .map_err(|e| adapter_error("controller thread", e))?;
        Ok(Self {
            tx,
            status,
            worker: Some(worker),
            origin,
        })
    }
    pub fn status(&self) -> EngineStatus {
        lock(&self.status).clone()
    }
    pub fn arm(&self) -> Result<(), ControllerError> {
        let (tx, rx) = mpsc::channel();
        self.tx
            .send(Command::Arm(tx))
            .map_err(|_| ControllerError::WorkerStopped)?;
        rx.recv().map_err(|_| ControllerError::WorkerStopped)?
    }
    pub fn trip(&self, reason: impl Into<String>) -> Result<(), ControllerError> {
        self.tx
            .send(Command::Trip(reason.into()))
            .map_err(|_| ControllerError::WorkerStopped)
    }
    pub fn inject(&self, event: Event) -> Result<(), ControllerError> {
        self.tx
            .send(Command::Inject(event))
            .map_err(|_| ControllerError::WorkerStopped)
    }
    pub fn elapsed(&self) -> Duration {
        self.origin.elapsed()
    }
    pub fn stop(&mut self, timeout: Duration) -> Result<(), ControllerError> {
        let Some(worker) = self.worker.take() else {
            return Ok(());
        };
        let (tx, rx) = mpsc::channel();
        if self.tx.send(Command::Stop(tx)).is_err() {
            let _ = worker.join();
            return Err(ControllerError::WorkerStopped);
        }
        match rx.recv_timeout(timeout) {
            Ok(result) => {
                let _ = worker.join();
                result
            }
            Err(_) => {
                drop(worker);
                Err(ControllerError::StopTimeout(timeout))
            }
        }
    }
}
impl Drop for Controller {
    fn drop(&mut self) {
        let _ = self.stop(Duration::from_millis(250));
    }
}
fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|p| p.into_inner())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn approach_has_separate_attack_and_release() {
        assert_eq!(approach(0.0, 100.0, 0.02, 100.0, 1000.0), 20.0);
        assert_eq!(approach(20.0, 0.0, 0.02, 100.0, 1000.0), 18.0);
    }
    #[test]
    fn channel_expansion_is_explicit() {
        assert_eq!(channels("both"), vec![Channel::A, Channel::B]);
        assert!(channels("alternate").is_empty());
    }
}
