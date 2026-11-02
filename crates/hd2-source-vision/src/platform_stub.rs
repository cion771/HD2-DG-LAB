//! 非 Windows 占位实现。
//!
//! 视觉源只对 Windows 版游戏有意义，但整个 workspace 需要在别的目标上也能
//! 编译通过（CI、`cargo check --target`、文档构建）。这里每个函数都返回
//! "不支持"，绝不静默返回假数据。

use crate::frame::Frame;

pub fn init() {}

pub fn find_window(_configured: &str) -> Option<isize> {
    None
}

pub fn client_rect(_window: isize) -> Option<(i32, i32, i32, i32)> {
    None
}

pub fn is_foreground(_window: isize) -> bool {
    false
}

pub fn capture_rect(_x: i32, _y: i32, _width: i32, _height: i32) -> Result<Frame, String> {
    Err("抓屏仅在 Windows 上可用".into())
}

pub fn capture_client(_window: isize) -> Result<Frame, String> {
    Err("抓屏仅在 Windows 上可用".into())
}
