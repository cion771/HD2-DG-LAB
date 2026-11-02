//! 外部 HUD 视觉源：只截游戏窗口客户区，从血条像素读出玩家血量。
//!
//! # 为什么存在
//!
//! 游戏内桥（`hd2-source-udp` / Lua 打补丁）依赖往游戏里注入 Lua，游戏更新
//! 或反作弊策略一变就断。这个源改成"看画面"：独立进程、Win32 公开 API、
//! 不碰游戏进程，因此不受注入面变化影响，代价是需要先把血条区域校准一次。
//!
//! # 安全边界
//!
//! * 只调窗口枚举/几何与 `BitBlt`；**不** `OpenProcess`、**不**
//!   `ReadProcessMemory`、不写游戏内存、不注入模块。
//! * 只抓游戏窗口客户区，不抓整屏。
//! * 窗口不在前台时立刻作废读数，宁可停输出也不读错画面。
//! * 读到的血量照样要过 `RuleEngine` → `SafetyGate`：本 crate 只提供观测，
//!   没有任何直通输出的路径。软件限幅不能保证人体安全，别把它当保护装置。
//!
//! # 与 Python 桌面版的关系
//!
//! 采样循环、状态机、错误文案逐条对齐 `hd2coyote/sources/vision.py`，两边
//! 可以互换配置、对照日志。差异只有两处：抓屏后端固定为 GDI（不需要 mss /
//! Pillow），以及断流重建基线改为通过 [`SourceItem::Resync`] 通知引擎，因为
//! Rust 侧 `EventTrackers` 由引擎独占。

#![deny(missing_docs)]

mod analyze;
mod frame;
mod preview;
mod titles;

#[cfg(windows)]
mod platform;
#[cfg(not(windows))]
#[path = "platform_stub.rs"]
mod platform;

use std::sync::{Arc, Condvar, Mutex, MutexGuard, RwLock, RwLockReadGuard};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use hd2_controller::{AdapterError, AdapterResult, Source, SourceItem, StateFrame};
use hd2_core::config::{AppConfig, ScreenRect, VisionConfig};

pub use analyze::{
    AUTO_LOCATE_MAX_THICKNESS, AUTO_LOCATE_MIN_RUN, AUTO_LOCATE_Y_START, BAR_MIN_PEAK,
    BAR_MIN_THRESHOLD, BAR_RELATIVE_THRESHOLD, BLACK_FRAME_MEAN, DEFAULT_BRIGHTNESS,
    auto_locate_hp_bar, bar_fill_ratio, crop, is_black_frame,
};
pub use frame::Frame;
pub use preview::data_url as preview_data_url;
pub use preview::{PREVIEW_MAX_HEIGHT, PREVIEW_MIN_HEIGHT, PREVIEW_WIDTH};
pub use titles::{DEFAULT_TITLES, title_candidates, window_title_matches};

/// 源名，与配置 `sources.enabled` 里的字符串一致。
pub const NAME: &str = "vision";
/// 界面显示名。
pub const LABEL: &str = "外部 HUD 视觉（EAC 兼容）";
/// 启动提示。
pub const HINT: &str = "独立进程只截取游戏窗口客户区。需先在任务中校准血条；选择 vision 时取消勾选 game_bridge，避免旧桥断流使输出持续静音。";

/// 启动等待上限，与 Python 桌面版一致。
pub const START_TIMEOUT: Duration = Duration::from_secs(4);
/// 客户区小于它就认为窗口最小化或不可用。
pub const MIN_CLIENT_WIDTH: i32 = 320;
/// 客户区小于它就认为窗口最小化或不可用。
pub const MIN_CLIENT_HEIGHT: i32 = 240;

/// 视觉源的失败原因。
///
/// 每条文案都直接来自采样循环，会原样进入 `note` 和界面，所以写成给玩家看
/// 的句子：说清楚"卡在哪一步"以及"该做什么"。
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum VisionError {
    /// 非 Windows 平台。
    #[error("视觉源仅支持 Windows（需要 Win32 抓屏）")]
    Unsupported,
    /// 没有找到游戏窗口。
    #[error("没有找到游戏窗口（标题候选：{0}）；请先启动游戏")]
    WindowMissing(String),
    /// 窗口存在但不在前台。
    #[error("游戏不在前台，暂不读取（避免误读其它窗口）")]
    NotForeground,
    /// 窗口太小或已最小化。
    #[error("游戏窗口已最小化或尺寸过小")]
    WindowTooSmall,
    /// 客户区全黑。
    #[error("窗口客户区全黑；请改用无边框窗口模式")]
    BlackFrame,
    /// 客户区全黑（已限定血条区域时）。
    #[error("血条区域全黑")]
    BlackBarRegion,
    /// 没有定位到血条。
    #[error("尚未定位到血条；需在任务中、HUD 可见")]
    BarNotLocated,
    /// 已保存的血条区域超出当前窗口。
    #[error("已保存的血条区域超出窗口；请重新校准")]
    RectOutOfBounds,
    /// 读数为空。
    #[error("血条读数无效")]
    InvalidReading,
    /// 窗口客户区读不出来。
    #[error("读不出窗口客户区")]
    ClientRect,
    /// 抓屏失败。
    #[error("抓屏失败：{0}")]
    Capture(String),
    /// 采样线程创建失败。
    #[error("无法创建采样线程：{0}")]
    Spawn(String),
    /// 启动超时。
    #[error("视觉源启动超时")]
    StartupTimeout,
}

/// 最近一次采样结果，给界面与 `/api/status` 用。
#[derive(Debug, Clone, PartialEq)]
pub struct VisionSnapshot {
    /// 血量比例 `0.0..=1.0`；读数过期时为 `None`。
    pub hp: Option<f64>,
    /// 当前使用的血条区域。
    pub rect: Option<ScreenRect>,
    /// 读数是否新鲜（未过期）。
    pub alive: bool,
    /// 连续抓屏失败次数。
    pub capture_errors: u64,
    /// 最近一条说明（"前台"或失败原因）。
    pub note: String,
    /// 距上一次成功采样的秒数。
    pub age_s: Option<f64>,
    /// 启动失败信息；正常启动时为空。
    pub startup_error: String,
}

#[derive(Debug, Clone)]
struct Job {
    vision: VisionConfig,
    titles: String,
}

#[derive(Debug, Default)]
struct Sample {
    hp: Option<f64>,
    rect: Option<ScreenRect>,
    valid: bool,
    last_at: Option<Instant>,
    capture_errors: u64,
    note: String,
    seq: u64,
}

/// 采样线程的停止信号。
type Control = Arc<(Mutex<bool>, Condvar)>;

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poison| poison.into_inner())
}

fn read<T>(lock: &RwLock<T>) -> RwLockReadGuard<'_, T> {
    lock.read().unwrap_or_else(|poison| poison.into_inner())
}

fn write<T>(lock: &RwLock<T>) -> std::sync::RwLockWriteGuard<'_, T> {
    lock.write().unwrap_or_else(|poison| poison.into_inner())
}

/// 外部 HUD 视觉源。
///
/// 采样在独立线程里按 `vision.fps` 跑；[`Source::poll`] 只读最新结果，因此
/// 引擎的 20ms 心跳不会被抓屏阻塞。
pub struct VisionSource {
    job: Arc<RwLock<Job>>,
    shared: Arc<Mutex<Sample>>,
    control: Control,
    worker: Option<JoinHandle<()>>,
    started: bool,
    startup_error: String,
    death_threshold: Option<f64>,
    injury_names: Vec<String>,
    /// 已消费到的采样序号，用于避免重复喂入同一帧。
    polled_seq: u64,
    /// 断流后是否还需要重建基线。
    need_baseline: bool,
    /// 上一次 poll 时读数是否新鲜。
    had_live: bool,
}

impl VisionSource {
    /// 按配置构造。构造不启动线程，也不碰 Win32。
    pub fn new(cfg: &AppConfig) -> Self {
        platform::init();
        Self {
            job: Arc::new(RwLock::new(Job {
                vision: cfg.vision.clone(),
                titles: cfg.safety.game_window_title.clone(),
            })),
            shared: Arc::new(Mutex::new(Sample::default())),
            control: Arc::new((Mutex::new(false), Condvar::new())),
            worker: None,
            started: false,
            startup_error: String::new(),
            death_threshold: Some(cfg.detect.death_state_threshold),
            injury_names: cfg.hud.injury_slot_names.clone(),
            polled_seq: 0,
            need_baseline: true,
            had_live: false,
        }
    }

    /// 换配置。采样频率、血条区域、标题候选立即生效，不需要重启源。
    pub fn set_config(&mut self, cfg: &AppConfig) {
        {
            let mut job = write(&self.job);
            job.vision = cfg.vision.clone();
            job.titles = cfg.safety.game_window_title.clone();
        }
        self.death_threshold = Some(cfg.detect.death_state_threshold);
        self.injury_names = cfg.hud.injury_slot_names.clone();
    }

    /// 读数是否新鲜。判据：已启动、上一次采样成功、且未超过
    /// `max(0.5, vision.stale_timeout_s)`。
    ///
    /// 刻意不参考引擎传入的 `now`：引擎的时间基准是它自己的 `Instant`，
    /// 与本 crate 不同源，拿来做绝对比较只会误判。
    pub fn is_alive(&self) -> bool {
        if !self.started {
            return false;
        }
        let timeout = self.stale_timeout();
        let shared = lock(&self.shared);
        let Some(last) = shared.last_at else {
            return false;
        };
        shared.valid && last.elapsed().as_secs_f64() <= timeout
    }

    fn stale_timeout(&self) -> f64 {
        let timeout = read(&self.job).vision.stale_timeout_s;
        if timeout.is_finite() {
            timeout.max(0.5)
        } else {
            1.5
        }
    }

    /// 最近一次采样的快照。
    pub fn snapshot(&self) -> VisionSnapshot {
        let timeout = self.stale_timeout();
        let shared = lock(&self.shared);
        let age_s = shared.last_at.map(|at| at.elapsed().as_secs_f64());
        let alive = self.started && shared.valid && age_s.is_some_and(|age| age <= timeout);
        VisionSnapshot {
            hp: if alive { shared.hp } else { None },
            rect: shared.rect,
            alive,
            capture_errors: shared.capture_errors,
            note: shared.note.clone(),
            age_s,
            startup_error: self.startup_error.clone(),
        }
    }

    /// 人读的一行状态，用于日志与 `/api/status` 的 detail 字段。
    pub fn describe(&self) -> String {
        if !self.startup_error.is_empty() {
            return format!("启动失败：{}", self.startup_error);
        }
        if !self.started {
            return "未启动".into();
        }
        let snapshot = self.snapshot();
        let area = match snapshot.rect {
            Some(rect) => format!("血条 {},{} {}x{}", rect.x, rect.y, rect.w, rect.h),
            None => "未定位血条".into(),
        };
        let value = match snapshot.hp {
            Some(hp) => format!("{:.1}%", hp * 100.0),
            None => "--".into(),
        };
        let extra = if snapshot.note.is_empty() {
            String::new()
        } else {
            format!("；{}", snapshot.note)
        };
        format!(
            "{area}；HP={value}；抓屏错误={}{extra}",
            snapshot.capture_errors
        )
    }

    /// 一次性校准：找窗口 → 校验前台 → 抓客户区 → 自动定位血条。
    ///
    /// 供界面"校准血条"按钮调用。成功后调用方应把返回的矩形写进
    /// `config.vision.hp_bar` 并保存，否则下次启动仍要重新校准。
    pub fn calibrate(&self) -> Result<ScreenRect, VisionError> {
        self.calibrate_frame().map(|(_, rect, _)| rect)
    }

    /// 校准抓取：返回整幅客户区帧、候选血条区域和该区域的填充比例。
    ///
    /// 界面用这帧裁出预览图，让玩家确认"这个位置真的是血条"再保存。
    pub fn calibrate_frame(&self) -> Result<(Frame, ScreenRect, f64), VisionError> {
        let job = read(&self.job).clone();
        let candidate_list = title_candidates(&job.titles).join("/");
        let window =
            platform::find_window(&job.titles).ok_or(VisionError::WindowMissing(candidate_list))?;
        let (_, _, width, height) = platform::client_rect(window).ok_or(VisionError::ClientRect)?;
        if width < MIN_CLIENT_WIDTH || height < MIN_CLIENT_HEIGHT {
            return Err(VisionError::WindowTooSmall);
        }
        if !platform::is_foreground(window) {
            return Err(VisionError::NotForeground);
        }
        let frame = platform::capture_client(window).map_err(VisionError::Capture)?;
        if is_black_frame(&frame) {
            return Err(VisionError::BlackFrame);
        }
        let rect =
            auto_locate_hp_bar(&frame, job.vision.brightness).ok_or(VisionError::BarNotLocated)?;
        let ratio = bar_fill_ratio(&frame, Some(rect)).ok_or(VisionError::InvalidReading)?;
        Ok((frame, rect, ratio))
    }

    /// 启动采样线程。
    ///
    /// 与 [`Source::start`] 等价，但返回具体的 [`VisionError`]，方便调用方
    /// 直接把原因塞进 HTTP 响应。重复调用是幂等的。
    pub fn start(&mut self) -> Result<(), VisionError> {
        self.start_worker()
    }

    /// 停止采样线程并作废当前读数。未启动时是空操作。
    pub fn stop(&mut self) {
        self.stop_worker();
    }

    fn start_worker(&mut self) -> Result<(), VisionError> {
        if self.started {
            return Ok(());
        }
        if !cfg!(windows) {
            self.startup_error = VisionError::Unsupported.to_string();
            return Err(VisionError::Unsupported);
        }
        self.startup_error.clear();
        *lock(&self.control.0) = false;
        {
            let mut shared = lock(&self.shared);
            *shared = Sample::default();
        }
        self.polled_seq = 0;
        self.need_baseline = true;
        self.had_live = false;

        let ready = Arc::new((Mutex::new(false), Condvar::new()));
        let job = Arc::clone(&self.job);
        let shared = Arc::clone(&self.shared);
        let control = Arc::clone(&self.control);
        let ready_for_worker = Arc::clone(&ready);
        let handle = thread::Builder::new()
            .name("vision-hud".into())
            .spawn(move || run(job, shared, control, ready_for_worker))
            .map_err(|err| VisionError::Spawn(err.to_string()))?;
        self.worker = Some(handle);

        let (guard, signal) = &*ready;
        let mut done = lock(guard);
        let deadline = Instant::now() + START_TIMEOUT;
        while !*done {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                break;
            }
            let (next, timeout) = signal
                .wait_timeout(done, remaining)
                .unwrap_or_else(|poison| poison.into_inner());
            done = next;
            if timeout.timed_out() && !*done {
                break;
            }
        }
        if !*done {
            self.stop_worker();
            self.startup_error = VisionError::StartupTimeout.to_string();
            return Err(VisionError::StartupTimeout);
        }

        self.started = true;
        Ok(())
    }

    fn stop_worker(&mut self) {
        *lock(&self.control.0) = true;
        self.control.1.notify_all();
        let joined = self.worker.take().map(|handle| handle.join());
        self.started = false;
        let mut shared = lock(&self.shared);
        shared.valid = false;
        shared.hp = None;
        // join 只有在采样线程 panic 时才返回 Err。这种情况必须让界面看得见：
        // 否则用户会以为"已停止"，下次启动才发现根本没人采。
        if matches!(joined, Some(Err(_))) {
            shared.note = "采样线程异常退出（可能已在抓屏时 panic）".into();
        }
    }
}

impl Source for VisionSource {
    fn name(&self) -> &str {
        NAME
    }

    /// `true`：断流即状态不可信。引擎会在 `alive` 为假时拒绝武装或直接跳闸。
    fn critical(&self) -> bool {
        true
    }

    fn start(&mut self) -> AdapterResult<()> {
        self.start_worker()
            .map_err(|error| -> AdapterError { Box::new(error) })
    }

    fn poll(&mut self, _now: f64) -> AdapterResult<Vec<SourceItem>> {
        let alive = self.is_alive();
        let (seq, hp) = {
            let shared = lock(&self.shared);
            (shared.seq, shared.hp)
        };

        if !alive {
            let mut out = Vec::new();
            if self.had_live {
                out.push(SourceItem::Resync);
                self.had_live = false;
            }
            self.need_baseline = true;
            self.polled_seq = seq;
            return Ok(out);
        }

        if seq == self.polled_seq {
            return Ok(Vec::new());
        }
        self.polled_seq = seq;
        self.had_live = true;

        let mut out = Vec::new();
        if self.need_baseline {
            // 恢复后的第一帧：先让引擎丢掉旧基线，否则"停游戏→回任务"的
            // 血量跳变会被算成一次巨量伤害。
            out.push(SourceItem::Resync);
            self.need_baseline = false;
        }
        let Some(hp) = hp else {
            return Ok(out);
        };
        out.push(SourceItem::State(StateFrame {
            hp: Some(hp),
            injury_scores: Vec::new(),
            bleeding_score: 0.0,
            death_score: 0.0,
            death_threshold: self.death_threshold,
            injury_names: if self.injury_names.is_empty() {
                None
            } else {
                Some(self.injury_names.clone())
            },
        }));
        Ok(out)
    }

    fn alive(&self, _now: f64) -> bool {
        self.is_alive()
    }

    fn stop(&mut self) -> AdapterResult<()> {
        self.stop_worker();
        Ok(())
    }
}

impl Drop for VisionSource {
    fn drop(&mut self) {
        self.stop_worker();
    }
}

fn publish(shared: &Mutex<Sample>, hp: f64, rect: ScreenRect) {
    let mut state = lock(shared);
    state.valid = true;
    state.hp = Some(hp.clamp(0.0, 1.0));
    state.rect = Some(rect);
    state.note = "前台".into();
    state.last_at = Some(Instant::now());
    state.capture_errors = 0;
    state.seq = state.seq.wrapping_add(1);
}

fn invalidate(shared: &Mutex<Sample>, reason: &str) {
    let mut state = lock(shared);
    state.valid = false;
    state.hp = None;
    state.capture_errors = state.capture_errors.saturating_add(1);
    let reason = reason.trim();
    state.note = if reason.chars().count() > 120 {
        reason.chars().take(120).collect()
    } else {
        reason.to_string()
    };
}

/// 采样线程主体：先点亮 ready，再按 fps 循环采样。
fn run(
    job: Arc<RwLock<Job>>,
    shared: Arc<Mutex<Sample>>,
    control: Control,
    ready: Arc<(Mutex<bool>, Condvar)>,
) {
    *lock(&ready.0) = true;
    ready.1.notify_all();

    loop {
        let started = Instant::now();
        let job_snapshot = read(&job).clone();
        let fps = if job_snapshot.vision.fps.is_finite() {
            job_snapshot.vision.fps.clamp(1.0, 30.0)
        } else {
            10.0
        };
        let period = Duration::from_secs_f64(1.0 / fps);

        match sample_once(&job_snapshot) {
            Ok((hp, rect)) => publish(&shared, hp, rect),
            Err(reason) => invalidate(&shared, &reason),
        }

        let remaining = period.saturating_sub(started.elapsed());
        if remaining.is_zero() {
            if *lock(&control.0) {
                break;
            }
            continue;
        }
        let guard = lock(&control.0);
        if *guard {
            break;
        }
        // 锁中毒说明另一条线程在持锁时 panic 过；采样线程没有不变量依赖这个值，
        // 取回内部状态继续用，不必整个源跟着停摆。
        let _guard = control
            .1
            .wait_timeout_while(guard, remaining, |stopped| !*stopped)
            .unwrap_or_else(|poison| poison.into_inner());
    }
}

/// 采一帧并读出比例。返回的错误文案直接进 `note`。
fn sample_once(job: &Job) -> Result<(f64, ScreenRect), String> {
    let candidate_list = title_candidates(&job.titles).join("/");
    let Some(window) = platform::find_window(&job.titles) else {
        return Err(format!(
            "没有找到游戏窗口（标题候选：{candidate_list}）；请先启动游戏"
        ));
    };
    let (x, y, width, height) = platform::client_rect(window).ok_or("读不出窗口客户区")?;
    if width < MIN_CLIENT_WIDTH || height < MIN_CLIENT_HEIGHT {
        return Err("游戏窗口已最小化或尺寸过小".into());
    }
    if !platform::is_foreground(window) {
        return Err("游戏不在前台，暂不读取（避免误读其它窗口）".into());
    }

    let configured = job.vision.hp_bar.filter(ScreenRect::is_valid);
    let (frame, rect) = match configured {
        Some(rect) => {
            if rect.x < 0
                || rect.y < 0
                || rect.x.saturating_add(rect.w) > width
                || rect.y.saturating_add(rect.h) > height
            {
                return Err("已保存的血条区域超出窗口；请重新校准".into());
            }
            let frame = platform::capture_rect(x + rect.x, y + rect.y, rect.w, rect.h)?;
            if is_black_frame(&frame) {
                return Err("血条区域全黑".into());
            }
            (frame, Some(rect))
        }
        None => {
            let frame = platform::capture_client(window)?;
            if is_black_frame(&frame) {
                return Err("窗口客户区全黑；请改用无边框窗口模式".into());
            }
            (frame, None)
        }
    };

    let rect = match rect {
        Some(rect) => Some(rect),
        None if job.vision.auto_locate => auto_locate_hp_bar(&frame, job.vision.brightness),
        None => None,
    };
    let rect = rect.ok_or("尚未定位到血条；需在任务中、HUD 可见")?;
    let ratio = bar_fill_ratio(&frame, Some(rect)).ok_or("血条读数无效")?;
    Ok((ratio, rect))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> AppConfig {
        AppConfig::default()
    }

    #[test]
    fn new_source_is_not_alive_and_not_started() {
        let source = VisionSource::new(&config());
        assert!(!source.is_alive());
        assert_eq!(source.describe(), "未启动");
        let snapshot = source.snapshot();
        assert!(!snapshot.alive);
        assert_eq!(snapshot.hp, None);
        assert!(snapshot.note.is_empty());
        assert!(snapshot.startup_error.is_empty());
    }

    #[test]
    fn metadata_matches_python_adapter() {
        let source = VisionSource::new(&config());
        assert_eq!(source.name(), NAME);
        assert!(source.critical());
        assert_eq!(NAME, "vision");
        assert_eq!(LABEL, "外部 HUD 视觉（EAC 兼容）");
    }

    #[test]
    fn poll_before_start_is_silent() {
        let mut source = VisionSource::new(&config());
        assert_eq!(source.poll(0.0).expect("poll"), Vec::new());
        assert!(source.poll(1.0).expect("poll").is_empty());
    }

    #[test]
    fn stale_source_asks_for_one_resync() {
        let mut source = VisionSource::new(&config());
        // 伪造"曾经有过实时读数"：poll 决定是否需要 Resync 只看这个标志。
        source.had_live = true;
        source.need_baseline = false;
        assert_eq!(source.poll(0.0).expect("poll"), vec![SourceItem::Resync]);
        // 同一次断流只发一次，不会每 20ms 刷一条。
        assert!(source.poll(0.02).expect("poll").is_empty());
        assert!(source.need_baseline);
    }

    #[test]
    fn publish_and_invalidate_drive_the_snapshot() {
        let source = VisionSource::new(&config());
        let rect = ScreenRect {
            x: 100,
            y: 900,
            w: 400,
            h: 12,
        };
        publish(&source.shared, 0.42, rect);
        let snapshot = source.snapshot();
        // 未 start() 时 started=false，所以 alive 仍为假，但读数已写入。
        assert!(!snapshot.alive);
        assert_eq!(snapshot.rect, Some(rect));
        assert_eq!(snapshot.note, "前台");
        assert_eq!(snapshot.capture_errors, 0);
        assert!((snapshot.age_s.unwrap()) < 5.0);

        invalidate(&source.shared, "游戏不在前台，暂不读取（避免误读其它窗口）");
        let snapshot = source.snapshot();
        assert_eq!(snapshot.hp, None);
        assert_eq!(snapshot.capture_errors, 1);
        assert!(snapshot.note.contains("不在前台"));
    }

    #[test]
    fn hp_is_clamped_into_range() {
        let source = VisionSource::new(&config());
        let rect = ScreenRect {
            x: 0,
            y: 0,
            w: 10,
            h: 10,
        };
        publish(&source.shared, 1.7, rect);
        assert_eq!(source.snapshot().hp, None); // alive 为假时不外泄读数
        let clamped = { lock(&source.shared).hp };
        assert_eq!(clamped, Some(1.0));
        publish(&source.shared, -3.0, rect);
        let clamped = { lock(&source.shared).hp };
        assert_eq!(clamped, Some(0.0));
    }

    #[test]
    fn long_notes_are_truncated() {
        let source = VisionSource::new(&config());
        let reason = "窗口".repeat(200);
        invalidate(&source.shared, &reason);
        let note = source.snapshot().note;
        assert_eq!(note.chars().count(), 120);
    }

    #[test]
    fn set_config_updates_thresholds_and_titles() {
        let mut source = VisionSource::new(&config());
        let mut cfg = config();
        cfg.detect.death_state_threshold = 0.9;
        cfg.hud.injury_slot_names = vec!["头".into(), "身".into()];
        cfg.safety.game_window_title = "MyGame".into();
        cfg.vision.auto_locate = true;
        source.set_config(&cfg);
        assert_eq!(source.death_threshold, Some(0.9));
        assert_eq!(
            source.injury_names,
            vec!["头".to_string(), "身".to_string()]
        );
        assert!(read(&source.job).vision.auto_locate);
        assert_eq!(read(&source.job).titles, "MyGame");
    }

    #[test]
    fn stop_is_idempotent_without_start() {
        let mut source = VisionSource::new(&config());
        source.stop();
        source.stop();
        assert!(!source.is_alive());
    }
}
