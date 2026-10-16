pub const MAX_FRAME_BYTES: usize = 1950;
pub const SAFE_FRAME_BYTES: usize = 1880;
pub const MAX_PULSE_UNITS: usize = 100;

pub fn strength_command(channel: char, mode: u8, value: i32) -> Option<String> {
    let side = match channel.to_ascii_uppercase() {
        'A' => 1,
        'B' => 2,
        _ => return None,
    };
    Some(format!("strength-{side}+{mode}+{}", value.clamp(0, 200)))
}

pub fn clear_command(channel: char) -> Option<&'static str> {
    match channel.to_ascii_uppercase() {
        'A' => Some("clear-1"),
        'B' => Some("clear-2"),
        _ => None,
    }
}

pub fn pulse_command(channel: char, units: &[String]) -> Option<String> {
    let side = channel.to_ascii_uppercase();
    if !matches!(side, 'A' | 'B')
        || units.is_empty()
        || units.len() > MAX_PULSE_UNITS
        || units
            .iter()
            .any(|u| u.len() != 16 || !u.bytes().all(|b| b.is_ascii_hexdigit()))
    {
        return None;
    }
    let json = serde_json::to_string(units).ok()?;
    let command = format!("pulse-{side}:{json}");
    (command.len() <= SAFE_FRAME_BYTES).then_some(command)
}

pub fn qr_url(ws_url: &str) -> String {
    format!("https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#{ws_url}")
}
