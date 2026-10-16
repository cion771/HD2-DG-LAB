//! Loopback-only Axum application and API for HD2 · DG-LAB.

use std::{
    collections::VecDeque,
    net::IpAddr,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Request, State},
    http::{HeaderValue, StatusCode, header},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use hd2_core::{
    config::{AppConfig, WaveEntry},
    events::Event,
    rules::RuleEngine,
    safety::SafetyGate,
    wave_lib::{UNIT_CHARS, WaveLibrary},
    waves::{self, FREQ_MAX, FREQ_MIN, UNIT_MS},
};
use hd2_dglab::{Config as DeviceConfig, DgLabDevice};
use qrcode::{QrCode, render::svg};
use serde_json::{Value, json};
use tokio::sync::{Mutex, Notify, RwLock};

pub const MAX_JSON_BYTES: usize = 1024 * 1024;
const VERSION: &str = env!("CARGO_PKG_VERSION");
const INDEX: &str = include_str!("../../../hd2coyote/frontend/index.html");
const STYLE: &str = include_str!("../../../hd2coyote/frontend/fluent.css");
const SCRIPT: &str = include_str!("../../../hd2coyote/frontend/app.js");
const PRESETS: &[&str] = &[
    "pinch",
    "sting",
    "buzz",
    "ramp_up",
    "breath",
    "heartbeat",
    "death",
];

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    #[error("{0}")]
    BadRequest(String),
    #[error("{0}")]
    Io(#[from] std::io::Error),
    #[error("{0}")]
    Json(#[from] serde_json::Error),
    #[error("{0}")]
    Device(#[from] hd2_dglab::Error),
    #[error("{0}")]
    Http(#[from] reqwest::Error),
}
impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let status = match &self {
            Self::BadRequest(_) | Self::Json(_) => StatusCode::BAD_REQUEST,
            _ => StatusCode::INTERNAL_SERVER_ERROR,
        };
        (status, Json(json!({"ok":false,"error":self.to_string()}))).into_response()
    }
}
type Result<T> = std::result::Result<T, AppError>;

struct Runtime {
    cfg: AppConfig,
    running: bool,
    armed: bool,
    mute_reason: String,
    device: Option<DgLabDevice>,
    output_a: u16,
    output_b: u16,
    hp_pct: Option<f64>,
    limbs: Vec<bool>,
    rules: RuleEngine,
    safety: SafetyGate,
    events: VecDeque<String>,
    update: Value,
    closing: bool,
}
impl Runtime {
    fn new(cfg: AppConfig) -> Self {
        let mut safety = SafetyGate::new(cfg.safety.clone());
        safety.trip("启动后请手动重新武装");
        let update = initial_update(&cfg);
        Self {
            cfg,
            running: false,
            armed: false,
            mute_reason: "启动后请手动重新武装".into(),
            device: None,
            output_a: 0,
            output_b: 0,
            hp_pct: None,
            limbs: vec![false; 6],
            rules: RuleEngine::default(),
            safety,
            events: VecDeque::new(),
            update,
            closing: false,
        }
    }
    fn note(&mut self, text: impl Into<String>) {
        self.events
            .push_back(format!("{:>8}  {}", "--:--:--", text.into()));
        while self.events.len() > 200 {
            self.events.pop_front();
        }
    }
}

#[derive(Clone)]
pub struct AppState {
    inner: Arc<Mutex<Runtime>>,
    config_path: Arc<PathBuf>,
    bridge_dir: Arc<PathBuf>,
    authority: Arc<RwLock<String>>,
    started: Instant,
    shutdown: Arc<Notify>,
    pulse_generation: Arc<AtomicU64>,
}

impl AppState {
    /// Construct state, loading an existing config or creating defaults. The path override is
    /// intended for tests and portable deployments.
    pub async fn new(config_path: Option<PathBuf>) -> Result<Self> {
        let path = config_path.unwrap_or_else(default_config_path);
        let cfg = match tokio::fs::read_to_string(&path).await {
            Ok(text) => AppConfig::from_json_str(&text)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => AppConfig::default(),
            Err(error) => return Err(error.into()),
        };
        let bridge_dir = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(std::env::temp_dir)
            .join("hd2coyote");
        Ok(Self {
            inner: Arc::new(Mutex::new(Runtime::new(cfg))),
            config_path: Arc::new(path),
            bridge_dir: Arc::new(bridge_dir),
            authority: Arc::new(RwLock::new("127.0.0.1:0".into())),
            started: Instant::now(),
            shutdown: Arc::new(Notify::new()),
            pulse_generation: Arc::new(AtomicU64::new(0)),
        })
    }
    pub async fn set_authority(&self, authority: String) {
        *self.authority.write().await = authority;
    }
    pub fn shutdown_notifier(&self) -> Arc<Notify> {
        self.shutdown.clone()
    }
    pub async fn config_snapshot(&self) -> AppConfig {
        self.inner.lock().await.cfg.clone()
    }
    pub async fn waves_snapshot(&self) -> Value {
        let runtime = self.inner.lock().await;
        waves_value(&runtime)
    }
    pub async fn check_update(&self) -> Result<Value> {
        check_update_inner(self).await
    }

    pub async fn safe_shutdown(&self) {
        self.pulse_generation.fetch_add(1, Ordering::SeqCst);
        let device = {
            let mut rt = self.inner.lock().await;
            rt.closing = true;
            rt.running = false;
            rt.armed = false;
            rt.safety.trip("程序关闭");
            rt.output_a = 0;
            rt.output_b = 0;
            rt.device.take()
        };
        if let Some(device) = device {
            device.mute().await;
            device.stop().await;
        }
    }

    async fn mute(&self, reason: &str, stop_device: bool) {
        self.pulse_generation.fetch_add(1, Ordering::SeqCst);
        let device = {
            let mut rt = self.inner.lock().await;
            rt.armed = false;
            rt.safety.trip(reason);
            rt.mute_reason = reason.into();
            rt.output_a = 0;
            rt.output_b = 0;
            if stop_device {
                rt.running = false;
                rt.device.take()
            } else {
                rt.device.clone()
            }
        };
        if let Some(device) = device {
            device.mute().await;
            if stop_device {
                device.stop().await;
            }
        }
    }
}

pub fn default_config_path() -> PathBuf {
    std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir)
        .join("hd2-DG-LAB")
        .join("config.json")
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/", get(index))
        .route("/index.html", get(index))
        .route("/qr.svg", get(qr))
        .route("/api/status", get(status))
        .route("/api/config", get(config_get).post(config_post))
        .route("/api/sources", get(sources))
        .route("/api/waves", get(waves_get).post(waves_post))
        .route("/api/update", get(update_get).post(update_post))
        .route("/api/bridge", post(bridge_post))
        .route("/api/actions", post(actions_post))
        .route("/api/event", post(event_post))
        .layer(DefaultBodyLimit::max(MAX_JSON_BYTES))
        .layer(middleware::from_fn_with_state(state.clone(), secure))
        .with_state(state)
}

async fn secure(State(state): State<AppState>, req: Request, next: Next) -> Response {
    let forbidden = || {
        (
            StatusCode::FORBIDDEN,
            Json(json!({"ok":false,"error":"只允许受信任的本机 / 同源请求"})),
        )
            .into_response()
    };
    if req
        .headers()
        .get("sec-fetch-site")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.eq_ignore_ascii_case("cross-site"))
    {
        return forbidden();
    }
    let expected = state.authority.read().await.clone();
    let Some(host) = req
        .headers()
        .get(header::HOST)
        .and_then(|v| v.to_str().ok())
    else {
        return forbidden();
    };
    if !valid_authority(host, &expected) {
        return forbidden();
    }
    if let Some(origin) = req
        .headers()
        .get(header::ORIGIN)
        .and_then(|v| v.to_str().ok())
    {
        if origin != format!("http://{host}") {
            return forbidden();
        }
    }
    if req.method() != http::Method::GET
        && req.method() != http::Method::HEAD
        && req
            .headers()
            .get(header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .map(|v| v.split(';').next().unwrap_or("").trim())
            != Some("application/json")
    {
        return forbidden();
    }
    let mut response = next.run(req).await;
    let h = response.headers_mut();
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    h.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    h.insert("x-frame-options", HeaderValue::from_static("DENY"));
    h.insert("referrer-policy", HeaderValue::from_static("no-referrer"));
    h.insert(
        "content-security-policy",
        HeaderValue::from_static(
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
        ),
    );
    response
}
fn valid_authority(host: &str, expected: &str) -> bool {
    let parse = |s: &str| s.parse::<http::uri::Authority>().ok();
    let (Some(got), Some(want)) = (parse(host), parse(expected)) else {
        return false;
    };
    if got.port_u16() != want.port_u16() {
        return false;
    }
    let h = got.host().trim_matches(['[', ']']);
    matches!(h, "localhost" | "127.0.0.1" | "::1")
        || h.parse::<IpAddr>().is_ok_and(|ip| ip.is_loopback())
}

async fn index() -> impl IntoResponse {
    (
        [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
        INDEX
            .replace("/*__STYLE__*/", STYLE)
            .replace("/*__SCRIPT__*/", SCRIPT),
    )
}
async fn qr(State(state): State<AppState>) -> Result<Response> {
    let url = {
        let rt = state.inner.lock().await;
        match &rt.device {
            Some(d) => d.snapshot().await.qr_url,
            None => String::new(),
        }
    };
    if url.is_empty() {
        return Ok((
            StatusCode::NOT_FOUND,
            Json(json!({"error":"二维码不可用（设备未启动）"})),
        )
            .into_response());
    }
    let code = QrCode::new(url.as_bytes()).map_err(|e| AppError::BadRequest(e.to_string()))?;
    Ok((
        [(header::CONTENT_TYPE, "image/svg+xml")],
        code.render::<svg::Color>().min_dimensions(256, 256).build(),
    )
        .into_response())
}

async fn status(State(state): State<AppState>) -> Json<Value> {
    let mut rt = state.inner.lock().await;
    let device_snapshot = match &rt.device {
        Some(d) => Some(d.snapshot().await),
        None => None,
    };
    let connected = device_snapshot.as_ref().is_some_and(|s| s.connected)
        || rt.cfg.device.kind.eq_ignore_ascii_case("mock");
    let limits = device_snapshot
        .as_ref()
        .map(|s| (s.limit_a, s.limit_b))
        .unwrap_or((200, 200));
    let qr_url = device_snapshot
        .as_ref()
        .map(|s| s.qr_url.clone())
        .unwrap_or_default();
    rt.armed = rt.safety.armed();
    let startup = json!({"ready_to_arm":rt.running&&connected,"armed":rt.armed,"steps":[
        {"id":"safety","label":"安全边界","ok":true,"detail":"安全上限已加载"},
        {"id":"device","label":"设备服务","ok":connected,"detail":if connected{"设备服务已连接"}else{"设备尚未连接"}},
        {"id":"detecting","label":"检测服务","ok":rt.running,"detail":if rt.running{"检测线程正在运行"}else{"尚未开始检测"}},
        {"id":"source","label":"事件数据","ok":rt.running,"detail":if rt.running{"事件入口已就绪"}else{"尚未开始检测"}}],"note":"检查向导不会自动武装输出；确认无误后仍需由你手动重新武装。"});
    Json(
        json!({"controller":{"version":VERSION,"uptime_s":state.started.elapsed().as_secs_f64(),"running":rt.running,"detecting":rt.running,"source":rt.cfg.source,"hp":rt.hp_pct.map(|x|x/100.0),"hp_pct":rt.hp_pct,"limbs":rt.limbs,"output":{"a":rt.output_a,"b":rt.output_b,"pct":rt.output_a.max(rt.output_b) as f64 / 2.0},"armed":rt.armed,"mute_reason":rt.mute_reason,"session_seconds":rt.safety.session_seconds(),"device":{"kind":rt.cfg.device.kind,"connected":connected,"limit_a":limits.0,"limit_b":limits.1,"qr_url":qr_url},"hook":{"alive":false,"connected":false,"listening":rt.running&&rt.cfg.sources.enabled.iter().any(|s|s=="game_bridge"),"mode":"","detail":"Rust 本地 API 已就绪；等待游戏桥遥测。","version":"","build":"","profile":"","packets":0},"last_error":"","sources":rt.cfg.sources.enabled.iter().map(|name|json!({"name":name,"label":source_label(name),"alive":rt.running})).collect::<Vec<_>>(),"ramp":{"enabled":rt.cfg.ramp.enabled,"pct":0.0,"detail":"-","ceiling_pct":rt.cfg.ramp.ceiling_pct}},"events":rt.events,"bridge":bridge_info(&state.bridge_dir),"startup":startup,"update":rt.update}),
    )
}
async fn config_get(State(state): State<AppState>) -> Json<AppConfig> {
    Json(state.inner.lock().await.cfg.clone())
}

fn merge(dst: &mut Value, patch: &Value) {
    if let (Some(d), Some(p)) = (dst.as_object_mut(), patch.as_object()) {
        for (k, v) in p {
            if let Some(old) = d.get_mut(k) {
                merge(old, v)
            } else {
                d.insert(k.clone(), v.clone());
            }
        }
    } else {
        *dst = patch.clone();
    }
}
fn validate(c: &AppConfig) -> Result<()> {
    if !(0.0..=100.0).contains(&c.safety.max_pct) {
        return Err(AppError::BadRequest("safety.max_pct 必须在 0..100".into()));
    }
    if !(0..=200).contains(&c.safety.max_absolute) {
        return Err(AppError::BadRequest(
            "safety.max_absolute 必须在 0..200".into(),
        ));
    }
    for (n, v) in [
        ("master_multiplier", c.safety.master_multiplier),
        ("channel_a_multiplier", c.safety.channel_a_multiplier),
        ("channel_b_multiplier", c.safety.channel_b_multiplier),
    ] {
        if !(0.0..=2.0).contains(&v) {
            return Err(AppError::BadRequest(format!("safety.{n} 必须在 0..2")));
        }
    }
    if c.device.port < 1024 {
        return Err(AppError::BadRequest(
            "device.port 必须在 1024..65535".into(),
        ));
    }
    if c.sources.enabled.is_empty() {
        return Err(AppError::BadRequest("至少启用一个事件源".into()));
    }
    Ok(())
}
async fn save_atomic(path: &Path, cfg: &AppConfig) -> Result<()> {
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await?
    }
    let temp = path.with_extension(format!("json.tmp.{}", std::process::id()));
    let data = serde_json::to_vec_pretty(cfg)?;
    tokio::fs::write(&temp, data).await?;
    tokio::fs::OpenOptions::new()
        .write(true)
        .open(&temp)
        .await?
        .sync_all()
        .await?;
    if tokio::fs::rename(&temp, path).await.is_err() {
        let _ = tokio::fs::remove_file(path).await;
        tokio::fs::rename(&temp, path).await?
    }
    Ok(())
}
async fn config_post(
    State(state): State<AppState>,
    Json(patch): Json<Value>,
) -> Result<Json<Value>> {
    if !patch.is_object() {
        return Err(AppError::BadRequest("请求体必须是 JSON 对象".into()));
    }
    let old = { state.inner.lock().await.cfg.clone() };
    let mut value = serde_json::to_value(&old)?;
    merge(&mut value, &patch);
    value["source"] = json!("hook");
    let cfg: AppConfig = serde_json::from_value(value)?;
    validate(&cfg)?;
    let restart = cfg.device != old.device || cfg.sources != old.sources;
    if restart {
        state.mute("配置已更改，请手动重新启动并武装", true).await
    } else {
        state.mute("配置已更改，请手动重新武装", false).await
    }
    save_atomic(&state.config_path, &cfg).await?;
    {
        let mut rt = state.inner.lock().await;
        rt.safety.set_config(cfg.safety.clone());
        rt.cfg = cfg;
        rt.note("配置已保存");
    }
    Ok(Json(
        json!({"ok":true,"applied":"配置已保存","restart_required":restart}),
    ))
}

fn source_label(name: &str) -> &str {
    match name {
        "game_bridge" => "游戏内桥",
        "http" => "HTTP 上报",
        "udp" => "UDP 上报",
        _ => "事件源",
    }
}
async fn sources(State(state): State<AppState>) -> Json<Value> {
    let rt = state.inner.lock().await;
    let runtime = rt
        .cfg
        .sources
        .enabled
        .iter()
        .map(|name| json!({"name":name,"label":source_label(name),"running":rt.running}))
        .collect::<Vec<_>>();
    Json(json!({
        "ok":true,
        "engine_source":rt.cfg.source,
        "catalog":[{"name":"game_bridge","label":"游戏内桥","hint":"接收本机 Lua 桥遥测"},{"name":"http","label":"HTTP 上报","hint":"由外部工具上报事件"},{"name":"udp","label":"UDP 上报","hint":"兼容 UDP 游戏桥"}],
        "enabled":rt.cfg.sources.enabled,
        "running":rt.running,
        "runtime":runtime,
        "http":{"host":rt.cfg.sources.http_host,"port":rt.cfg.sources.http_port,"url":format!("http://{}:{}/event",rt.cfg.sources.http_host,rt.cfg.sources.http_port),"token_required":!rt.cfg.sources.http_token.is_empty(),"max_per_s":rt.cfg.sources.http_max_per_s,"examples":{"damage":{"ev":"damage","severity":20},"death":{"ev":"death"},"state":{"ev":"state","hp":80,"hp_max":100,"limbs":[0,0,0]}}}
    }))
}

fn waves_value(rt: &Runtime) -> Value {
    let lib = WaveLibrary::new(&rt.cfg.waves);
    let rows=lib.entries.iter().map(|(name,e)|json!({"name":name,"kind":if e.units.is_empty(){"preset"}else{"units"},"preset":e.preset,"units":e.units,"unit_count":e.units.len(),"freq":e.freq,"peak":e.peak,"default_ms":e.default_ms,"note":e.note})).collect::<Vec<_>>();
    json!({"ok":true,"presets":PRESETS,"waves":rows,"rules":rt.cfg.rules.iter().map(|(k,v)|(k.clone(),Value::String(v.wave.clone()))).collect::<serde_json::Map<_,_>>(),"text":serde_json::to_string_pretty(&lib.export_value()).unwrap_or_default(),"freq_min":FREQ_MIN,"freq_max":FREQ_MAX,"unit_ms":UNIT_MS,"unit_chars":UNIT_CHARS})
}
async fn waves_get(State(state): State<AppState>) -> Json<Value> {
    let runtime = state.inner.lock().await;
    Json(waves_value(&runtime))
}
async fn waves_post(State(state): State<AppState>, Json(body): Json<Value>) -> Result<Json<Value>> {
    let action = body.get("action").and_then(Value::as_str).unwrap_or("");
    if action == "test" {
        let name = required_str(&body, "name")?;
        let pct = number(&body, "pct", 5.0)?;
        let ms = number(&body, "ms", 700.0)?;
        let channel = body
            .get("channel")
            .and_then(Value::as_str)
            .unwrap_or("both");
        pulse(&state, Some(name), pct, ms, channel).await?;
        let rt = state.inner.lock().await;
        return Ok(Json(with_fields(
            waves_value(&rt),
            json!({"detail":"测试波形已发送"}),
        )));
    }
    let mut rt = state.inner.lock().await;
    let mut lib = WaveLibrary::new(&rt.cfg.waves);
    let (applied, import_result) = match action {
        "set" | "preset" => {
            let name = required_str(&body, "name")?;
            let units = if action == "preset" {
                Vec::new()
            } else {
                body.get("units")
                    .and_then(Value::as_array)
                    .map(|a| {
                        a.iter()
                            .filter_map(Value::as_str)
                            .map(str::to_owned)
                            .collect()
                    })
                    .unwrap_or_default()
            };
            let entry = WaveEntry {
                units,
                preset: body
                    .get("preset")
                    .and_then(Value::as_str)
                    .unwrap_or(if action == "preset" { "pinch" } else { "" })
                    .into(),
                freq: body.get("freq").and_then(Value::as_f64),
                peak: number(&body, "peak", 100.0)?,
                default_ms: number(&body, "default_ms", 0.0)?,
                note: body
                    .get("note")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .into(),
            };
            lib.set_entry(name, entry).map_err(AppError::BadRequest)?;
            (format!("已保存波形「{name}」"), None)
        }
        "remove" => {
            let name = required_str(&body, "name")?;
            if !lib.remove(name) {
                return Err(AppError::BadRequest("波形不存在".into()));
            }
            (format!("已删除波形「{name}」"), None)
        }
        "import" | "replace" => {
            let value = if let Some(data) = body.get("data").filter(|v| !v.is_null()) {
                data.clone()
            } else {
                serde_json::from_str(required_str(&body, "text")?)?
            };
            let replace = action == "replace"
                || body
                    .get("replace")
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
            let n = lib
                .import_value(&value, replace)
                .map_err(AppError::BadRequest)?;
            (
                format!("已导入 {n} 个波形"),
                Some(json!({"count":n,"replace":replace})),
            )
        }
        "export" => {
            return Ok(Json(waves_value(&rt)));
        }
        _ => return Err(AppError::BadRequest("未知波形操作".into())),
    };
    rt.cfg.waves = lib.to_config();
    let cfg = rt.cfg.clone();
    let result = with_fields(
        waves_value(&rt),
        json!({"applied":applied,"import":import_result}),
    );
    drop(rt);
    save_atomic(&state.config_path, &cfg).await?;
    Ok(Json(result))
}

async fn actions_post(
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> Result<Json<Value>> {
    let action = required_str(&body, "action")?;
    match action {
        "device_start" => {
            start_device(&state).await?;
            Ok(Json(
                json!({"ok":true,"detail":"设备服务已启动；输出保持未武装"}),
            ))
        }
        "start" => {
            let mut rt = state.inner.lock().await;
            rt.running = true;
            rt.note("检测已启动（保持未武装）");
            Ok(Json(
                json!({"ok":true,"detail":"检测已启动；请确认状态后手动重新武装"}),
            ))
        }
        "arm" => {
            let mut rt = state.inner.lock().await;
            if !rt.running {
                return Err(AppError::BadRequest("请先开始检测".into()));
            }
            let connected = rt.cfg.device.kind == "mock"
                || match &rt.device {
                    Some(d) => d.snapshot().await.connected,
                    None => false,
                };
            if !connected {
                return Err(AppError::BadRequest("设备尚未连接".into()));
            }
            rt.safety.arm();
            rt.armed = true;
            rt.mute_reason.clear();
            rt.note("已手动武装");
            Ok(Json(json!({"ok":true,"detail":"已重新武装"})))
        }
        "trip" => {
            state.mute("用户急停", false).await;
            Ok(Json(json!({"ok":true,"detail":"已急停并清零输出"})))
        }
        "stop" => {
            state.mute("检测已停止", true).await;
            Ok(Json(json!({"ok":true,"detail":"检测、设备与输出已停止"})))
        }
        "test_pulse" => {
            pulse(
                &state,
                None,
                number(&body, "pct", 5.0)?,
                number(&body, "ms", 700.0)?,
                body.get("channel")
                    .and_then(Value::as_str)
                    .unwrap_or("both"),
            )
            .await?;
            Ok(Json(json!({"ok":true,"detail":"测试脉冲已发送"})))
        }
        "shutdown" => {
            state.safe_shutdown().await;
            state.shutdown.notify_waiters();
            Ok(Json(
                json!({"ok":true,"detail":"输出已清零，控制服务正在关闭"}),
            ))
        }
        _ => Err(AppError::BadRequest("未知 action".into())),
    }
}
async fn start_device(state: &AppState) -> Result<()> {
    let (cfg, existing) = {
        let rt = state.inner.lock().await;
        (rt.cfg.device.clone(), rt.device.clone())
    };
    if existing.is_some() || cfg.kind.eq_ignore_ascii_case("mock") {
        return Ok(());
    }
    let d = DgLabDevice::bind(DeviceConfig {
        host: cfg.host,
        port: cfg.port,
        advertise_ip: (!cfg.advertise_ip.trim().is_empty()).then_some(cfg.advertise_ip),
    })
    .await?;
    d.mute().await;
    let mut rt = state.inner.lock().await;
    rt.device = Some(d);
    rt.armed = false;
    rt.safety.trip("设备服务启动后保持未武装");
    rt.mute_reason = "设备服务启动后保持未武装".into();
    Ok(())
}
async fn pulse(
    state: &AppState,
    name: Option<&str>,
    pct: f64,
    ms: f64,
    channel: &str,
) -> Result<()> {
    let (use_a, use_b) = match channel.trim().to_ascii_uppercase().as_str() {
        "A" => (true, false),
        "B" => (false, true),
        "" | "BOTH" | "DEFAULT" => (true, true),
        _ => {
            return Err(AppError::BadRequest(
                "channel 必须是 A、B、both 或 default".into(),
            ));
        }
    };
    if !(0.1..=100.0).contains(&pct) || !(100.0..=5000.0).contains(&ms) {
        return Err(AppError::BadRequest(
            "测试范围必须是 0.1..100% / 100..5000ms".into(),
        ));
    }
    let (device, units, a, b, generation) = {
        let mut rt = state.inner.lock().await;
        if !rt.running || !rt.safety.armed() {
            return Err(AppError::BadRequest("请先开始检测并重新武装".into()));
        }
        let units = if let Some(name) = name {
            WaveLibrary::new(&rt.cfg.waves)
                .build(name, ms, 100.0, None)
                .ok_or_else(|| AppError::BadRequest("波形不存在".into()))?
        } else {
            waves::build("pinch", ms, 100.0, None).map_err(AppError::BadRequest)?
        };
        let a = if use_a {
            rt.safety.strength(pct, 'A', 200)
        } else {
            0
        };
        let b = if use_b {
            rt.safety.strength(pct, 'B', 200)
        } else {
            0
        };
        rt.output_a = a;
        rt.output_b = b;
        rt.note(format!("测试输出 {pct:.1}% / {ms:.0}ms"));
        (
            rt.device.clone(),
            units,
            a,
            b,
            state.pulse_generation.fetch_add(1, Ordering::SeqCst) + 1,
        )
    };
    if let Some(d) = device.clone() {
        d.clear("A").await?;
        d.clear("B").await?;
        if use_a {
            d.send_wave("A", &units).await?;
            d.set_strength("A", a, 2).await?;
        }
        if use_b {
            d.send_wave("B", &units).await?;
            d.set_strength("B", b, 2).await?;
        }
    }
    let s = state.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(ms as u64)).await;
        if s.pulse_generation.load(Ordering::SeqCst) == generation {
            if let Some(d) = device {
                d.mute().await
            }
            let mut rt = s.inner.lock().await;
            rt.output_a = 0;
            rt.output_b = 0;
        }
    });
    Ok(())
}

fn parse_event(v: &Value, now: f64) -> Result<Option<Event>> {
    let ev = v
        .get("ev")
        .or_else(|| v.get("kind"))
        .and_then(Value::as_str)
        .unwrap_or("");
    Ok(match ev {
        "damage" => {
            let severity = number(v, "severity", 0.0)?;
            Some(Event::Damage {
                t: now,
                severity,
                hp_before: 1.0,
                hp_after: (1.0 - severity / 100.0).max(0.0),
            })
        }
        "limb_injury" => Some(Event::LimbInjury {
            t: now,
            slot: v.get("slot").and_then(Value::as_u64).unwrap_or(0) as usize,
            bleeding: v.get("bleeding").and_then(Value::as_bool).unwrap_or(false),
            name: v.get("name").and_then(Value::as_str).unwrap_or("").into(),
        }),
        "death" => Some(Event::Death { t: now }),
        "revive" => Some(Event::Revive { t: now }),
        "low_health" => Some(Event::LowHealth {
            t: now,
            ratio: number(v, "ratio", 0.0)?,
        }),
        "state" => None,
        _ => return Err(AppError::BadRequest("未知事件类型".into())),
    })
}
async fn event_post(State(state): State<AppState>, Json(body): Json<Value>) -> Result<Json<Value>> {
    let list = body.as_array().cloned().unwrap_or_else(|| vec![body]);
    if list.len() > 1000 {
        return Err(AppError::BadRequest("一次最多注入 1000 个事件".into()));
    }
    for v in list {
        let now = state.started.elapsed().as_secs_f64();
        if v.get("ev").and_then(Value::as_str) == Some("state") {
            let mut rt = state.inner.lock().await;
            let hp = number(&v, "hp", 0.0)?;
            let max = number(&v, "hp_max", 100.0)?.max(1.0);
            rt.hp_pct = Some((hp / max * 100.0).clamp(0.0, 100.0));
            if let Some(a) = v.get("limbs").and_then(Value::as_array) {
                rt.limbs = a.iter().map(|x| x.as_f64().unwrap_or(0.0) > 0.0).collect()
            }
            rt.note("注入状态包");
            continue;
        }
        if let Some(event) = parse_event(&v, now)? {
            let actions = {
                let mut rt = state.inner.lock().await;
                rt.note(format!("注入事件 {}", event.kind()));
                let cfg = rt.cfg.clone();
                rt.rules.actions(&cfg, &event, now)
            };
            for action in actions {
                if state.inner.lock().await.armed {
                    pulse(
                        &state,
                        None,
                        action.pct.clamp(0.1, 100.0),
                        action.duration_ms.clamp(100.0, 5000.0),
                        &action.channel,
                    )
                    .await?
                }
            }
        }
    }
    Ok(Json(json!({"ok":true,"detail":"事件已注入"})))
}

fn bridge_info(dir: &Path) -> Value {
    let path = dir.join("bridge_config.lua");
    let text = std::fs::read_to_string(&path).unwrap_or_default();
    let get = |key: &str, default: &str| {
        text.lines()
            .find_map(|l| {
                l.trim()
                    .strip_prefix(&format!("{key} = "))
                    .map(|s| s.trim_matches([',', '\'', ' ']).to_string())
            })
            .unwrap_or_else(|| default.into())
    };
    json!({"dir":dir,"config_path":path,"exists":path.exists(),"text":text,"parsed":{"mode":get("mode","safe"),"port":get("port","47777").parse::<u16>().unwrap_or(47777),"interval":get("interval","0.1").parse::<f64>().unwrap_or(0.1),"profile":get("profile","steam_25480438")},"status":"","log_tail":[],"loader_line":"","loader_log":"","shared_log_tail":[],"recon_report":[]})
}
async fn bridge_post(
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> Result<Json<Value>> {
    let mode = required_str(&body, "mode")?;
    if !["safe", "net", "menu", "recon", "live"].contains(&mode) {
        return Err(AppError::BadRequest("非法 mode".into()));
    }
    let port = number(&body, "port", 47777.0)? as u16;
    if port < 1024 {
        return Err(AppError::BadRequest("port 必须在 1024..65535".into()));
    }
    let interval = number(&body, "interval", 0.1)?;
    if !(0.02..=5.0).contains(&interval) {
        return Err(AppError::BadRequest("interval 必须在 0.02..5.0 秒".into()));
    }
    let profile = required_str(&body, "profile")?;
    if !profile
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '_')
    {
        return Err(AppError::BadRequest(
            "profile 只能包含字母/数字/下划线".into(),
        ));
    }
    tokio::fs::create_dir_all(&*state.bridge_dir).await?;
    let path = state.bridge_dir.join("bridge_config.lua");
    if path.exists() {
        let _ = tokio::fs::copy(&path, path.with_extension("lua.bak")).await;
    }
    let text = format!(
        "-- 由 hd2-coyote Rust 桌面端生成（改完重启游戏生效）\nreturn {{\n  config = {{\n    mode = '{mode}',\n    port = {port},\n    interval = {interval},\n    profile = '{profile}',\n  }},\n}}\n"
    );
    tokio::fs::write(&path, text).await?;
    Ok(Json(
        json!({"ok":true,"path":path,"note":"已写入桥配置；请重启游戏后检查实际运行状态"}),
    ))
}

async fn update_get(State(state): State<AppState>) -> Json<Value> {
    Json(state.inner.lock().await.update.clone())
}
async fn update_post(
    State(state): State<AppState>,
    Json(body): Json<Value>,
) -> Result<Json<Value>> {
    if body.get("clear").and_then(Value::as_bool).unwrap_or(false) {
        let mut rt = state.inner.lock().await;
        let value = initial_update(&rt.cfg);
        rt.update = value.clone();
        return Ok(Json(value));
    }
    Ok(Json(check_update_inner(&state).await?))
}
async fn check_update_inner(state: &AppState) -> Result<Value> {
    let (enabled, repo, timeout_s) = {
        let rt = state.inner.lock().await;
        (
            rt.cfg.update.enabled,
            rt.cfg.update.repo.clone(),
            rt.cfg.update.timeout_s,
        )
    };
    if !enabled {
        return Err(AppError::BadRequest("更新检查已禁用".into()));
    }
    if !repo
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || "-_/".contains(c))
    {
        return Err(AppError::BadRequest("更新仓库名称无效".into()));
    }
    let url = format!("https://api.github.com/repos/{repo}/releases/latest");
    let response = reqwest::Client::builder()
        .timeout(Duration::from_secs_f64(timeout_s.clamp(1.0, 30.0)))
        .build()?
        .get(url)
        .header("User-Agent", format!("hd2-app/{VERSION}"))
        .send()
        .await?
        .error_for_status()?;
    let v: Value = response.json().await?;
    let tag = v.get("tag_name").and_then(Value::as_str).unwrap_or("");
    let clean = tag.trim_start_matches('v');
    let result = json!({"ok":true,"current":VERSION,"repo":repo,"latest":{"tag":tag,"url":v.get("html_url")},"update_available":!clean.is_empty()&&clean!=VERSION});
    state.inner.lock().await.update = result.clone();
    Ok(result)
}
fn initial_update(cfg: &AppConfig) -> Value {
    json!({
        "ok": Value::Null,
        "current": VERSION,
        "repo": cfg.update.repo,
        "enabled": cfg.update.enabled,
        "error": "还没检查过（点「检查更新」）"
    })
}

fn with_fields(mut base: Value, extra: Value) -> Value {
    if let (Some(base), Some(extra)) = (base.as_object_mut(), extra.as_object()) {
        base.extend(extra.clone());
    }
    base
}

fn required_str<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| AppError::BadRequest(format!("缺少 {key}")))
}
fn number(v: &Value, key: &str, default: f64) -> Result<f64> {
    let n = v
        .get(key)
        .and_then(|x| x.as_f64().or_else(|| x.as_str()?.parse().ok()))
        .unwrap_or(default);
    if n.is_finite() {
        Ok(n)
    } else {
        Err(AppError::BadRequest(format!("{key} 必须是有限数字")))
    }
}

#[cfg(test)]
mod tests;
