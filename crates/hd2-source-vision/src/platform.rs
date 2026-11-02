//! Win32 实现：窗口枚举、客户区几何、GDI 抓屏。
//!
//! 安全边界（与 Python 桌面版一致，不可放宽）：
//! * 只用窗口枚举与几何 API 找目标，**不** `OpenProcess`、**不**
//!   `ReadProcessMemory`、不往游戏进程里加载任何东西；
//! * 只 `BitBlt` 客户区，不抓整屏、不挂游戏渲染管线；
//! * 窗口不在前台时调用方必须拒绝发布读数（见 `crate::VisionSource`）。
//!
//! 这就是"进程外"的含义：对游戏而言本 crate 和一个截图软件没有区别。

use std::ffi::c_void;
use std::sync::Once;

use windows_sys::Win32::Foundation::{HWND, LPARAM, POINT, RECT};
use windows_sys::Win32::Graphics::Gdi::{
    BITMAPINFO, BitBlt, CAPTUREBLT, ClientToScreen, CreateCompatibleDC, CreateDIBSection,
    DIB_RGB_COLORS, DeleteDC, DeleteObject, GetDC, HDC, HGDIOBJ, ReleaseDC, SRCCOPY, SelectObject,
};
use windows_sys::Win32::UI::HiDpi::{
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, SetProcessDpiAwarenessContext,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    EnumWindows, GetClientRect, GetForegroundWindow, GetWindowTextLengthW, GetWindowTextW,
    IsIconic, IsWindowVisible, SetProcessDPIAware, WNDENUMPROC,
};

use crate::frame::Frame;
use crate::titles;

static INIT: Once = Once::new();

/// 声明进程 DPI 感知。必须在任何窗口查询之前执行一次。
///
/// 不声明的话，系统会对坐标做 DPI 虚拟化：125% 缩放下读到的客户区比真实
/// 像素少 20%，抓出来的图会被拉伸，血条读数跟着系统性偏低。
pub fn init() {
    INIT.call_once(|| {
        // 优先 per-monitor v2（多屏不同缩放也正确）；老系统退回系统级感知。
        let ok =
            unsafe { SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) };
        if ok == 0 {
            unsafe { SetProcessDPIAware() };
        }
    });
}

fn hwnd(raw: isize) -> HWND {
    raw as HWND
}

/// 读窗口标题；空标题返回空串。
fn window_title(hwnd: HWND) -> String {
    let len = unsafe { GetWindowTextLengthW(hwnd) };
    if len <= 0 {
        return String::new();
    }
    let mut buffer = vec![0u16; len as usize + 1];
    let copied = unsafe { GetWindowTextW(hwnd, buffer.as_mut_ptr(), buffer.len() as i32) };
    if copied <= 0 {
        return String::new();
    }
    String::from_utf16_lossy(&buffer[..copied as usize])
}

struct Search<'a> {
    configured: &'a str,
    found: Option<HWND>,
}

unsafe extern "system" fn enum_proc(hwnd: HWND, lparam: LPARAM) -> i32 {
    // SAFETY: lparam 是 find_window 栈上 Search 的指针，枚举期间一直有效。
    let search = unsafe { &mut *(lparam as *mut Search<'_>) };
    if search.found.is_some() {
        return 0; // 找到即停
    }
    if unsafe { IsWindowVisible(hwnd) } == 0 {
        return 1;
    }
    let title = window_title(hwnd);
    if title.trim().is_empty() {
        return 1;
    }
    if titles::window_title_matches(&title, search.configured) {
        search.found = Some(hwnd);
        return 0;
    }
    1
}

/// 找游戏主窗口，返回不透明句柄。
pub fn find_window(configured: &str) -> Option<isize> {
    init();
    let mut search = Search {
        configured,
        found: None,
    };
    let callback: WNDENUMPROC = Some(enum_proc);
    unsafe {
        EnumWindows(callback, &mut search as *mut Search<'_> as LPARAM);
    }
    search.found.map(|found| found as isize)
}

/// 客户区在屏幕坐标系的位置与尺寸 `(x, y, w, h)`。
///
/// 最小化时返回 `(0, 0, 0, 0)`，调用方按"尺寸过小"处理。
pub fn client_rect(window: isize) -> Option<(i32, i32, i32, i32)> {
    let hwnd = hwnd(window);
    unsafe {
        if IsIconic(hwnd) != 0 {
            return Some((0, 0, 0, 0));
        }
        let mut rect = RECT::default();
        if GetClientRect(hwnd, &mut rect) == 0 {
            return None;
        }
        let mut origin = POINT { x: 0, y: 0 };
        if ClientToScreen(hwnd, &mut origin) == 0 {
            return None;
        }
        Some((
            origin.x,
            origin.y,
            rect.right - rect.left,
            rect.bottom - rect.top,
        ))
    }
}

/// 目标窗口当前是否在前台。
pub fn is_foreground(window: isize) -> bool {
    unsafe { GetForegroundWindow() == hwnd(window) }
}

/// 抓屏幕上一块矩形，转成 RGB 帧。
///
/// 用 `CAPTUREBLT`：无边框窗口叠加层（HUD、Discord 覆盖）也能被抓到，
/// 少了这个标志偶尔会拿到黑帧。
pub fn capture_rect(x: i32, y: i32, width: i32, height: i32) -> Result<Frame, String> {
    if width <= 0 || height <= 0 {
        return Err(format!("抓屏尺寸无效：{width}x{height}"));
    }
    init();
    let screen: HDC = unsafe { GetDC(std::ptr::null_mut()) };
    if screen.is_null() {
        return Err("GetDC 失败：拿不到屏幕设备上下文".into());
    }
    let memory: HDC = unsafe { CreateCompatibleDC(screen) };
    if memory.is_null() {
        unsafe { ReleaseDC(std::ptr::null_mut(), screen) };
        return Err("CreateCompatibleDC 失败".into());
    }

    let mut info = BITMAPINFO::default();
    info.bmiHeader.biSize = size_of::<windows_sys::Win32::Graphics::Gdi::BITMAPINFOHEADER>() as u32;
    info.bmiHeader.biWidth = width;
    info.bmiHeader.biHeight = -height; // 负高度 = 自上而下的行序
    info.bmiHeader.biPlanes = 1;
    info.bmiHeader.biBitCount = 32;
    info.bmiHeader.biCompression = 0; // BI_RGB
    info.bmiHeader.biSizeImage = (width as u32) * (height as u32) * 4;

    let mut bits: *mut c_void = std::ptr::null_mut();
    let bitmap = unsafe {
        CreateDIBSection(
            screen,
            &info,
            DIB_RGB_COLORS,
            &mut bits,
            std::ptr::null_mut(),
            0,
        )
    };
    if bitmap.is_null() || bits.is_null() {
        unsafe {
            DeleteDC(memory);
            ReleaseDC(std::ptr::null_mut(), screen);
        }
        return Err("CreateDIBSection 失败：申请抓屏位图出错".into());
    }

    let previous: HGDIOBJ = unsafe { SelectObject(memory, bitmap) };
    let copied = unsafe {
        BitBlt(
            memory,
            0,
            0,
            width,
            height,
            screen,
            x,
            y,
            SRCCOPY | CAPTUREBLT,
        )
    };
    let result = if copied == 0 {
        Err(format!("BitBlt 失败：{}", std::io::Error::last_os_error()))
    } else {
        let pixels = (width as usize) * (height as usize) * 4;
        // SAFETY: CreateDIBSection 已保证该缓冲至少这么多字节，且在下面
        // 释放位图之前一直有效。
        let buffer = unsafe { std::slice::from_raw_parts(bits as *const u8, pixels) };
        Frame::from_bgra(width as u32, height as u32, buffer)
            .ok_or_else(|| "抓屏缓冲尺寸不匹配".to_string())
    };

    unsafe {
        SelectObject(memory, previous);
        DeleteObject(bitmap);
        DeleteDC(memory);
        ReleaseDC(std::ptr::null_mut(), screen);
    }
    result
}

/// 抓窗口客户区的全部像素。
pub fn capture_client(window: isize) -> Result<Frame, String> {
    let (x, y, w, h) = client_rect(window).ok_or_else(|| "读不出窗口客户区".to_string())?;
    capture_rect(x, y, w, h)
}
