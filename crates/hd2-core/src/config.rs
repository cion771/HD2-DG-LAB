use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

macro_rules! cfg { ($name:ident { $($field:ident : $ty:ty = $value:expr),* $(,)? }) => {
    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)] #[serde(default)]
    pub struct $name { $(pub $field: $ty),* }
    impl Default for $name { fn default() -> Self { Self { $($field: $value),* } } }
}; }

cfg!(DeviceConfig {
    kind: String = "socket".into(),
    host: String = "0.0.0.0".into(),
    port: u16 = 9999,
    channel: String = "both".into(),
    advertise_ip: String = String::new()
});
cfg!(HookConfig {
    host: String = "127.0.0.1".into(),
    port: u16 = 47777,
    timeout_s: f64 = 2.0
});
cfg!(SourcesConfig { enabled:Vec<String>=vec!["game_bridge".into()], http_host:String="127.0.0.1".into(), http_port:u16=47778, http_token:String=String::new(), http_max_per_s:f64=20.0, http_timeout_s:f64=3.0 });
cfg!(HudConfig { injury_slots:usize=3, injury_slot_names:Vec<String>=vec!["左肢".into(),"躯干".into(),"右肢".into()] });
cfg!(DetectConfig {
    damage_min_pct: f64 = 4.0,
    damage_merge_window_ms: f64 = 250.0,
    low_health_pct: f64 = 35.0,
    recover_pct: f64 = 60.0,
    dead_hold_s: f64 = 0.8,
    revive_ignore_damage_s: f64 = 2.0,
    injury_min_fraction: f64 = 0.015,
    injury_clear_s: f64 = 0.5,
    bleeding_min_fraction: f64 = 0.01,
    death_state_threshold: f64 = 0.72
});
cfg!(SafetyConfig {
    max_pct: f64 = 30.0,
    max_absolute: i64 = 40,
    attack_ms: f64 = 150.0,
    release_ms: f64 = 800.0,
    max_event_ms: f64 = 8000.0,
    emergency_key: String = "F12".into(),
    pause_key: String = "F11".into(),
    mute_on_focus_loss: bool = false,
    game_window_title: String = "HELLDIVERS".into(),
    max_session_seconds: f64 = 0.0,
    master_multiplier: f64 = 1.0,
    channel_a_multiplier: f64 = 1.0,
    channel_b_multiplier: f64 = 1.0
});
cfg!(WaveEntry { units:Vec<String>=vec![], preset:String=String::new(), freq:Option<f64>=None, peak:f64=100.0, default_ms:f64=0.0, note:String=String::new() });
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct WaveLibConfig {
    pub entries: BTreeMap<String, WaveEntry>,
}
cfg!(RampConfig { enabled:bool=false, per_event:f64=2.0, hp_missing_pct:f64=20.0, ceiling_pct:f64=25.0, decay_after_s:f64=4.0, decay_per_s:f64=1.5, reset_on_death:bool=true, apply_to:Vec<String>=vec!["damage".into(),"limb_injury".into()] });
cfg!(UpdateConfig {
    enabled: bool = true,
    repo: String = "cion771/HD2-DG-LAB".into(),
    check_on_start: bool = false,
    timeout_s: f64 = 6.0
});
cfg!(RuleConfig { enabled:bool=true, base_pct:f64=20.0, wave:String="pinch".into(), duration_ms:f64=400.0, channel:String="default".into(), cooldown_ms:f64=500.0, priority:i32=1, freq:Option<f64>=None, per_10hp:f64=6.0, repeat_ms:f64=0.0, period_ms:f64=1500.0 });

pub fn default_rules() -> BTreeMap<String, RuleConfig> {
    let mut r = BTreeMap::new();
    r.insert(
        "damage".into(),
        RuleConfig {
            base_pct: 16.0,
            per_10hp: 7.0,
            wave: "pinch".into(),
            duration_ms: 400.0,
            channel: "A".into(),
            cooldown_ms: 400.0,
            ..Default::default()
        },
    );
    r.insert(
        "limb_injury".into(),
        RuleConfig {
            base_pct: 30.0,
            wave: "ramp_up".into(),
            duration_ms: 1200.0,
            channel: "both".into(),
            cooldown_ms: 1500.0,
            priority: 2,
            ..Default::default()
        },
    );
    r.insert(
        "death".into(),
        RuleConfig {
            base_pct: 45.0,
            wave: "death".into(),
            duration_ms: 4000.0,
            channel: "both".into(),
            cooldown_ms: 5000.0,
            priority: 5,
            repeat_ms: 1500.0,
            ..Default::default()
        },
    );
    r.insert(
        "low_health".into(),
        RuleConfig {
            enabled: false,
            base_pct: 12.0,
            wave: "heartbeat".into(),
            duration_ms: 1200.0,
            channel: "both".into(),
            cooldown_ms: 0.0,
            priority: 0,
            period_ms: 1500.0,
            ..Default::default()
        },
    );
    r
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct AppConfig {
    pub source: String,
    pub device: DeviceConfig,
    pub hook: HookConfig,
    pub sources: SourcesConfig,
    pub hud: HudConfig,
    pub detect: DetectConfig,
    pub safety: SafetyConfig,
    pub waves: WaveLibConfig,
    pub ramp: RampConfig,
    pub update: UpdateConfig,
    pub rules: BTreeMap<String, RuleConfig>,
    pub log_level: String,
}
impl Default for AppConfig {
    fn default() -> Self {
        Self {
            source: "hook".into(),
            device: Default::default(),
            hook: Default::default(),
            sources: Default::default(),
            hud: Default::default(),
            detect: Default::default(),
            safety: Default::default(),
            waves: Default::default(),
            ramp: Default::default(),
            update: Default::default(),
            rules: default_rules(),
            log_level: "INFO".into(),
        }
    }
}
fn python_string(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(text) => text.clone(),
        Value::Number(number) => number.to_string(),
        // These forms are uncommon in config, but preserving their JSON is closer than rejecting them.
        Value::Array(_) | Value::Object(_) => value.to_string(),
    }
}

fn normalize_python_string_list(value: &mut Value) {
    *value = match value {
        Value::Array(items) => Value::Array(
            items
                .iter()
                .map(|item| Value::String(python_string(item)))
                .collect(),
        ),
        _ => Value::Array(Vec::new()),
    };
}

impl AppConfig {
    pub fn from_json_str(text: &str) -> Result<Self, serde_json::Error> {
        let mut v: Value = serde_json::from_str(text)?;
        if let Some(root) = v.as_object_mut() {
            root.insert("source".into(), Value::String("hook".into()));
            if let Some(Value::Object(d)) = root.get_mut("detect") {
                if !d.contains_key("death_state_threshold") {
                    if let Some(x) = d.get("death_template_threshold").cloned() {
                        d.insert("death_state_threshold".into(), x);
                    }
                }
            }
            if let Some(Value::Object(sources)) = root.get_mut("sources") {
                if let Some(enabled) = sources.get_mut("enabled") {
                    normalize_python_string_list(enabled);
                    if enabled.as_array().is_some_and(Vec::is_empty) {
                        *enabled = serde_json::json!(["game_bridge"]);
                    }
                }
            }
            if let Some(Value::Object(waves)) = root.get_mut("waves") {
                if let Some(Value::Object(entries)) = waves.get_mut("entries") {
                    entries.retain(|_, entry| entry.is_object());
                    for entry in entries.values_mut().filter_map(Value::as_object_mut) {
                        if let Some(units) = entry.get_mut("units") {
                            normalize_python_string_list(units);
                        }
                        if let Some(freq) = entry.get_mut("freq") {
                            if !freq.is_null() && !freq.is_number() {
                                let number = match freq {
                                    Value::String(text) => text.parse::<f64>().ok(),
                                    Value::Bool(value) => Some(if *value { 1.0 } else { 0.0 }),
                                    _ => None,
                                };
                                if let Some(number) = number.and_then(serde_json::Number::from_f64)
                                {
                                    *freq = Value::Number(number);
                                }
                            }
                        }
                    }
                }
            }
        }
        let mut c: Self = serde_json::from_value(v)?;
        if c.sources.enabled.is_empty() {
            c.sources.enabled = vec!["game_bridge".into()]
        };
        Ok(c)
    }
    pub fn to_json_pretty(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string_pretty(self)
    }
}
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_defaults_and_names() {
        let c = AppConfig::default();
        assert!(c.rules.contains_key("damage"));
        let v = serde_json::to_value(&c).unwrap();
        assert_eq!(v["device"]["host"], "0.0.0.0");
        assert_eq!(v["hook"]["host"], "127.0.0.1");
        assert_eq!(v["sources"]["http_host"], "127.0.0.1");
        assert_eq!(v["sources"]["http_max_per_s"], 20.0);
        assert_eq!(
            v["hud"]["injury_slot_names"],
            serde_json::json!(["左肢", "躯干", "右肢"])
        );
        assert_eq!(v["waves"]["entries"], serde_json::json!({}));
    }

    #[test]
    fn legacy_alias_and_source() {
        let c = AppConfig::from_json_str(
            r#"{"source":"vision","detect":{"death_template_threshold":0.9},"sources":{"enabled":["http"]}}"#,
        )
        .unwrap();
        assert_eq!(c.source, "hook");
        assert_eq!(c.detect.death_state_threshold, 0.9);
        assert_eq!(c.sources.enabled, vec!["http"]);
    }

    #[test]
    fn python_style_list_and_wave_entry_coercion() {
        let c = AppConfig::from_json_str(
            r#"{
                "sources":{"enabled":[1,true,null]},
                "waves":{"entries":{
                    "ignored":["0A0A0A0A64646464"],
                    "kept":{"units":[1,true,null],"freq":"12.5"}
                }}
            }"#,
        )
        .unwrap();
        assert_eq!(c.sources.enabled, vec!["1", "True", "None"]);
        assert!(!c.waves.entries.contains_key("ignored"));
        assert_eq!(c.waves.entries["kept"].units, vec!["1", "True", "None"]);
        assert_eq!(c.waves.entries["kept"].freq, Some(12.5));

        let fallback = AppConfig::from_json_str(r#"{"sources":{"enabled":"http"}}"#).unwrap();
        assert_eq!(fallback.sources.enabled, vec!["game_bridge"]);
    }
}
