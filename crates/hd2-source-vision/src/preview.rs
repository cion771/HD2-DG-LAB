//! 校准预览图。
//!
//! 界面在保存 `vision.hp_bar` 之前需要看到"候选区域长什么样"。这里把血条
//! 区域放大成一张 24 位 BMP，再用 data URL 返回——BMP 编码只有几十行，
//! 不引入 PNG 依赖，浏览器照样能显示。

use crate::analyze::crop;
use crate::frame::Frame;
use hd2_core::config::ScreenRect;

/// 预览目标宽度，与 Python 桌面版一致。
pub const PREVIEW_WIDTH: u32 = 480;
/// 预览最小高度。
pub const PREVIEW_MIN_HEIGHT: u32 = 24;
/// 预览最大高度。
pub const PREVIEW_MAX_HEIGHT: u32 = 120;
/// 血条厚度换算成预览高度的倍数。
const PREVIEW_HEIGHT_FACTOR: u32 = 4;

/// 把血条区域放大成 `data:image/bmp;base64,...`。
///
/// 区域无效或为空时返回 `None`。
pub fn data_url(frame: &Frame, rect: ScreenRect) -> Option<String> {
    let region = crop(frame, rect)?;
    let (width, height) = preview_size(&region);
    let scaled = scale(&region, width, height)?;
    let bmp = encode_bmp(&scaled);
    let mut url = String::with_capacity(bmp.len() / 3 * 4 + 24);
    url.push_str("data:image/bmp;base64,");
    encode_base64(&bmp, &mut url);
    Some(url)
}

/// 预览尺寸：宽度固定，高度是血条厚度的四倍并夹在 24..120。
fn preview_size(region: &Frame) -> (u32, u32) {
    let width = PREVIEW_WIDTH.min(region.width().max(1));
    let height = region
        .height()
        .saturating_mul(PREVIEW_HEIGHT_FACTOR)
        .clamp(PREVIEW_MIN_HEIGHT, PREVIEW_MAX_HEIGHT);
    (width, height)
}

/// 最近邻放大。血条只有十几个像素高，插值反而糊掉亮度分布。
///
/// 只有尺寸积溢出时才返回 `None`——正常帧不会走到那条分支。
fn scale(frame: &Frame, width: u32, height: u32) -> Option<Frame> {
    let source = frame.rgb();
    let source_width = frame.width().max(1);
    let source_height = frame.height().max(1);
    let capacity = (width as usize)
        .checked_mul(height as usize)?
        .checked_mul(3)?;
    let mut rgb = Vec::with_capacity(capacity);
    for y in 0..height {
        let sy = (y as u64 * source_height as u64 / height as u64) as u32;
        for x in 0..width {
            let sx = (x as u64 * source_width as u64 / width as u64) as u32;
            let base = ((sy * source_width + sx) * 3) as usize;
            rgb.extend_from_slice(source.get(base..base + 3)?);
        }
    }
    Frame::new(width, height, rgb)
}

/// 编码 24 位未压缩 BMP（自底向上，行按 4 字节对齐）。
fn encode_bmp(frame: &Frame) -> Vec<u8> {
    let width = frame.width();
    let height = frame.height();
    let row_bytes = (width * 3).div_ceil(4) * 4;
    let pixel_bytes = row_bytes * height;
    let mut out = Vec::with_capacity(54 + pixel_bytes as usize);

    out.extend_from_slice(b"BM");
    out.extend_from_slice(&(54 + pixel_bytes).to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    out.extend_from_slice(&54u32.to_le_bytes());

    out.extend_from_slice(&40u32.to_le_bytes());
    out.extend_from_slice(&(width as i32).to_le_bytes());
    out.extend_from_slice(&(height as i32).to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&24u16.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&pixel_bytes.to_le_bytes());
    out.extend_from_slice(&2835i32.to_le_bytes());
    out.extend_from_slice(&2835i32.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());

    let rgb = frame.rgb();
    let padding = (row_bytes - width * 3) as usize;
    for y in (0..height).rev() {
        let start = (y * width * 3) as usize;
        for pixel in rgb[start..start + (width * 3) as usize].chunks_exact(3) {
            out.extend_from_slice(&[pixel[2], pixel[1], pixel[0]]);
        }
        out.extend(std::iter::repeat_n(0u8, padding));
    }
    out
}

const BASE64_TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// 标准 base64（带 `=` 填充）。
fn encode_base64(bytes: &[u8], out: &mut String) {
    for chunk in bytes.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(BASE64_TABLE[(triple >> 18) as usize & 63] as char);
        out.push(BASE64_TABLE[(triple >> 12) as usize & 63] as char);
        if chunk.len() > 1 {
            out.push(BASE64_TABLE[(triple >> 6) as usize & 63] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(BASE64_TABLE[triple as usize & 63] as char);
        } else {
            out.push('=');
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn solid(width: u32, height: u32, value: [u8; 3]) -> Frame {
        let mut rgb = Vec::with_capacity((width * height * 3) as usize);
        for _ in 0..width * height {
            rgb.extend_from_slice(&value);
        }
        Frame::new(width, height, rgb).expect("frame")
    }

    #[test]
    fn preview_url_is_a_bmp_data_url() {
        let frame = solid(40, 10, [10, 20, 30]);
        let url = data_url(
            &frame,
            ScreenRect {
                x: 0,
                y: 0,
                w: 40,
                h: 10,
            },
        )
        .expect("preview");
        assert!(url.starts_with("data:image/bmp;base64,"));
        assert!(url.len() > 64);
    }

    #[test]
    fn invalid_region_has_no_preview() {
        let frame = solid(8, 8, [1, 2, 3]);
        assert!(data_url(&frame, ScreenRect::default()).is_none());
    }

    #[test]
    fn bmp_header_describes_the_image() {
        let frame = solid(3, 2, [1, 2, 3]);
        let bmp = encode_bmp(&frame);
        assert_eq!(&bmp[0..2], b"BM");
        assert_eq!(
            u32::from_le_bytes(bmp[2..6].try_into().unwrap()) as usize,
            bmp.len()
        );
        assert_eq!(u32::from_le_bytes(bmp[10..14].try_into().unwrap()), 54);
        assert_eq!(u32::from_le_bytes(bmp[18..22].try_into().unwrap()), 3);
        assert_eq!(u32::from_le_bytes(bmp[22..26].try_into().unwrap()), 2);
        // 每行 3 像素 × 3 字节 = 9，向上对齐到 12。
        assert_eq!(u32::from_le_bytes(bmp[34..38].try_into().unwrap()), 24);
        assert_eq!(bmp.len(), 54 + 24);
    }

    #[test]
    fn base64_matches_known_padding_cases() {
        let cases: [(&[u8], &str); 3] = [(b"", ""), (b"f", "Zg=="), (b"foob", "Zm9vYg==")];
        for (input, expected) in cases {
            let mut out = String::new();
            encode_base64(input, &mut out);
            assert_eq!(out, expected);
        }
    }

    #[test]
    fn base64_matches_rfc4648_vectors() {
        let mut out = String::new();
        encode_base64(b"foobar", &mut out);
        assert_eq!(out, "Zm9vYmFy");
        out.clear();
        encode_base64(&[0u8, 0, 0], &mut out);
        assert_eq!(out, "AAAA");
    }
}
