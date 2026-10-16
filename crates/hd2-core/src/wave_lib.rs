use crate::config::{WaveEntry, WaveLibConfig};
use crate::events::positive_float;
use crate::rounding::round_ties_even;
use crate::waves;
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;

pub const FORMAT_ID: &str = "hd2-coyote-wave-lib/1";
pub const UNIT_CHARS: usize = 16;
pub const NAME_MAX: usize = 64;

pub fn normalize_unit(input: &str) -> Result<String, String> {
    let mut text = input.trim().replace([' ', ',', '-', ':'], "");
    if text
        .get(..2)
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case("0x"))
    {
        text.drain(..2);
    }
    let normalized = text.to_ascii_uppercase();
    if normalized.len() == UNIT_CHARS && normalized.bytes().all(|b| b.is_ascii_hexdigit()) {
        Ok(normalized)
    } else {
        Err("pulse unit must contain exactly 16 hexadecimal characters".into())
    }
}

pub fn normalize_units<I, S>(values: I) -> Vec<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    values
        .into_iter()
        .filter_map(|value| normalize_unit(value.as_ref()).ok())
        .collect()
}

pub fn parse_unit(input: &str) -> Result<Vec<(u8, u8)>, String> {
    let unit = normalize_unit(input)?;
    (0..4)
        .map(|index| {
            Ok((
                u8::from_str_radix(&unit[index * 2..index * 2 + 2], 16)
                    .map_err(|err| err.to_string())?,
                u8::from_str_radix(&unit[8 + index * 2..10 + index * 2], 16)
                    .map_err(|err| err.to_string())?,
            ))
        })
        .collect()
}

#[derive(Debug, Clone, Default)]
pub struct WaveLibrary {
    pub entries: BTreeMap<String, WaveEntry>,
}

impl WaveLibrary {
    pub fn new(config: &WaveLibConfig) -> Self {
        Self {
            entries: config.entries.clone(),
        }
    }

    pub fn get(&self, name: &str) -> Option<&WaveEntry> {
        self.entries.get(name)
    }

    pub fn names(&self) -> impl Iterator<Item = &String> {
        self.entries.keys()
    }

    pub fn set_entry(&mut self, name: &str, mut entry: WaveEntry) -> Result<&WaveEntry, String> {
        let key = name.trim();
        if key.is_empty() || key.chars().count() > NAME_MAX {
            return Err("wave name must contain 1..64 characters".into());
        }
        if let Some(bad) = entry
            .units
            .iter()
            .find(|unit| normalize_unit(unit).is_err())
        {
            return Err(format!("invalid pulse unit: {bad}"));
        }
        entry.units = normalize_units(&entry.units);
        if !entry.preset.is_empty() && !waves::is_preset(&entry.preset) {
            return Err(format!("unknown preset: {}", entry.preset));
        }
        if !entry.preset.is_empty() {
            entry.units.clear();
        } else if !entry.units.is_empty() {
            entry.preset.clear();
        } else {
            entry.preset = "pinch".into();
        }
        entry.freq = entry.freq.filter(|value| value.is_finite() && *value > 0.0);
        entry.peak = if entry.peak.is_finite() {
            entry.peak.clamp(0.0, 100.0)
        } else {
            100.0
        };
        entry.default_ms = if entry.default_ms.is_finite() {
            entry.default_ms.clamp(0.0, 60_000.0)
        } else {
            0.0
        };
        entry.note = entry.note.chars().take(200).collect();
        self.entries.insert(key.into(), entry);
        Ok(&self.entries[key])
    }

    pub fn remove(&mut self, name: &str) -> bool {
        self.entries.remove(name).is_some()
    }

    /// Resolve a library entry. Custom units repeat; preset entries use caller overrides.
    pub fn build(
        &self,
        name: &str,
        duration_ms: f64,
        peak: f64,
        freq: Option<f64>,
    ) -> Option<Vec<String>> {
        let entry = self.entries.get(name)?;
        let units = normalize_units(&entry.units);
        if !units.is_empty() {
            let count = waves::units_for(duration_ms);
            return Some(
                (0..count)
                    .map(|index| units[index % units.len()].clone())
                    .collect(),
            );
        }
        waves::build(
            if entry.preset.is_empty() {
                "pinch"
            } else {
                &entry.preset
            },
            duration_ms,
            peak,
            freq.or(entry.freq),
        )
        .ok()
    }

    pub fn to_config(&self) -> WaveLibConfig {
        WaveLibConfig {
            entries: self.entries.clone(),
        }
    }

    pub fn export_value(&self) -> Value {
        let mut pulse_data = Map::new();
        let mut punish_time = Map::new();
        let mut presets = Map::new();
        for (name, entry) in &self.entries {
            let units = normalize_units(&entry.units);
            if !units.is_empty() {
                pulse_data.insert(name.clone(), json!(units));
                if entry.default_ms > 0.0 {
                    let seconds = round_ties_even(entry.default_ms) as f64 / 1000.0;
                    punish_time.insert(name.clone(), json!(seconds));
                }
            } else {
                presets.insert(
                    name.clone(),
                    json!({
                        "preset": if entry.preset.is_empty() { "pinch" } else { &entry.preset },
                        "freq": entry.freq,
                        "peak": entry.peak,
                        "default_ms": entry.default_ms,
                        "note": entry.note,
                    }),
                );
            }
        }
        json!({
            "format": FORMAT_ID,
            "note": "pulse_data 与 DG-Lab-Punishment 的格式互通",
            "pulse_data": pulse_data,
            "punish_time": punish_time,
            "presets": presets,
        })
    }

    pub fn import_value(&mut self, value: &Value, replace: bool) -> Result<usize, String> {
        let object = value
            .as_object()
            .ok_or_else(|| "import JSON must be an object".to_string())?;
        // Python uses `pulse_data or waves or entries`: select the first
        // truthy value, then require it to be an object.  In particular an empty
        // pulse_data map falls through to waves, while a truthy non-object does
        // not fall through and instead triggers the direct-map compatibility path.
        let payload = ["pulse_data", "waves", "entries"]
            .iter()
            .filter_map(|key| object.get(*key))
            .find(|value| python_truthy(value))
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_else(|| {
                object
                    .iter()
                    .filter(|(_, value)| {
                        value.is_array()
                            || value.is_string()
                            || value.as_object().is_some_and(|entry| {
                                entry.contains_key("units") || entry.contains_key("preset")
                            })
                    })
                    .map(|(key, value)| (key.clone(), value.clone()))
                    .collect()
            });
        let times = object.get("punish_time").and_then(Value::as_object);
        let mut merged = object
            .get("presets")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        merged.extend(payload);

        let mut accepted = Vec::new();
        for (raw_name, raw_entry) in merged {
            let name = raw_name.trim();
            if name.is_empty() || matches!(name, "format" | "note" | "version") {
                continue;
            }
            let Some(mut entry) = entry_from_value(&raw_entry) else {
                continue;
            };
            if entry.default_ms <= 0.0 {
                if let Some(value) = times.and_then(|map| map.get(&raw_name)) {
                    entry.default_ms = positive_float(value, 0.0).max(0.0) * 1000.0;
                }
            }
            let truncated: String = name.chars().take(NAME_MAX).collect();
            accepted.push((truncated, entry));
        }
        if replace {
            self.entries.clear();
        }
        let imported = accepted.len();
        // Python's importer intentionally bypasses set_entry: imported files retain
        // out-of-range metadata and unknown object-form presets verbatim.
        for (name, entry) in accepted {
            self.entries.insert(name, entry);
        }
        Ok(imported)
    }
}

fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(number) => number.as_f64().is_some_and(|value| value != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(object) => !object.is_empty(),
    }
}

fn python_string(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::String(text) => text.clone(),
        Value::Number(number) => number.to_string(),
        Value::Array(_) | Value::Object(_) => value.to_string(),
    }
}

fn units_from_value(value: Option<&Value>) -> Vec<String> {
    value
        .and_then(Value::as_array)
        .map(|array| normalize_units(array.iter().map(python_string)))
        .unwrap_or_default()
}

fn entry_from_value(value: &Value) -> Option<WaveEntry> {
    if let Some(array) = value.as_array() {
        let units = normalize_units(array.iter().map(python_string));
        return (!units.is_empty()).then(|| WaveEntry {
            units,
            ..Default::default()
        });
    }
    if let Some(text) = value.as_str() {
        if let Ok(unit) = normalize_unit(text) {
            return Some(WaveEntry {
                units: vec![unit],
                ..Default::default()
            });
        }
        return waves::is_preset(text).then(|| WaveEntry {
            preset: text.into(),
            ..Default::default()
        });
    }
    let object = value.as_object()?;
    let units_source = object
        .get("units")
        .filter(|value| python_truthy(value))
        .or_else(|| {
            object
                .get("pulse_data")
                .filter(|value| python_truthy(value))
        });
    let units = units_from_value(units_source);
    let preset = object
        .get("preset")
        .filter(|value| python_truthy(value))
        .map(python_string)
        .unwrap_or_default()
        .trim()
        .to_string();
    if units.is_empty() && preset.is_empty() {
        return None;
    }
    let freq = object
        .get("freq")
        .filter(|value| !value.is_null())
        .map(|value| positive_float(value, 0.0))
        .filter(|value| *value > 0.0);
    let peak = object
        .get("peak")
        .map(|value| positive_float(value, 100.0))
        .unwrap_or(100.0);
    let default_source = object
        .get("default_ms")
        .filter(|value| python_truthy(value))
        .or_else(|| object.get("ms"));
    let default_ms = default_source
        .map(|value| positive_float(value, 0.0))
        .unwrap_or(0.0);
    let note = object
        .get("note")
        .filter(|value| python_truthy(value))
        .map(python_string)
        .unwrap_or_default()
        .chars()
        .take(200)
        .collect();
    let has_units = !units.is_empty();
    Some(WaveEntry {
        units,
        preset: if has_units { String::new() } else { preset },
        freq,
        peak,
        default_ms,
        note,
    })
}
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_and_parse() {
        assert_eq!(
            normalize_unit("0x0a0a-0a0a:6464,6464").unwrap(),
            "0A0A0A0A64646464"
        );
        assert_eq!(parse_unit("1E1E1E1E3C3C3C3C").unwrap()[0], (30, 60));
    }

    #[test]
    fn custom_units_cycle_to_duration() {
        let mut library = WaveLibrary::default();
        library
            .set_entry(
                "x",
                WaveEntry {
                    units: vec!["1414141464646464".into()],
                    ..Default::default()
                },
            )
            .unwrap();
        assert_eq!(library.build("x", 300.0, 100.0, None).unwrap().len(), 3);
    }

    #[test]
    fn preset_export_import_round_trip() {
        let mut library = WaveLibrary::default();
        library
            .set_entry(
                "named",
                WaveEntry {
                    preset: "death".into(),
                    peak: 71.0,
                    default_ms: 333.0,
                    ..Default::default()
                },
            )
            .unwrap();
        let exported = library.export_value();
        let mut restored = WaveLibrary::default();
        assert_eq!(restored.import_value(&exported, true).unwrap(), 1);
        assert_eq!(restored.get("named").unwrap().preset, "death");
        assert_eq!(restored.get("named").unwrap().peak, 71.0);
    }

    #[test]
    fn import_uses_python_truthy_payload_chain() {
        let mut library = WaveLibrary::default();
        let data = json!({
            "pulse_data": {},
            "waves": {"fallback": ["1414141464646464"]},
            "entries": {"not_reached": ["1E1E1E1E3C3C3C3C"]}
        });
        assert_eq!(library.import_value(&data, true).unwrap(), 1);
        assert!(library.get("fallback").is_some());
        assert!(library.get("not_reached").is_none());
    }

    #[test]
    fn object_import_matches_python_coercion_and_bypasses_set_entry() {
        let mut library = WaveLibrary::default();
        let data = json!({
            "pulse_data": {
                "custom": {
                    "units": [],
                    "pulse_data": ["1414141464646464"],
                    "preset": "death",
                    "freq": "12.5",
                    "peak": 150,
                    "default_ms": 0,
                    "ms": 777,
                    "note": 12
                },
                "future": {
                    "preset": "future_preset",
                    "peak": 150,
                    "default_ms": 70000
                }
            },
            "punish_time": {"custom": "1.5"}
        });
        assert_eq!(library.import_value(&data, true).unwrap(), 2);

        let custom = library.get("custom").unwrap();
        assert_eq!(custom.units, vec!["1414141464646464"]);
        assert!(custom.preset.is_empty());
        assert_eq!(custom.freq, Some(12.5));
        assert_eq!(custom.peak, 150.0);
        assert_eq!(custom.default_ms, 777.0);
        assert_eq!(custom.note, "12");

        let future = library.get("future").unwrap();
        assert_eq!(future.preset, "future_preset");
        assert_eq!(future.peak, 150.0);
        assert_eq!(future.default_ms, 70000.0);
    }

    #[test]
    fn punish_time_accepts_python_float_inputs() {
        let mut library = WaveLibrary::default();
        let data = json!({
            "pulse_data": {"custom": ["1414141464646464"]},
            "punish_time": {"custom": "1.5"}
        });
        assert_eq!(library.import_value(&data, true).unwrap(), 1);
        assert_eq!(library.get("custom").unwrap().default_ms, 1500.0);
    }
}
