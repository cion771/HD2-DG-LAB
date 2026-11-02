//! 与平台无关的 RGB 帧容器。
//!
//! 抓屏后端只负责把像素搬到这里，分析代码（[`crate::analyze`]）完全不知道
//! Win32 的存在，因此可以离线用合成帧做单元测试。

/// 一帧 8 位 RGB 图像，行优先、每像素 3 字节、无行填充。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    width: u32,
    height: u32,
    rgb: Vec<u8>,
}

impl Frame {
    /// 用已排好序的 RGB 字节构造。长度不符时返回 `None`，不做静默裁剪。
    pub fn new(width: u32, height: u32, rgb: Vec<u8>) -> Option<Self> {
        let expected = (width as usize)
            .checked_mul(height as usize)?
            .checked_mul(3)?;
        if width == 0 || height == 0 || rgb.len() != expected {
            return None;
        }
        Some(Self { width, height, rgb })
    }

    /// 把 BGRA 缓冲（GDI `BI_RGB` 32 位 DIB 的原生布局）转成 RGB。
    ///
    /// 第 4 个字节被丢弃：视觉源只读亮度，alpha 在位图上恒为 0。
    pub fn from_bgra(width: u32, height: u32, bgra: &[u8]) -> Option<Self> {
        let pixels = (width as usize).checked_mul(height as usize)?;
        if bgra.len() < pixels.checked_mul(4)? {
            return None;
        }
        let mut rgb = Vec::with_capacity(pixels * 3);
        for chunk in bgra[..pixels * 4].chunks_exact(4) {
            rgb.push(chunk[2]);
            rgb.push(chunk[1]);
            rgb.push(chunk[0]);
        }
        Some(Self { width, height, rgb })
    }

    /// 像素宽度。
    pub fn width(&self) -> u32 {
        self.width
    }

    /// 像素高度。
    pub fn height(&self) -> u32 {
        self.height
    }

    /// 紧凑 RGB 字节，长度恒为 `width * height * 3`。
    pub fn rgb(&self) -> &[u8] {
        &self.rgb
    }

    /// 单像素 RGB。坐标越界返回 `None`（分析代码不依赖它做边界裁剪）。
    pub fn pixel(&self, x: u32, y: u32) -> Option<[u8; 3]> {
        if x >= self.width || y >= self.height {
            return None;
        }
        let i = (y as usize * self.width as usize + x as usize) * 3;
        Some([self.rgb[i], self.rgb[i + 1], self.rgb[i + 2]])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_mismatched_buffer() {
        assert!(Frame::new(2, 2, vec![0; 11]).is_none());
        assert!(Frame::new(0, 2, vec![]).is_none());
        assert!(Frame::new(2, 2, vec![0; 12]).is_some());
    }

    #[test]
    fn bgra_becomes_rgb() {
        // 两个像素：红 (B=0,G=0,R=255) 与蓝 (B=255,G=0,R=0)。
        let bgra = [0, 0, 255, 0, 255, 0, 0, 0];
        let frame = Frame::from_bgra(2, 1, &bgra).expect("frame");
        assert_eq!(frame.pixel(0, 0), Some([255, 0, 0]));
        assert_eq!(frame.pixel(1, 0), Some([0, 0, 255]));
        assert_eq!(frame.pixel(2, 0), None);
    }
}
