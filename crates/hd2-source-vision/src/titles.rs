//! 窗口标题匹配。
//!
//! 纯字符串逻辑，所以放在平台模块之外：非 Windows 上也能跑测试。
//! 语义与 Python `hd2coyote.sources.vision` 的同名函数逐条对齐。

/// 内置候选标题。配置里的 `safety.game_window_title` 会用 `|` 追加在它们前面。
pub const DEFAULT_TITLES: [&str; 3] = ["HELLDIVERS", "绝地潜兵", "HELLDIVER"];

/// 把配置串拆成候选标题：按 `|` 分隔、去空白、丢空项，再补上内置标题。
///
/// 顺序保留配置里写的先后，`DEFAULT_TITLES` 永远在最后；按大写去重，
/// 因此 `"helldivers|HELLDIVERS"` 只会留一个。
pub fn title_candidates(configured: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for part in configured.split('|') {
        let token = part.trim();
        if !token.is_empty() {
            out.push(token.to_string());
        }
    }
    for title in DEFAULT_TITLES {
        out.push(title.to_string());
    }
    let mut seen = Vec::new();
    out.retain(|token| {
        let key = token.to_uppercase();
        if seen.contains(&key) {
            false
        } else {
            seen.push(key);
            true
        }
    });
    out
}

/// 归一化：去首尾空白、去掉商标符、去掉所有空格、转大写。
fn normalize(text: &str) -> String {
    text.trim().to_uppercase().replace(['\u{2122}', ' '], "")
}

/// 判断窗口标题是否属于游戏。
///
/// 故意保守：只有归一化后完全相等才算命中，`HELLDIVERS 2 - 浏览器` 这类
/// 带后缀的窗口不会被误认成游戏，避免截到网页冒充的画面。
/// 内置标题额外接受带 `2` 的写法（`HELLDIVERS2`、`绝地潜兵2`）。
pub fn window_title_matches(title: &str, configured: &str) -> bool {
    let candidate = normalize(title);
    if candidate.is_empty() {
        return false;
    }
    let mut allowed: Vec<String> = Vec::new();
    for token in title_candidates(configured) {
        let normalized = normalize(&token);
        if normalized.is_empty() {
            continue;
        }
        if DEFAULT_TITLES
            .iter()
            .any(|known| normalize(known) == normalized)
        {
            allowed.push(format!("{normalized}2"));
        }
        allowed.push(normalized);
    }
    allowed.contains(&candidate)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn candidates_keep_configured_order_then_defaults() {
        assert_eq!(
            title_candidates("MyGame | HELDIVERS"),
            vec!["MyGame", "HELDIVERS", "HELLDIVERS", "绝地潜兵", "HELLDIVER"]
        );
    }

    #[test]
    fn candidates_drop_blanks_and_duplicates_case_insensitively() {
        assert_eq!(
            title_candidates("  | helldivers | HELLDIVERS |"),
            vec!["helldivers", "绝地潜兵", "HELLDIVER"]
        );
    }

    #[test]
    fn empty_configuration_still_yields_defaults() {
        assert_eq!(title_candidates(""), DEFAULT_TITLES.to_vec());
    }

    #[test]
    fn matches_game_titles() {
        let configured = "HELLDIVERS";
        assert!(window_title_matches("HELLDIVERS 2", configured));
        assert!(window_title_matches("HELLDIVERS\u{2122} 2", configured));
        assert!(window_title_matches("  helldivers  ", configured));
        assert!(window_title_matches("HELLDIVERS2", configured));
        assert!(window_title_matches("绝地潜兵 2", configured));
        assert!(window_title_matches("绝地潜兵2", configured));
    }

    #[test]
    fn rejects_lookalikes() {
        let configured = "HELLDIVERS";
        assert!(!window_title_matches(
            "HELLDIVERS 2 - Google Chrome",
            configured
        ));
        assert!(!window_title_matches(
            "HELLDIVERS 2 攻略 - bilibili",
            configured
        ));
        assert!(!window_title_matches("HELLDIVERS 3", configured));
        assert!(!window_title_matches("", configured));
        assert!(!window_title_matches("   ", configured));
    }

    #[test]
    fn custom_titles_only_accept_exact_form() {
        let configured = "MyGame";
        assert!(window_title_matches("MyGame", configured));
        assert!(window_title_matches("mygame", configured));
        // 自定义标题不享受内置标题的 "+2" 放宽。
        assert!(!window_title_matches("MyGame2", configured));
    }
}
