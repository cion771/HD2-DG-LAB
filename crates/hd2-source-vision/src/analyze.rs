//! 血条像素分析。逐条对齐 `hd2coyote/vision.py` 的纯函数实现。
//!
//! 这里没有任何 Win32 依赖，输入永远是一帧 [`Frame`]，输出是 `0.0..=1.0`
//! 的血条填充比例或一个矩形。所有魔法常量都保留 Python 端原值，改动会直接
//! 改变真实读数，必须同步两边。

use hd2_core::config::ScreenRect;

use crate::frame::Frame;

/// 整帧平均亮度低于它就算黑屏（0..255 尺度）。
pub const BLACK_FRAME_MEAN: f64 = 0.6;
/// 列峰值低于它就不再找阈值，直接判 0%——血条区域一片暗。
pub const BAR_MIN_PEAK: f64 = 110.0;
/// 判定"填充"的绝对门槛。
pub const BAR_MIN_THRESHOLD: f64 = 60.0;
/// 判定"填充"的相对门槛（占列峰值的比例）。
pub const BAR_RELATIVE_THRESHOLD: f64 = 0.55;
/// 自动定位默认亮度阈值。
pub const DEFAULT_BRIGHTNESS: f64 = 165.0;
/// 自动定位只扫画面下方这一段之后的行。
pub const AUTO_LOCATE_Y_START: f64 = 0.55;
/// 候选横条的最短像素长度。
pub const AUTO_LOCATE_MIN_RUN: usize = 40;
/// 候选横条最短长度（占画面宽度）。
pub const AUTO_LOCATE_MIN_WIDTH_RATIO: f64 = 0.05;
/// 候选横条最长长度（占画面宽度）。
pub const AUTO_LOCATE_MAX_WIDTH_RATIO: f64 = 0.35;
/// 血条最大厚度，超过就认为是面板/对话框而不是血条。
pub const AUTO_LOCATE_MAX_THICKNESS: u32 = 40;
/// 上下扩展时，相邻行在同段范围内至少这么亮才并入。
pub const AUTO_LOCATE_GROW_FRACTION: f64 = 0.45;

/// 整帧是否接近全黑。全黑说明抓到了错误的目标（最小化、独占全屏、遮挡）。
pub fn is_black_frame(frame: &Frame) -> bool {
    is_black_frame_with(frame, BLACK_FRAME_MEAN)
}

/// 与 [`is_black_frame`] 相同，可指定阈值（测试与调试用）。
pub fn is_black_frame_with(frame: &Frame, threshold: f64) -> bool {
    let bytes = frame.rgb();
    if bytes.is_empty() {
        return true;
    }
    let sum: u64 = bytes.iter().map(|value| u64::from(*value)).sum();
    (sum as f64) / (bytes.len() as f64) < threshold
}

/// 把 `(start, len)` 夹到 `0..limit`，返回半开区间。
///
/// 对齐 Python `crop()`：起点夹到 `limit-1`，终点先夹到 `limit` 再保证至少
/// 比起点大 1。返回 `None` 表示这个维度上取不到像素。
fn clamp_span(start: i32, len: i32, limit: u32) -> Option<(usize, usize)> {
    if limit == 0 {
        return None;
    }
    let limit = limit as i32;
    let start = start.max(0).min(limit - 1);
    let end = start
        .saturating_add(len)
        .min(limit)
        .max(start + 1)
        .min(limit);
    Some((start as usize, end as usize))
}

/// 按矩形裁出子帧，越界部分按 `clamp_span` 规则夹到帧内。
///
/// 矩形无效（宽或高不大于 1）时返回 `None`，与 Python `crop()` 一致。
pub fn crop(frame: &Frame, rect: ScreenRect) -> Option<Frame> {
    if !rect.is_valid() {
        return None;
    }
    let (x0, x1) = clamp_span(rect.x, rect.w, frame.width())?;
    let (y0, y1) = clamp_span(rect.y, rect.h, frame.height())?;
    let width = x1 - x0;
    let height = y1 - y0;
    let stride = frame.width() as usize;
    let source = frame.rgb();
    let mut rgb = Vec::with_capacity(width * height * 3);
    for y in y0..y1 {
        let row = (y * stride + x0) * 3;
        rgb.extend_from_slice(&source[row..row + width * 3]);
    }
    Frame::new(width as u32, height as u32, rgb)
}

/// 血条区域的填充比例。
///
/// `rect` 为 `None` 表示整帧；给了但 `is_valid()` 为假时返回 `None`，与
/// Python 里 `crop()` 拿到无效 box 后直接放弃一致。算法：丢掉左右各一列
/// 边框 → 只取纵向中间一半（避开描边和上下高光）→ 每列取最亮通道的最大值
/// → 以 `max(60, 峰值*0.55)` 为门槛数超过门槛的列。
pub fn bar_fill_ratio(frame: &Frame, rect: Option<ScreenRect>) -> Option<f64> {
    let (mut left, mut right, mut top, mut bottom) = match rect {
        Some(rect) => {
            if !rect.is_valid() {
                return None;
            }
            let (x0, x1) = clamp_span(rect.x, rect.w, frame.width())?;
            let (y0, y1) = clamp_span(rect.y, rect.h, frame.height())?;
            (x0, x1, y0, y1)
        }
        None => (0, frame.width() as usize, 0, frame.height() as usize),
    };

    // 左右各丢 1 列：血条两端常有 1px 描边，算进去会把读数系统性地抬高。
    if right - left > 4 {
        left += 1;
        right -= 1;
    }
    // 纵向只留中间一半：上下各 1/4 通常是高光与描边。
    let origin_top = top;
    let height = bottom - top;
    if height >= 4 {
        let mid0 = height / 4;
        let mid1 = (height * 3 / 4).max(mid0 + 1).min(height);
        top = origin_top + mid0;
        bottom = origin_top + mid1;
    }

    let cols = right - left;
    if cols == 0 || top >= bottom {
        return Some(0.0);
    }

    let width = frame.width() as usize;
    let rgb = frame.rgb();
    let mut col_max = vec![0u8; cols];
    for y in top..bottom {
        let row = y * width;
        for (offset, slot) in col_max.iter_mut().enumerate() {
            let index = (row + left + offset) * 3;
            let peak = rgb[index].max(rgb[index + 1]).max(rgb[index + 2]);
            if peak > *slot {
                *slot = peak;
            }
        }
    }

    let peak = col_max.iter().copied().max().unwrap_or(0) as f64;
    if peak < BAR_MIN_PEAK {
        return Some(0.0);
    }
    let threshold = BAR_MIN_THRESHOLD.max(peak * BAR_RELATIVE_THRESHOLD);
    let filled = col_max
        .iter()
        .filter(|value| f64::from(**value) > threshold)
        .count();
    Some(filled as f64 / cols as f64)
}

/// 最亮通道是否超过亮度阈值。
#[inline]
fn bright_at(frame: &Frame, x: usize, y: usize, brightness: f64) -> bool {
    let width = frame.width() as usize;
    let index = (y * width + x) * 3;
    let rgb = frame.rgb();
    let peak = rgb[index].max(rgb[index + 1]).max(rgb[index + 2]);
    f64::from(peak) > brightness
}

/// 在画面下方自动找血条。
///
/// 依据：血条是一条细长的亮色横条，水平居中，厚度不超过 40px。逐行找满足
/// 长度区间的连续亮像素段，用 `长度 - 居中偏差*0.6` 打分取最优，再向上下
/// 把同段范围内仍足够亮的行并进来算厚度。
///
/// 找不到返回 `None`——调用方据此报"尚未定位到血条"，绝不猜一个坐标。
pub fn auto_locate_hp_bar(frame: &Frame, brightness: f64) -> Option<ScreenRect> {
    let width = frame.width() as usize;
    let height = frame.height() as usize;
    if width == 0 || height == 0 {
        return None;
    }
    let band_top = (height as f64 * AUTO_LOCATE_Y_START) as usize;
    let band_height = height.saturating_sub(band_top);
    if band_height == 0 {
        return None;
    }

    let min_len = AUTO_LOCATE_MIN_RUN.max((width as f64 * AUTO_LOCATE_MIN_WIDTH_RATIO) as usize);
    let max_len = (width as f64 * AUTO_LOCATE_MAX_WIDTH_RATIO) as usize;
    if min_len > max_len {
        return None;
    }

    let mut best: Option<(usize, usize, usize, f64)> = None;
    for row in 0..band_height {
        let y = band_top + row;
        let mut x = 0;
        while x < width {
            if !bright_at(frame, x, y, brightness) {
                x += 1;
                continue;
            }
            let start = x;
            while x < width && bright_at(frame, x, y, brightness) {
                x += 1;
            }
            let end = x;
            let length = end - start;
            if length < min_len || length > max_len {
                continue;
            }
            let center = (start + end) as f64 / 2.0;
            let score = length as f64 - (center - width as f64 / 2.0).abs() * 0.6;
            if best.is_none_or(|(_, _, _, previous)| score > previous) {
                best = Some((row, start, end, score));
            }
        }
    }

    let (row, start, end, _) = best?;

    // 向上下扩厚度：相邻行在同一段范围内至少 45% 的列仍然够亮才并入。
    let grow = |row: usize| -> bool {
        let y = band_top + row;
        let mut lit = 0usize;
        for x in start..end {
            if bright_at(frame, x, y, brightness) {
                lit += 1;
            }
        }
        let span = end - start;
        span > 0 && (lit as f64 / span as f64) > AUTO_LOCATE_GROW_FRACTION
    };

    let mut top = row;
    while top > 0 && grow(top - 1) {
        top -= 1;
    }
    let mut bottom = row;
    while bottom + 1 < band_height && grow(bottom + 1) {
        bottom += 1;
    }

    let thickness = ((bottom - top + 1) as u32).max(3);
    if thickness > AUTO_LOCATE_MAX_THICKNESS {
        return None;
    }
    Some(ScreenRect {
        x: start as i32,
        y: (band_top + top) as i32,
        w: (end - start) as i32,
        h: thickness as i32,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 深色背景 + 一条亮条；`fill` 是填充比例，`bar_width` 是条宽度。
    fn frame_with_bar(width: u32, height: u32, bar_width: u32, fill: f64) -> Frame {
        let bar_x = (width - bar_width) / 2;
        let bar_y = height * 3 / 4;
        let bar_h = 12u32;
        let filled = (bar_width as f64 * fill) as u32;
        let mut data = Vec::with_capacity((width * height * 3) as usize);
        for y in 0..height {
            for x in 0..width {
                let in_bar = y >= bar_y && y < bar_y + bar_h && x >= bar_x && x < bar_x + bar_width;
                let lit = in_bar && (x - bar_x) < filled;
                let value: u8 = if lit { 240 } else { 18 };
                data.extend_from_slice(&[value, value, value]);
            }
        }
        Frame::new(width, height, data).expect("frame")
    }

    #[test]
    fn fill_ratio_tracks_filled_width() {
        let full = frame_with_bar(800, 400, 240, 1.0);
        let half = frame_with_bar(800, 400, 240, 0.5);
        let empty = frame_with_bar(800, 400, 240, 0.0);
        let rect = auto_locate_hp_bar(&full, DEFAULT_BRIGHTNESS).expect("located");
        assert!((bar_fill_ratio(&full, Some(rect)).unwrap() - 1.0).abs() < 0.02);
        assert!((bar_fill_ratio(&half, Some(rect)).unwrap() - 0.5).abs() < 0.05);
        assert!(bar_fill_ratio(&empty, Some(rect)).unwrap() < 0.02);
    }

    #[test]
    fn auto_locate_finds_the_bar_geometry() {
        // 240/800 = 30%，落在 5%..35% 的合理区间内。
        let frame = frame_with_bar(800, 400, 240, 1.0);
        let rect = auto_locate_hp_bar(&frame, DEFAULT_BRIGHTNESS).expect("located");
        assert_eq!(rect.y, 300);
        assert_eq!(rect.w, 240);
        assert_eq!(rect.x, 280);
        assert!(rect.h >= 3 && rect.h <= AUTO_LOCATE_MAX_THICKNESS as i32);
        assert!(rect.is_valid());
    }

    #[test]
    fn auto_locate_gives_up_on_features_and_dark_screens() {
        // 一条太宽的亮带（面板/对话框）不能被当成血条。
        let wide = frame_with_bar(800, 400, 600, 1.0);
        assert!(auto_locate_hp_bar(&wide, DEFAULT_BRIGHTNESS).is_none());
        // 全黑画面。
        let dark = Frame::new(800, 400, vec![0; 800 * 400 * 3]).expect("frame");
        assert!(auto_locate_hp_bar(&dark, DEFAULT_BRIGHTNESS).is_none());
        // 尺寸为 0 的画面。
        let tiny = Frame::new(1, 1, vec![255; 3]).expect("frame");
        assert!(auto_locate_hp_bar(&tiny, DEFAULT_BRIGHTNESS).is_none());
    }

    #[test]
    fn invalid_or_out_of_bounds_rect_is_rejected() {
        let frame = frame_with_bar(400, 200, 200, 1.0);
        let invalid = ScreenRect {
            x: 0,
            y: 0,
            w: 1,
            h: 1,
        };
        assert!(bar_fill_ratio(&frame, Some(invalid)).is_none());
        // 越界矩形被夹到帧内，读数仍然有效而不是报错。
        let outside = ScreenRect {
            x: 10_000,
            y: 10_000,
            w: 50,
            h: 50,
        };
        assert!(bar_fill_ratio(&frame, Some(outside)).is_some());
        let negative = ScreenRect {
            x: -40,
            y: -40,
            w: 80,
            h: 80,
        };
        assert!(bar_fill_ratio(&frame, Some(negative)).is_some());
    }

    #[test]
    fn dark_region_reads_zero_not_noise() {
        let frame = frame_with_bar(400, 200, 200, 1.0);
        let top_left = ScreenRect {
            x: 0,
            y: 0,
            w: 100,
            h: 40,
        };
        assert_eq!(bar_fill_ratio(&frame, Some(top_left)), Some(0.0));
    }

    #[test]
    fn whole_frame_default_is_accepted() {
        let frame = frame_with_bar(400, 200, 200, 1.0);
        assert!(bar_fill_ratio(&frame, None).is_some());
    }

    #[test]
    fn black_frame_detection() {
        let black = Frame::new(4, 4, vec![0; 48]).expect("frame");
        assert!(is_black_frame(&black));
        let lit = frame_with_bar(400, 200, 200, 1.0);
        assert!(!is_black_frame(&lit));
    }
}
