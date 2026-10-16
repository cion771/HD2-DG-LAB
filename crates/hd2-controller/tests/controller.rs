use hd2_controller::{
    AdapterResult, Channel, Device, DeviceLimits, Engine, Source, SourceItem, StateFrame,
};
use hd2_core::config::AppConfig;
use hd2_core::events::Event;
use std::collections::VecDeque;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap()
}

#[derive(Clone, Debug, PartialEq)]
enum Call {
    DeviceStart,
    Strength(i64, i64),
    Clear(Channel),
    Wave(Channel),
    DeviceStop,
    SourceStart(String),
    Poll(String),
    SourceStop(String),
}

struct MockDevice {
    calls: Arc<Mutex<Vec<Call>>>,
    connected: Arc<AtomicBool>,
}
impl Device for MockDevice {
    fn start(&mut self) -> AdapterResult {
        lock(&self.calls).push(Call::DeviceStart);
        Ok(())
    }
    fn connected(&self) -> bool {
        self.connected.load(Ordering::SeqCst)
    }
    fn limits(&self) -> DeviceLimits {
        DeviceLimits { a: 200, b: 200 }
    }
    fn set_strength(&mut self, a: i64, b: i64) -> AdapterResult {
        lock(&self.calls).push(Call::Strength(a, b));
        Ok(())
    }
    fn clear(&mut self, ch: Channel) -> AdapterResult {
        lock(&self.calls).push(Call::Clear(ch));
        Ok(())
    }
    fn send_wave(&mut self, ch: Channel, _: &[String]) -> AdapterResult {
        lock(&self.calls).push(Call::Wave(ch));
        Ok(())
    }
    fn stop(&mut self) -> AdapterResult {
        lock(&self.calls).push(Call::DeviceStop);
        Ok(())
    }
}

struct MockSource {
    name: String,
    critical: bool,
    alive: Arc<AtomicBool>,
    calls: Arc<Mutex<Vec<Call>>>,
    batches: VecDeque<Vec<SourceItem>>,
}
impl Source for MockSource {
    fn name(&self) -> &str {
        &self.name
    }
    fn critical(&self) -> bool {
        self.critical
    }
    fn start(&mut self) -> AdapterResult {
        lock(&self.calls).push(Call::SourceStart(self.name.clone()));
        Ok(())
    }
    fn poll(&mut self, _: f64) -> AdapterResult<Vec<SourceItem>> {
        lock(&self.calls).push(Call::Poll(self.name.clone()));
        Ok(self.batches.pop_front().unwrap_or_default())
    }
    fn alive(&self, _: f64) -> bool {
        self.alive.load(Ordering::SeqCst)
    }
    fn stop(&mut self) -> AdapterResult {
        lock(&self.calls).push(Call::SourceStop(self.name.clone()));
        Ok(())
    }
}

fn damage() -> Event {
    Event::Damage {
        t: 0.0,
        severity: 10.0,
        hp_before: 1.0,
        hp_after: 0.9,
    }
}
fn fixture(sources: Vec<Box<dyn Source>>) -> (Engine, Arc<Mutex<Vec<Call>>>, Arc<AtomicBool>) {
    let calls = Arc::new(Mutex::new(vec![]));
    let connected = Arc::new(AtomicBool::new(true));
    let mut cfg = AppConfig::default();
    cfg.safety.attack_ms = 100.0;
    cfg.safety.release_ms = 1000.0;
    cfg.safety.max_pct = 100.0;
    cfg.safety.max_absolute = 200;
    cfg.rules.get_mut("damage").unwrap().cooldown_ms = 0.0;
    cfg.rules.get_mut("limb_injury").unwrap().cooldown_ms = 0.0;
    let device = MockDevice {
        calls: calls.clone(),
        connected: connected.clone(),
    };
    (
        Engine::new(cfg, sources, Box::new(device)),
        calls,
        connected,
    )
}

#[test]
fn starts_disarmed_and_ordered_sources_feed_rules() {
    let calls = Arc::new(Mutex::new(vec![]));
    let alive = Arc::new(AtomicBool::new(true));
    let s1 = MockSource {
        name: "one".into(),
        critical: false,
        alive: alive.clone(),
        calls: calls.clone(),
        batches: VecDeque::from([vec![SourceItem::Event(damage())]]),
    };
    let s2 = MockSource {
        name: "two".into(),
        critical: false,
        alive,
        calls: calls.clone(),
        batches: VecDeque::new(),
    };
    let (mut engine, device_calls, _) = fixture(vec![Box::new(s1), Box::new(s2)]);
    engine.start(0.0).unwrap();
    assert!(!engine.status().armed);
    engine.tick(0.02).unwrap();
    assert_eq!(engine.status().active_effects, 0);
    engine.arm(0.03).unwrap();
    engine.inject(damage(), 0.04).unwrap();
    engine.tick(0.14).unwrap();
    assert!(engine.status().output_a > 0);
    let polls: Vec<_> = lock(&calls)
        .iter()
        .filter_map(|c| {
            if let Call::Poll(n) = c {
                Some(n.clone())
            } else {
                None
            }
        })
        .collect();
    assert_eq!(&polls[..2], ["one", "two"]);
    assert!(
        lock(&device_calls)
            .iter()
            .any(|c| matches!(c, Call::Wave(Channel::A)))
    );
}

#[test]
fn replacement_max_composition_slew_and_repeat_intent() {
    let (mut engine, calls, _) = fixture(vec![]);
    engine.start(0.0).unwrap();
    engine.arm(0.0).unwrap();
    engine.inject(damage(), 0.01).unwrap();
    engine.inject(damage(), 0.02).unwrap();
    assert_eq!(
        engine.status().active_effects,
        1,
        "same name and channel replaces"
    );
    engine
        .inject(
            Event::LimbInjury {
                t: 0.0,
                slot: 0,
                bleeding: false,
                name: String::new(),
            },
            0.03,
        )
        .unwrap();
    engine.tick(0.08).unwrap();
    assert_eq!(engine.status().output_a, 60); // 30 pct limb effect wins by per-channel max
    assert_eq!(engine.status().output_b, 60);
    engine.tick(1.22).unwrap(); // limb effect remains active through 1.23
    engine.tick(1.24).unwrap(); // then release is gradual over a short tick
    assert!(engine.status().output_a > 0);

    // Default death action has repeat_ms=1500 and duration 4000.
    engine.inject(Event::Death { t: 0.0 }, 2.0).unwrap();
    let before = lock(&calls)
        .iter()
        .filter(|c| matches!(c, Call::Wave(_)))
        .count();
    engine.tick(3.51).unwrap();
    let after = lock(&calls)
        .iter()
        .filter(|c| matches!(c, Call::Wave(_)))
        .count();
    assert_eq!(after - before, 2, "repeat clears/resends both channels");
}

#[test]
fn state_frames_run_detectors_and_ramp_rules() {
    let (mut engine, _calls, _) = fixture(vec![]);
    engine.start(0.0).unwrap();
    engine.arm(0.0).unwrap();
    let frame = |hp| {
        SourceItem::State(StateFrame {
            hp: Some(hp),
            ..Default::default()
        })
    };
    // Drive detector directly through a mock source-equivalent sequence by recreating with batches.
    engine.stop().unwrap();
    let calls = Arc::new(Mutex::new(vec![]));
    let alive = Arc::new(AtomicBool::new(true));
    let src = MockSource {
        name: "state".into(),
        critical: true,
        alive,
        calls,
        batches: VecDeque::from([vec![frame(1.0)], vec![frame(0.8)], vec![frame(0.8)]]),
    };
    let (mut engine, _, _) = fixture(vec![Box::new(src)]);
    engine.start(0.0).unwrap();
    engine.arm(0.0).unwrap();
    engine.tick(0.01).unwrap();
    engine.tick(0.02).unwrap();
    engine.tick(0.30).unwrap();
    assert!(engine.status().processed_events >= 1);
    assert_eq!(engine.status().active_effects, 1);
}

#[test]
fn disconnect_and_critical_loss_trip_and_stop_is_zero() {
    let calls = Arc::new(Mutex::new(vec![]));
    let alive = Arc::new(AtomicBool::new(true));
    let src = MockSource {
        name: "critical".into(),
        critical: true,
        alive: alive.clone(),
        calls: calls.clone(),
        batches: VecDeque::new(),
    };
    let (mut engine, device_calls, connected) = fixture(vec![Box::new(src)]);
    engine.start(0.0).unwrap();
    engine.arm(0.0).unwrap();
    engine.inject(damage(), 0.01).unwrap();
    engine.tick(0.11).unwrap();
    connected.store(false, Ordering::SeqCst);
    engine.tick(0.12).unwrap();
    assert!(!engine.status().armed);
    assert_eq!((engine.status().output_a, engine.status().output_b), (0, 0));
    connected.store(true, Ordering::SeqCst);
    engine.arm(0.2).unwrap();
    alive.store(false, Ordering::SeqCst);
    engine.tick(0.21).unwrap();
    assert!(engine.status().trip_reason.contains("critical source"));
    engine.stop().unwrap();
    let log = lock(&device_calls);
    assert!(log.iter().rev().any(|c| *c == Call::Strength(0, 0)));
    assert!(log.contains(&Call::DeviceStop));
}
