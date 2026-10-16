//! Strict, bounded UDP v1 bridge source compatible with the Python client.

use serde_json::{Map, Value};
use std::{
    collections::VecDeque,
    io,
    net::{SocketAddr, ToSocketAddrs, UdpSocket},
    str,
    sync::{
        Arc, Mutex, MutexGuard,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use thiserror::Error;

pub const PROTOCOL_VERSION: i64 = 1;
pub const MAX_DATAGRAM: usize = 1024;
pub const QUEUE_CAPACITY: usize = 256;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PacketKind {
    Hello,
    Heartbeat,
    State,
    Bye,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Packet {
    pub kind: PacketKind,
    pub fields: Map<String, Value>,
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum DecodeError {
    #[error("datagram exceeds {MAX_DATAGRAM} bytes")]
    TooLarge,
    #[error("datagram is not valid UTF-8")]
    InvalidUtf8,
    #[error("datagram is not a JSON object")]
    NotObject,
    #[error("protocol version must be the integer 1")]
    InvalidVersion,
    #[error("unsupported bridge event")]
    InvalidEvent,
    #[error("state packet has invalid health values")]
    InvalidHealth,
    #[error("state packet limbs must be an array")]
    InvalidLimbs,
    #[error("invalid JSON: {0}")]
    Json(String),
}

#[derive(Debug, Clone, PartialEq)]
pub struct StateSample {
    pub received: f64,
    pub source_time: f64,
    pub hp: Option<f64>,
    pub hp_max: f64,
    pub limbs: Vec<bool>,
    pub bleeding: bool,
    pub dead: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct HookState {
    pub received: Option<f64>,
    pub transport_received: Option<f64>,
    pub mode: String,
    pub bridge_detail: String,
    pub hp: Option<f64>,
    pub hp_max: f64,
    pub limbs: Vec<bool>,
    pub bleeding: bool,
    pub dead: bool,
    pub build: String,
    pub version: String,
    pub profile: String,
    pub packets: u64,
    pub bad_packets: u64,
}

impl Default for HookState {
    fn default() -> Self {
        Self {
            received: None,
            transport_received: None,
            mode: String::new(),
            bridge_detail: String::new(),
            hp: None,
            hp_max: 100.0,
            limbs: Vec::new(),
            bleeding: false,
            dead: false,
            build: String::new(),
            version: String::new(),
            profile: String::new(),
            packets: 0,
            bad_packets: 0,
        }
    }
}

impl HookState {
    pub fn alive(&self, now: f64, timeout_s: f64) -> bool {
        self.received.is_some_and(|t| now - t <= timeout_s.max(0.5))
    }

    pub fn connected(&self, now: f64, timeout_s: f64) -> bool {
        self.transport_received
            .is_some_and(|t| now - t <= timeout_s.max(3.0))
    }
}

#[derive(Debug, Clone)]
struct QueuedPacket {
    packet: Packet,
    received: f64,
}

#[derive(Debug, Default)]
pub struct HookTracker {
    state: HookState,
    queue: VecDeque<QueuedPacket>,
    hello: Option<Map<String, Value>>,
}

impl HookTracker {
    pub fn state(&self) -> &HookState {
        &self.state
    }

    pub fn hello(&self) -> Option<&Map<String, Value>> {
        self.hello.as_ref()
    }

    pub fn queued(&self) -> usize {
        self.queue.len()
    }

    /// Validate and enqueue one datagram. Transport timestamps are updated at
    /// arrival, while queued state samples retain that original timestamp.
    pub fn ingest(&mut self, raw: &[u8], received: f64) -> Result<(), DecodeError> {
        let packet = match parse_packet(raw) {
            Ok(packet) => packet,
            Err(error) => {
                self.state.bad_packets = self.state.bad_packets.saturating_add(1);
                return Err(error);
            }
        };

        match packet.kind {
            PacketKind::Bye => {
                self.state.received = None;
                self.state.transport_received = None;
            }
            PacketKind::State => {
                self.state.received = Some(received);
                self.state.transport_received = Some(received);
            }
            PacketKind::Hello | PacketKind::Heartbeat => {
                self.state.transport_received = Some(received);
            }
        }

        if self.queue.len() == QUEUE_CAPACITY {
            self.queue.pop_front();
        }
        self.queue.push_back(QueuedPacket { packet, received });
        Ok(())
    }

    pub fn drain(&mut self) -> Vec<StateSample> {
        let mut samples = Vec::new();
        while let Some(queued) = self.queue.pop_front() {
            self.state.packets = self.state.packets.saturating_add(1);
            match queued.packet.kind {
                PacketKind::Hello | PacketKind::Heartbeat => {
                    self.apply_presence(queued.packet.kind, queued.packet.fields, queued.received);
                }
                PacketKind::Bye => {
                    self.state.received = None;
                    self.state.transport_received = None;
                }
                PacketKind::State => {
                    if let Some(sample) = decode_state(&queued.packet.fields, queued.received) {
                        self.state.transport_received = Some(queued.received);
                        self.state.received = Some(queued.received);
                        self.state.hp = sample.hp;
                        self.state.hp_max = sample.hp_max;
                        self.state.limbs.clone_from(&sample.limbs);
                        self.state.bleeding = sample.bleeding;
                        self.state.dead = sample.dead;
                        samples.push(sample);
                    }
                }
            }
        }
        samples
    }

    fn apply_presence(&mut self, kind: PacketKind, fields: Map<String, Value>, received: f64) {
        self.state.transport_received = Some(received);
        self.state.mode = text_field(&fields, "mode");
        self.state.bridge_detail = text_field(&fields, "detail").chars().take(400).collect();
        if kind == PacketKind::Hello {
            self.state.build = text_field(&fields, "build");
            self.state.version = text_field(&fields, "version");
            self.state.profile = text_field(&fields, "profile");
            self.hello = Some(fields);
        } else {
            update_nonempty(&mut self.state.build, text_field(&fields, "build"));
            update_nonempty(&mut self.state.version, text_field(&fields, "version"));
            update_nonempty(&mut self.state.profile, text_field(&fields, "profile"));
        }
    }
}

pub fn parse_packet(raw: &[u8]) -> Result<Packet, DecodeError> {
    if raw.len() > MAX_DATAGRAM {
        return Err(DecodeError::TooLarge);
    }
    let text = str::from_utf8(raw).map_err(|_| DecodeError::InvalidUtf8)?;
    let value: Value =
        serde_json::from_str(text.trim()).map_err(|error| DecodeError::Json(error.to_string()))?;
    let fields = value.as_object().cloned().ok_or(DecodeError::NotObject)?;

    let version = fields.get("v").ok_or(DecodeError::InvalidVersion)?;
    let exact_integer_one = version.as_number().is_some_and(|n| {
        (n.is_i64() && n.as_i64() == Some(PROTOCOL_VERSION))
            || (n.is_u64() && n.as_u64() == Some(PROTOCOL_VERSION as u64))
    });
    if !exact_integer_one {
        return Err(DecodeError::InvalidVersion);
    }

    let kind = match fields.get("event").and_then(Value::as_str) {
        Some("hello") => PacketKind::Hello,
        Some("heartbeat") => PacketKind::Heartbeat,
        Some("state") => PacketKind::State,
        Some("bye") => PacketKind::Bye,
        _ => return Err(DecodeError::InvalidEvent),
    };

    if kind == PacketKind::State {
        validate_state(&fields)?;
    }
    Ok(Packet { kind, fields })
}

fn validate_state(fields: &Map<String, Value>) -> Result<(), DecodeError> {
    let hp = fields
        .get("hp")
        .and_then(parse_float)
        .ok_or(DecodeError::InvalidHealth)?;
    if !hp.is_finite() {
        return Err(DecodeError::InvalidHealth);
    }
    if let Some(value) = fields.get("hp_max") {
        let hp_max = parse_float(value).ok_or(DecodeError::InvalidHealth)?;
        if !hp_max.is_finite() || hp_max <= 0.0 {
            return Err(DecodeError::InvalidHealth);
        }
    }
    if fields.get("limbs").is_some_and(|v| !v.is_array()) {
        return Err(DecodeError::InvalidLimbs);
    }
    Ok(())
}

fn decode_state(fields: &Map<String, Value>, received: f64) -> Option<StateSample> {
    let hp_raw = fields.get("hp").and_then(parse_float)?;
    let hp_max = fields
        .get("hp_max")
        .and_then(parse_float)
        .filter(|v| v.is_finite() && *v > 0.0)
        .unwrap_or(100.0);
    let hp = if hp_raw.is_finite() {
        Some((hp_raw / hp_max).clamp(0.0, 1.0))
    } else {
        None
    };
    let limbs = fields
        .get("limbs")
        .and_then(Value::as_array)
        .map(|items| items.iter().take(8).map(truthy).collect())
        .unwrap_or_default();
    Some(StateSample {
        received,
        source_time: fields
            .get("t")
            .and_then(parse_float)
            .filter(|v| v.is_finite())
            .unwrap_or(0.0),
        hp,
        hp_max,
        limbs,
        bleeding: fields.get("bleeding").is_some_and(truthy),
        dead: fields.get("dead").is_some_and(truthy),
    })
}

fn parse_float(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|n| n != 0.0),
        Value::String(value) => matches!(
            value.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
        _ => false,
    }
}

fn text_field(fields: &Map<String, Value>, key: &str) -> String {
    match fields.get(key) {
        Some(Value::String(value)) => value.clone(),
        Some(Value::Bool(true)) => "True".into(),
        Some(Value::Bool(false)) => "False".into(),
        Some(Value::Null) | None => String::new(),
        Some(value) => value.to_string(),
    }
}

fn update_nonempty(slot: &mut String, candidate: String) {
    if !candidate.is_empty() {
        *slot = candidate;
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// Owned UDP receiver with bounded shutdown and a shared protocol tracker.
pub struct HookSource {
    tracker: Arc<Mutex<HookTracker>>,
    origin: Instant,
    local_addr: SocketAddr,
    stopping: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
}

impl HookSource {
    pub fn start(address: impl ToSocketAddrs) -> io::Result<Self> {
        let socket = UdpSocket::bind(address)?;
        socket.set_read_timeout(Some(Duration::from_millis(200)))?;
        let local_addr = socket.local_addr()?;
        let tracker = Arc::new(Mutex::new(HookTracker::default()));
        let stopping = Arc::new(AtomicBool::new(false));
        let origin = Instant::now();
        let worker_tracker = Arc::clone(&tracker);
        let worker_stopping = Arc::clone(&stopping);
        let worker = thread::Builder::new()
            .name("hd2-udp-bridge".into())
            .spawn(move || {
                let mut buffer = [0_u8; MAX_DATAGRAM];
                while !worker_stopping.load(Ordering::Acquire) {
                    match socket.recv_from(&mut buffer) {
                        Ok((size, _peer)) => {
                            lock(&worker_tracker)
                                .ingest(&buffer[..size], origin.elapsed().as_secs_f64())
                                .ok();
                        }
                        Err(error)
                            if matches!(
                                error.kind(),
                                io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                            ) => {}
                        Err(_) => break,
                    }
                }
            })?;
        Ok(Self {
            tracker,
            origin,
            local_addr,
            stopping,
            worker: Some(worker),
        })
    }

    pub fn local_addr(&self) -> SocketAddr {
        self.local_addr
    }

    pub fn now(&self) -> f64 {
        self.origin.elapsed().as_secs_f64()
    }

    pub fn state(&self) -> HookState {
        lock(&self.tracker).state().clone()
    }

    pub fn drain(&self) -> Vec<StateSample> {
        lock(&self.tracker).drain()
    }

    pub fn stop(&mut self) {
        self.stopping.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

impl Drop for HookSource {
    fn drop(&mut self) {
        self.stop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn packet(json: &str) -> Vec<u8> {
        json.as_bytes().to_vec()
    }

    #[test]
    fn strict_version_and_packet_shape() {
        assert!(parse_packet(&packet(r#"{"v":1,"event":"heartbeat"}"#)).is_ok());
        assert_eq!(
            parse_packet(&packet(r#"{"v":1.0,"event":"heartbeat"}"#)),
            Err(DecodeError::InvalidVersion)
        );
        assert_eq!(
            parse_packet(&packet(r#"{"v":true,"event":"heartbeat"}"#)),
            Err(DecodeError::InvalidVersion)
        );
        assert_eq!(parse_packet(&packet(r#"[]"#)), Err(DecodeError::NotObject));
    }

    #[test]
    fn heartbeat_connects_but_does_not_refresh_state() {
        let mut tracker = HookTracker::default();
        tracker
            .ingest(
                &packet(r#"{"v":1,"event":"state","hp":80,"hp_max":100}"#),
                1.0,
            )
            .unwrap();
        tracker.drain();
        tracker
            .ingest(&packet(r#"{"v":1,"event":"heartbeat"}"#), 5.0)
            .unwrap();
        tracker.drain();
        assert!(!tracker.state().alive(5.0, 2.0));
        assert!(tracker.state().connected(5.0, 2.0));
        assert_eq!(tracker.state().received, Some(1.0));
    }

    #[test]
    fn malformed_packet_never_claims_presence() {
        let mut tracker = HookTracker::default();
        assert!(tracker.ingest(b"not-json", 10.0).is_err());
        assert_eq!(tracker.state().transport_received, None);
        assert_eq!(tracker.state().bad_packets, 1);
    }

    #[test]
    fn bye_disconnects_transport_and_state() {
        let mut tracker = HookTracker::default();
        tracker
            .ingest(&packet(r#"{"v":1,"event":"state","hp":50}"#), 1.0)
            .unwrap();
        tracker
            .ingest(&packet(r#"{"v":1,"event":"bye"}"#), 2.0)
            .unwrap();
        assert_eq!(tracker.state().received, None);
        assert_eq!(tracker.state().transport_received, None);
        tracker.drain();
        assert_eq!(tracker.state().received, None);
        assert_eq!(tracker.state().transport_received, None);
    }

    #[test]
    fn queued_state_keeps_arrival_timestamp() {
        let mut tracker = HookTracker::default();
        tracker.ingest(&packet(r#"{"v":1,"event":"state","hp":"25","hp_max":100,"limbs":[1,0,"yes"],"bleeding":"on"}"#), 3.0).unwrap();
        let samples = tracker.drain();
        assert_eq!(samples[0].received, 3.0);
        assert_eq!(samples[0].hp, Some(0.25));
        assert_eq!(samples[0].limbs, vec![true, false, true]);
        assert!(samples[0].bleeding);
    }

    #[test]
    fn bounded_queue_drops_oldest() {
        let mut tracker = HookTracker::default();
        for index in 0..(QUEUE_CAPACITY + 10) {
            let body = format!(r#"{{"v":1,"event":"state","hp":{index},"hp_max":1000}}"#);
            tracker.ingest(body.as_bytes(), index as f64).unwrap();
        }
        assert_eq!(tracker.queued(), QUEUE_CAPACITY);
        let samples = tracker.drain();
        assert_eq!(samples.len(), QUEUE_CAPACITY);
        assert_eq!(samples[0].received, 10.0);
    }
}
