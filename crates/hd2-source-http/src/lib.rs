//! Bounded HTTP event/state input compatible with the Python client.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::collections::VecDeque;
use std::io::Read;
use std::net::{IpAddr, SocketAddr};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use subtle::ConstantTimeEq;
use tiny_http::{Header, Method, Request, Response, Server, StatusCode};
use url::form_urlencoded;

pub const MAX_BODY: usize = 64 * 1024;
pub const MAX_QUEUE: usize = 256;

#[derive(Clone, Debug)]
pub struct HttpSourceConfig {
    pub host: IpAddr,
    pub port: u16,
    pub token: String,
    pub max_per_s: usize,
    pub state_timeout: Duration,
}

impl Default for HttpSourceConfig {
    fn default() -> Self {
        Self {
            host: "127.0.0.1".parse().expect("literal IP"),
            port: 47_778,
            token: String::new(),
            max_per_s: 20,
            state_timeout: Duration::from_secs(3),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SourceItem {
    State {
        hp_ratio: f64,
        limbs: Vec<bool>,
        dead: bool,
        bleeding: bool,
    },
    Damage {
        severity: f64,
        hp_before: f64,
        hp_after: f64,
    },
    LimbInjury {
        slot: usize,
        bleeding: bool,
        name: String,
    },
    Death,
    Revive,
    LowHealth {
        ratio: f64,
    },
}

#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct SourceStats {
    pub packets: u64,
    pub events: u64,
    pub bad: u64,
    pub dropped: u64,
    pub queued: usize,
    pub state_seen: bool,
    pub state_age_s: Option<f64>,
    pub listening: bool,
    pub token_required: bool,
    pub max_per_s: usize,
}

#[derive(Debug)]
struct Shared {
    queue: VecDeque<SourceItem>,
    rate: VecDeque<Instant>,
    stats: SourceStats,
    last_state: Option<Instant>,
}

impl Shared {
    fn new(config: &HttpSourceConfig) -> Self {
        Self {
            queue: VecDeque::with_capacity(MAX_QUEUE),
            rate: VecDeque::new(),
            stats: SourceStats {
                token_required: !config.token.is_empty(),
                max_per_s: config.max_per_s.max(1),
                ..SourceStats::default()
            },
            last_state: None,
        }
    }

    fn allow(&mut self, now: Instant, limit: usize) -> bool {
        while self
            .rate
            .front()
            .is_some_and(|t| now.duration_since(*t) >= Duration::from_secs(1))
        {
            self.rate.pop_front();
        }
        if self.rate.len() >= limit.max(1) {
            self.stats.dropped += 1;
            return false;
        }
        self.rate.push_back(now);
        true
    }

    fn push(&mut self, item: SourceItem, now: Instant) {
        if matches!(item, SourceItem::State { .. }) {
            self.stats.state_seen = true;
            self.last_state = Some(now);
        }
        if self.queue.len() == MAX_QUEUE {
            self.queue.pop_front();
            self.stats.dropped += 1;
        }
        self.queue.push_back(item);
        self.stats.events += 1;
        self.stats.queued = self.queue.len();
    }
}

pub struct HttpSource {
    shared: Arc<Mutex<Shared>>,
    stop: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
    addr: SocketAddr,
    state_timeout: Duration,
}

impl HttpSource {
    pub fn start(config: HttpSourceConfig) -> Result<Self, String> {
        let server =
            Server::http(SocketAddr::new(config.host, config.port)).map_err(|e| e.to_string())?;
        let tiny_http::ListenAddr::IP(addr) = server.server_addr();
        let shared = Arc::new(Mutex::new(Shared::new(&config)));
        shared.lock().expect("source mutex").stats.listening = true;
        let stop = Arc::new(AtomicBool::new(false));
        let thread_shared = Arc::clone(&shared);
        let thread_stop = Arc::clone(&stop);
        let thread_config = config.clone();
        let worker = thread::Builder::new()
            .name("hd2-http-source".into())
            .spawn(move || {
                while !thread_stop.load(Ordering::Acquire) {
                    match server.recv_timeout(Duration::from_millis(100)) {
                        Ok(Some(request)) => {
                            handle_request(request, &thread_config, &thread_shared)
                        }
                        Ok(None) => {}
                        Err(_) => break,
                    }
                }
                thread_shared.lock().expect("source mutex").stats.listening = false;
            })
            .map_err(|e| e.to_string())?;
        Ok(Self {
            shared,
            stop,
            worker: Some(worker),
            addr,
            state_timeout: config.state_timeout,
        })
    }

    pub fn addr(&self) -> SocketAddr {
        self.addr
    }

    pub fn poll(&self) -> Vec<SourceItem> {
        let mut guard = self.shared.lock().expect("source mutex");
        let items = guard.queue.drain(..).collect();
        guard.stats.queued = 0;
        items
    }

    pub fn critical(&self) -> bool {
        self.state_timeout > Duration::ZERO
            && self.shared.lock().expect("source mutex").stats.state_seen
    }

    pub fn alive(&self) -> bool {
        let guard = self.shared.lock().expect("source mutex");
        if !guard.stats.listening {
            return false;
        }
        if self.state_timeout == Duration::ZERO || !guard.stats.state_seen {
            return true;
        }
        guard
            .last_state
            .is_some_and(|t| t.elapsed() <= self.state_timeout)
    }

    pub fn stats(&self) -> SourceStats {
        let guard = self.shared.lock().expect("source mutex");
        let mut out = guard.stats.clone();
        out.queued = guard.queue.len();
        out.state_age_s = guard.last_state.map(|t| t.elapsed().as_secs_f64());
        out
    }

    pub fn stop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

impl Drop for HttpSource {
    fn drop(&mut self) {
        self.stop();
    }
}

fn json_header() -> Header {
    Header::from_bytes("Content-Type", "application/json; charset=utf-8").expect("static header")
}

fn respond(request: Request, status: u16, body: Value) {
    let response = Response::from_string(body.to_string())
        .with_status_code(StatusCode(status))
        .with_header(json_header());
    let _ = request.respond(response);
}

fn request_token(request: &Request) -> String {
    if let Some(value) = request
        .headers()
        .iter()
        .find(|h| h.field.equiv("X-HD2Coyote-Token"))
        .map(|h| h.value.as_str().to_owned())
        .filter(|v| !v.is_empty())
    {
        return value;
    }
    request
        .url()
        .split_once('?')
        .map(|(_, query)| {
            form_urlencoded::parse(query.as_bytes())
                .find(|(key, _)| key == "token")
                .map(|(_, value)| value.into_owned())
                .unwrap_or_default()
        })
        .unwrap_or_default()
}

fn token_matches(expected: &str, supplied: &str) -> bool {
    expected.is_empty()
        || (expected.len() == supplied.len()
            && bool::from(expected.as_bytes().ct_eq(supplied.as_bytes())))
}

fn handle_request(mut request: Request, config: &HttpSourceConfig, shared: &Arc<Mutex<Shared>>) {
    let path = request.url().split('?').next().unwrap_or("/").to_owned();
    if request.method() == &Method::Get {
        if matches!(path.as_str(), "/health" | "/status") {
            let stats = snapshot_stats(shared);
            return respond(request, 200, json!({"ok": true, "v": 1, "stats": stats}));
        }
        if path != "/" {
            return respond(request, 404, json!({"ok": false, "error": "not found"}));
        }
        if !token_matches(&config.token, &request_token(&request)) {
            return respond(request, 401, json!({"ok": false, "error": "unauthorized"}));
        }
        let stats = snapshot_stats(shared);
        return respond(
            request,
            200,
            json!({"ok": true, "v": 1, "stats": stats,
            "usage": {"post": ["/", "/event", "/state"]}}),
        );
    }
    if request.method() != &Method::Post || !matches!(path.as_str(), "/" | "/event" | "/state") {
        return respond(request, 404, json!({"ok": false, "error": "not found"}));
    }
    if !token_matches(&config.token, &request_token(&request)) {
        return respond(request, 401, json!({"ok": false, "error": "unauthorized"}));
    }
    let now = Instant::now();
    if !shared
        .lock()
        .expect("source mutex")
        .allow(now, config.max_per_s)
    {
        return respond(request, 429, json!({"ok": false, "error": "rate limit"}));
    }
    let mut bytes = Vec::new();
    if request
        .as_reader()
        .take((MAX_BODY + 1) as u64)
        .read_to_end(&mut bytes)
        .is_err()
        || bytes.len() > MAX_BODY
    {
        return respond(
            request,
            413,
            json!({"ok": false, "error": "body too large"}),
        );
    }
    let value: Value = match serde_json::from_slice(&bytes) {
        Ok(value) => value,
        Err(_) => {
            shared.lock().expect("source mutex").stats.bad += 1;
            return respond(request, 400, json!({"ok": false, "error": "invalid json"}));
        }
    };
    let values: Vec<Value> = match value {
        Value::Array(v) => v,
        other => vec![other],
    };
    let mut accepted = 0usize;
    for value in values {
        let Some(object) = value.as_object() else {
            continue;
        };
        let mut guard = shared.lock().expect("source mutex");
        guard.stats.packets += 1;
        if let Some(item) = parse_item(object, &path) {
            guard.push(item, now);
            accepted += 1;
        } else {
            guard.stats.bad += 1;
        }
    }
    let stats = snapshot_stats(shared);
    respond(
        request,
        200,
        json!({"ok": true, "accepted": accepted, "stats": stats}),
    );
}

fn snapshot_stats(shared: &Arc<Mutex<Shared>>) -> SourceStats {
    let guard = shared.lock().expect("source mutex");
    let mut stats = guard.stats.clone();
    stats.queued = guard.queue.len();
    stats.state_age_s = guard.last_state.map(|t| t.elapsed().as_secs_f64());
    stats
}

fn parse_item(object: &Map<String, Value>, path: &str) -> Option<SourceItem> {
    let kind = string_field(object, &["ev", "event", "kind"])
        .unwrap_or_default()
        .trim()
        .to_lowercase();
    let looks_state = path == "/state"
        || matches!(kind.as_str(), "" | "state" | "status" | "player")
            && ["hp", "hp_max", "limbs", "dead", "bleeding"]
                .iter()
                .any(|k| object.contains_key(*k));
    if looks_state {
        let hp = finite_field(object, &["hp"])?;
        let ratio = if let Some(max) = finite_field(object, &["hp_max"]).filter(|v| *v > 0.0) {
            hp / max
        } else if hp <= 1.0 {
            hp
        } else {
            hp / 100.0
        };
        let limbs = parse_limbs(object.get("limbs"));
        return Some(SourceItem::State {
            hp_ratio: ratio.clamp(0.0, 1.0),
            limbs,
            dead: truthy(object.get("dead")),
            bleeding: truthy(object.get("bleeding")),
        });
    }
    match kind.as_str() {
        "damage" | "hit" | "hurt" | "受伤" | "掉血" => Some(SourceItem::Damage {
            severity: finite_field(object, &["severity", "damage", "pct"])
                .unwrap_or(0.0)
                .max(0.0),
            hp_before: finite_field(object, &["hp_before"]).unwrap_or(1.0),
            hp_after: finite_field(object, &["hp_after"]).unwrap_or(1.0),
        }),
        "limb" | "limb_injury" | "injury" | "肢体" => Some(SourceItem::LimbInjury {
            slot: finite_field(object, &["slot"]).unwrap_or(0.0).max(0.0) as usize,
            bleeding: truthy(object.get("bleeding")),
            name: string_field(object, &["name"]).unwrap_or_default(),
        }),
        "death" | "dead" | "阵亡" => Some(SourceItem::Death),
        "revive" | "respawn" | "复活" | "增援" => Some(SourceItem::Revive),
        "low_health" | "lowhp" | "低血量" => Some(SourceItem::LowHealth {
            ratio: finite_field(object, &["ratio", "hp"])
                .unwrap_or(0.0)
                .clamp(0.0, 1.0),
        }),
        _ => None,
    }
}

fn finite_field(object: &Map<String, Value>, keys: &[&str]) -> Option<f64> {
    keys.iter()
        .find_map(|key| object.get(*key))
        .and_then(|value| match value {
            Value::Number(n) => n.as_f64().filter(|v| v.is_finite()),
            Value::String(s) => s.parse::<f64>().ok().filter(|v| v.is_finite()),
            _ => None,
        })
}

fn string_field(object: &Map<String, Value>, keys: &[&str]) -> Option<String> {
    keys.iter()
        .find_map(|key| object.get(*key))
        .and_then(Value::as_str)
        .map(str::to_owned)
}

fn truthy(value: Option<&Value>) -> bool {
    match value {
        Some(Value::Bool(v)) => *v,
        Some(Value::Number(v)) => v.as_f64().is_some_and(|v| v.is_finite() && v != 0.0),
        Some(Value::String(v)) => matches!(
            v.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
        _ => false,
    }
}

fn parse_limbs(value: Option<&Value>) -> Vec<bool> {
    let values: Vec<&Value> = match value {
        Some(Value::Array(items)) => items.iter().take(8).collect(),
        Some(Value::Object(items)) => {
            let mut pairs: Vec<_> = items.iter().collect();
            pairs.sort_by(|a, b| a.0.cmp(b.0));
            pairs.into_iter().map(|(_, value)| value).take(8).collect()
        }
        _ => Vec::new(),
    };
    values.into_iter().map(|v| truthy(Some(v))).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn object(value: Value) -> Map<String, Value> {
        value.as_object().unwrap().clone()
    }

    #[test]
    fn normalizes_state_and_sorted_limbs() {
        let item = parse_item(
            &object(json!({"hp": 75, "hp_max": 100,
            "limbs": {"2": 0, "0": 1, "1": "yes"}, "dead": false})),
            "/state",
        )
        .unwrap();
        assert_eq!(
            item,
            SourceItem::State {
                hp_ratio: 0.75,
                limbs: vec![true, true, false],
                dead: false,
                bleeding: false
            }
        );
    }

    #[test]
    fn parses_event_synonyms_and_rejects_nan() {
        assert!(matches!(
            parse_item(&object(json!({"event":"掉血","pct":12})), "/event"),
            Some(SourceItem::Damage { severity: 12.0, .. })
        ));
        assert!(parse_item(&object(json!({"event":"state","hp":"NaN"})), "/state").is_none());
    }

    #[test]
    fn bounded_queue_drops_oldest() {
        let config = HttpSourceConfig::default();
        let mut shared = Shared::new(&config);
        let now = Instant::now();
        for _ in 0..MAX_QUEUE + 4 {
            shared.push(SourceItem::Death, now);
        }
        assert_eq!(shared.queue.len(), MAX_QUEUE);
        assert_eq!(shared.stats.dropped, 4);
    }

    #[test]
    fn rate_window_is_fixed_to_one_second() {
        let config = HttpSourceConfig::default();
        let mut shared = Shared::new(&config);
        let now = Instant::now();
        assert!(shared.allow(now, 2));
        assert!(shared.allow(now, 2));
        assert!(!shared.allow(now, 2));
        assert!(shared.allow(now + Duration::from_secs(1), 2));
    }

    #[test]
    fn token_comparison_handles_lengths() {
        assert!(token_matches("", "anything"));
        assert!(token_matches("secret", "secret"));
        assert!(!token_matches("secret", "secrex"));
        assert!(!token_matches("secret", "short"));
    }
}
