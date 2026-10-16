use std::{collections::HashMap, net::SocketAddr, sync::Arc, time::Instant};

use futures_util::{SinkExt, StreamExt};
use hd2_core::{
    events::Event,
    protocol::{MAX_FRAME_BYTES, MAX_PULSE_UNITS, SAFE_FRAME_BYTES},
};
use serde::{Deserialize, Serialize};
use tokio::{
    net::{TcpListener, TcpStream, UdpSocket},
    sync::{Mutex, RwLock, broadcast, mpsc, watch},
    task::JoinHandle,
    time::{self, Duration},
};
use tokio_tungstenite::{accept_async, tungstenite::Message};
use uuid::Uuid;

const HISTORY_LIMIT: usize = 500;
const HISTORY_TRIM: usize = 250;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
    #[error("websocket error: {0}")]
    WebSocket(#[from] tokio_tungstenite::tungstenite::Error),
    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("invalid channel: {0}")]
    InvalidChannel(String),
    #[error("invalid pulse unit")]
    InvalidPulse,
    #[error("device task has stopped")]
    Stopped,
}

#[derive(Debug, Clone)]
pub struct Config {
    pub host: String,
    pub port: u16,
    pub advertise_ip: Option<String>,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            host: "0.0.0.0".into(),
            port: 9999,
            advertise_ip: None,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Snapshot {
    pub connected: bool,
    pub bound_clients: usize,
    pub limit_a: u16,
    pub limit_b: u16,
    pub local_addr: SocketAddr,
    pub client_id: String,
    pub ws_url: String,
    pub qr_url: String,
}

#[derive(Debug, Clone)]
struct Client {
    tx: mpsc::Sender<String>,
    target_id: String,
    bound: bool,
}

#[derive(Debug)]
struct State {
    clients: HashMap<Uuid, Client>,
    limit_a: u16,
    limit_b: u16,
    history: Vec<String>,
}

struct Inner {
    state: RwLock<State>,
    local_addr: SocketAddr,
    client_id: String,
    ws_url: String,
    qr_url: String,
    started: Instant,
    events: broadcast::Sender<Event>,
    shutdown: watch::Sender<bool>,
    accept_task: Mutex<Option<JoinHandle<()>>>,
}

#[derive(Clone)]
pub struct DgLabDevice(Arc<Inner>);

#[derive(Debug, Deserialize)]
struct Incoming {
    #[serde(rename = "type")]
    kind: String,
    #[serde(rename = "targetId", default)]
    target_id: String,
    #[serde(default)]
    message: String,
}

#[derive(Serialize)]
struct Envelope<'a> {
    #[serde(rename = "type")]
    kind: &'a str,
    #[serde(rename = "clientId")]
    client_id: &'a str,
    #[serde(rename = "targetId")]
    target_id: &'a str,
    message: &'a str,
}

impl DgLabDevice {
    pub async fn bind(config: Config) -> Result<Self, Error> {
        let listener = TcpListener::bind((config.host.as_str(), config.port)).await?;
        let local_addr = listener.local_addr()?;
        let client_id = Uuid::new_v4().to_string();
        let advertise_ip = match config.advertise_ip.filter(|s| !s.trim().is_empty()) {
            Some(ip) => ip,
            None => discover_lan_ip().await,
        };
        let ws_url = format!("ws://{}:{}/{}", advertise_ip, local_addr.port(), client_id);
        let qr_url = hd2_core::protocol::qr_url(&ws_url);
        let (events, _) = broadcast::channel(256);
        let (shutdown, shutdown_rx) = watch::channel(false);
        let inner = Arc::new(Inner {
            state: RwLock::new(State {
                clients: HashMap::new(),
                limit_a: 200,
                limit_b: 200,
                history: Vec::new(),
            }),
            local_addr,
            client_id,
            ws_url,
            qr_url,
            started: Instant::now(),
            events,
            shutdown,
            accept_task: Mutex::new(None),
        });
        let task_inner = inner.clone();
        let task =
            tokio::spawn(async move { accept_loop(listener, task_inner, shutdown_rx).await });
        *inner.accept_task.lock().await = Some(task);
        Ok(Self(inner))
    }

    pub fn subscribe(&self) -> broadcast::Receiver<Event> {
        self.0.events.subscribe()
    }

    pub async fn snapshot(&self) -> Snapshot {
        let state = self.0.state.read().await;
        let bound_clients = state.clients.values().filter(|c| c.bound).count();
        Snapshot {
            connected: bound_clients > 0,
            bound_clients,
            limit_a: state.limit_a,
            limit_b: state.limit_b,
            local_addr: self.0.local_addr,
            client_id: self.0.client_id.clone(),
            ws_url: self.0.ws_url.clone(),
            qr_url: self.0.qr_url.clone(),
        }
    }

    pub async fn history(&self) -> Vec<String> {
        self.0.state.read().await.history.clone()
    }

    pub async fn set_strength(&self, channel: &str, value: u16, mode: u8) -> Result<(), Error> {
        let (number, limit) = {
            let state = self.0.state.read().await;
            match channel.to_ascii_uppercase().as_str() {
                "A" => (1, state.limit_a),
                "B" => (2, state.limit_b),
                other => return Err(Error::InvalidChannel(other.into())),
            }
        };
        self.broadcast(
            "msg",
            &format!("strength-{number}+{mode}+{}", value.min(limit)),
        )
        .await
    }

    pub async fn clear(&self, channel: &str) -> Result<(), Error> {
        let number = match channel.to_ascii_uppercase().as_str() {
            "A" => 1,
            "B" => 2,
            other => return Err(Error::InvalidChannel(other.into())),
        };
        self.broadcast("msg", &format!("clear-{number}")).await
    }

    pub async fn send_wave(&self, channel: &str, units: &[String]) -> Result<(), Error> {
        let channel = channel.to_ascii_uppercase();
        if channel != "A" && channel != "B" {
            return Err(Error::InvalidChannel(channel));
        }
        if units.is_empty() || units.iter().any(|u| !valid_pulse(u)) {
            return Err(Error::InvalidPulse);
        }
        let chunk_size = self.max_units_per_frame().max(1);
        for chunk in units.chunks(chunk_size) {
            let encoded = serde_json::to_string(chunk)?;
            self.broadcast("msg", &format!("pulse-{channel}:{encoded}"))
                .await?;
        }
        Ok(())
    }

    pub async fn mute(&self) {
        let _ = self.clear("A").await;
        let _ = self.clear("B").await;
        let _ = self.set_strength("A", 0, 2).await;
        let _ = self.set_strength("B", 0, 2).await;
    }

    pub async fn stop(&self) {
        self.mute().await;
        let _ = self.0.shutdown.send(true);
        if let Some(task) = self.0.accept_task.lock().await.take() {
            let _ = time::timeout(Duration::from_secs(2), task).await;
        }
    }

    async fn broadcast(&self, kind: &str, message: &str) -> Result<(), Error> {
        let deliveries: Vec<(mpsc::Sender<String>, String)> = {
            let state = self.0.state.read().await;
            state
                .clients
                .values()
                .filter(|c| c.bound)
                .map(|c| {
                    let text = serde_json::to_string(&Envelope {
                        kind,
                        client_id: &self.0.client_id,
                        target_id: &c.target_id,
                        message,
                    })
                    .expect("serializable envelope");
                    (c.tx.clone(), text)
                })
                .collect()
        };
        for (tx, text) in deliveries {
            if text.len() >= MAX_FRAME_BYTES {
                return Err(Error::InvalidPulse);
            }
            record_history(&self.0, text.clone()).await;
            tx.send(text).await.map_err(|_| Error::Stopped)?;
        }
        Ok(())
    }

    fn max_units_per_frame(&self) -> usize {
        let target = "x".repeat(36);
        let empty = serde_json::to_string(&Envelope {
            kind: "msg",
            client_id: &self.0.client_id,
            target_id: &target,
            message: "pulse-A:[]",
        })
        .unwrap();
        let mut count =
            ((SAFE_FRAME_BYTES.saturating_sub(empty.len())) / 21).clamp(1, MAX_PULSE_UNITS);
        let sample = "0".repeat(16);
        while count > 1 {
            let encoded = serde_json::to_string(&vec![sample.clone(); count]).unwrap();
            let text = serde_json::to_string(&Envelope {
                kind: "msg",
                client_id: &self.0.client_id,
                target_id: &target,
                message: &format!("pulse-A:{encoded}"),
            })
            .unwrap();
            if text.len() <= MAX_FRAME_BYTES - 20 {
                break;
            }
            count -= 1;
        }
        count
    }
}

impl Drop for DgLabDevice {
    fn drop(&mut self) {
        if Arc::strong_count(&self.0) == 2 {
            let _ = self.0.shutdown.send(true);
        }
    }
}

async fn accept_loop(
    listener: TcpListener,
    inner: Arc<Inner>,
    mut shutdown: watch::Receiver<bool>,
) {
    loop {
        tokio::select! {
            changed = shutdown.changed() => if changed.is_err() || *shutdown.borrow() { break; },
            accepted = listener.accept() => match accepted {
                Ok((stream, _)) => { let inner = inner.clone(); let rx = shutdown.clone(); tokio::spawn(async move { let _ = handle_connection(stream, inner, rx).await; }); }
                Err(_) => break,
            }
        }
    }
}

async fn handle_connection(
    stream: TcpStream,
    inner: Arc<Inner>,
    mut shutdown: watch::Receiver<bool>,
) -> Result<(), Error> {
    let ws = accept_async(stream).await?;
    let (mut sink, mut source) = ws.split();
    let connection_id = Uuid::new_v4();
    let initial_target = Uuid::new_v4().to_string();
    let (tx, mut rx) = mpsc::channel::<String>(128);
    inner.state.write().await.clients.insert(
        connection_id,
        Client {
            tx: tx.clone(),
            target_id: initial_target.clone(),
            bound: false,
        },
    );
    let initial = serde_json::to_string(&Envelope {
        kind: "bind",
        client_id: &inner.client_id,
        target_id: &initial_target,
        message: "targetId",
    })?;
    sink.send(Message::Text(initial.into())).await?;
    let mut heartbeat = time::interval(Duration::from_secs(25));
    heartbeat.tick().await;
    loop {
        tokio::select! {
            changed = shutdown.changed() => if changed.is_err() || *shutdown.borrow() { break; },
            outgoing = rx.recv() => match outgoing {
                Some(text) => sink.send(Message::Text(text.into())).await?,
                None => break,
            },
            _ = heartbeat.tick() => {
                let target = current_target(&inner, connection_id).await.unwrap_or_default();
                let text = serde_json::to_string(&Envelope { kind: "heartbeat", client_id: &inner.client_id, target_id: &target, message: "200" })?;
                sink.send(Message::Text(text.into())).await?;
            },
            incoming = source.next() => match incoming {
                Some(Ok(Message::Text(text))) => process_message(&inner, connection_id, &tx, text.as_str()).await?,
                Some(Ok(Message::Ping(bytes))) => sink.send(Message::Pong(bytes)).await?,
                Some(Ok(Message::Close(_))) | None => break,
                Some(Err(error)) => return Err(error.into()),
                _ => {}
            }
        }
    }
    inner.state.write().await.clients.remove(&connection_id);
    emit_connection_state(&inner, "disconnected").await;
    Ok(())
}

async fn process_message(
    inner: &Arc<Inner>,
    id: Uuid,
    tx: &mpsc::Sender<String>,
    text: &str,
) -> Result<(), Error> {
    let incoming: Incoming = serde_json::from_str(text)?;
    let mut reply: Option<String> = None;
    match incoming.kind.as_str() {
        "bind" if incoming.message == "DGLAB" => {
            let target = {
                let mut state = inner.state.write().await;
                let client = state.clients.get_mut(&id).ok_or(Error::Stopped)?;
                if !incoming.target_id.is_empty() {
                    client.target_id = incoming.target_id.clone();
                }
                client.bound = true;
                client.target_id.clone()
            };
            let bind_reply = serde_json::to_string(&Envelope {
                kind: "bind",
                client_id: &inner.client_id,
                target_id: &target,
                message: "200",
            })?;
            record_history(inner, bind_reply.clone()).await;
            tx.send(bind_reply).await.map_err(|_| Error::Stopped)?;
            emit_connection_state(inner, "connected").await;
            for message in ["strength-1+2+0", "strength-2+2+0"] {
                let text = serde_json::to_string(&Envelope {
                    kind: "msg",
                    client_id: &inner.client_id,
                    target_id: &target,
                    message,
                })?;
                record_history(inner, text.clone()).await;
                tx.send(text).await.map_err(|_| Error::Stopped)?;
            }
        }
        "msg" => {
            if let Some(rest) = incoming.message.strip_prefix("strength-") {
                let parts: Vec<_> = rest.split('+').collect();
                if parts.len() >= 4 {
                    if let (Ok(a), Ok(b)) = (parts[2].parse::<u16>(), parts[3].parse::<u16>()) {
                        let mut state = inner.state.write().await;
                        state.limit_a = a.min(200);
                        state.limit_b = b.min(200);
                        if let Some(client) = state.clients.get_mut(&id) {
                            client.bound = true;
                        }
                    }
                }
            } else if let Some(index) = incoming
                .message
                .strip_prefix("feedback-")
                .and_then(|v| v.parse::<usize>().ok())
            {
                if index <= 9 {
                    let _ = inner.events.send(Event::Feedback {
                        t: inner.started.elapsed().as_secs_f64(),
                        index,
                    });
                }
            }
        }
        "heartbeat" if incoming.message != "200" => {
            let target = current_target(inner, id)
                .await
                .unwrap_or(incoming.target_id);
            reply = Some(serde_json::to_string(&Envelope {
                kind: "heartbeat",
                client_id: &inner.client_id,
                target_id: &target,
                message: "200",
            })?);
        }
        _ => {}
    }
    if let Some(reply) = reply {
        tx.send(reply).await.map_err(|_| Error::Stopped)?;
    }
    Ok(())
}

async fn current_target(inner: &Arc<Inner>, id: Uuid) -> Option<String> {
    inner
        .state
        .read()
        .await
        .clients
        .get(&id)
        .map(|c| c.target_id.clone())
}

async fn emit_connection_state(inner: &Arc<Inner>, detail: &str) {
    let connected = inner.state.read().await.clients.values().any(|c| c.bound);
    let _ = inner.events.send(Event::DeviceState {
        t: inner.started.elapsed().as_secs_f64(),
        connected,
        detail: detail.into(),
    });
}

async fn record_history(inner: &Arc<Inner>, text: String) {
    let mut state = inner.state.write().await;
    state.history.push(text);
    if state.history.len() > HISTORY_LIMIT {
        state.history.drain(..HISTORY_TRIM);
    }
}

fn valid_pulse(value: &str) -> bool {
    value.len() == 16 && value.bytes().all(|b| b.is_ascii_hexdigit())
}

async fn discover_lan_ip() -> String {
    if let Ok(socket) = UdpSocket::bind("0.0.0.0:0").await {
        if socket.connect("8.8.8.8:80").await.is_ok() {
            if let Ok(addr) = socket.local_addr() {
                return addr.ip().to_string();
            }
        }
    }
    "127.0.0.1".into()
}

#[cfg(test)]
mod tests {
    use super::*;
    use futures_util::{SinkExt, StreamExt};
    use serde_json::{Value, json};
    use tokio_tungstenite::{connect_async, tungstenite::Message};

    async fn text<S>(stream: &mut S) -> String
    where
        S: StreamExt<Item = Result<Message, tokio_tungstenite::tungstenite::Error>> + Unpin,
    {
        loop {
            if let Some(Ok(Message::Text(value))) = stream.next().await {
                return value.to_string();
            }
        }
    }

    #[tokio::test]
    async fn handshake_commands_feedback_and_chunking() {
        let device = DgLabDevice::bind(Config {
            host: "127.0.0.1".into(),
            port: 0,
            advertise_ip: Some("127.0.0.1".into()),
        })
        .await
        .unwrap();
        let snapshot = device.snapshot().await;
        let (mut ws, _) = connect_async(&snapshot.ws_url).await.unwrap();
        let bind: Value = serde_json::from_str(&text(&mut ws).await).unwrap();
        assert_eq!(bind["type"], "bind");
        let target = bind["targetId"].as_str().unwrap();
        ws.send(Message::Text(json!({"type":"bind","clientId":snapshot.client_id,"targetId":target,"message":"DGLAB"}).to_string().into())).await.unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&text(&mut ws).await).unwrap()["message"],
            "200"
        );
        assert!(text(&mut ws).await.contains("strength-1+2+0"));
        assert!(text(&mut ws).await.contains("strength-2+2+0"));
        device.set_strength("A", 77, 2).await.unwrap();
        assert!(text(&mut ws).await.contains("strength-1+2+77"));
        let units = vec!["0A0B0C0D0E0F1011".to_string(); 250];
        device.send_wave("B", &units).await.unwrap();
        let mut seen = 0;
        while seen < 250 {
            let frame = text(&mut ws).await;
            assert!(frame.len() < MAX_FRAME_BYTES);
            let value: Value = serde_json::from_str(&frame).unwrap();
            if let Some(payload) = value["message"]
                .as_str()
                .and_then(|m| m.strip_prefix("pulse-B:"))
            {
                let chunk: Vec<String> = serde_json::from_str(payload).unwrap();
                assert!(chunk.len() <= MAX_PULSE_UNITS);
                seen += chunk.len();
            }
        }
        let mut events = device.subscribe();
        // Subscribe before the next feedback so broadcast delivery is deterministic.
        ws.send(Message::Text(
            json!({"type":"msg","targetId":target,"message":"feedback-4"})
                .to_string()
                .into(),
        ))
        .await
        .unwrap();
        let event = time::timeout(Duration::from_secs(1), events.recv())
            .await
            .unwrap()
            .unwrap();
        assert!(matches!(event, Event::Feedback { index: 4, .. }));
        device.stop().await;
    }
}
