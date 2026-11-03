//! Bounded projection of large Codex tool-image output. The physical JSON row is
//! capped by the caller; serde may temporarily hold one image string (up to that
//! 64 MiB cap), but image bytes are never copied into the retained transcript.
use serde::de::{self, DeserializeSeed, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Value};
use std::fmt;

#[derive(Clone, Copy)]
enum Path {
    Root,
    Payload,
    Output,
    Block,
    Image,
    Other,
}
struct Budget {
    bytes: usize,
    nodes: usize,
    images: usize,
}
impl Budget {
    fn charge<E: de::Error>(&mut self, bytes: usize) -> Result<(), E> {
        self.bytes = self.bytes.saturating_add(bytes);
        self.nodes += 1;
        if self.bytes > super::MAX_JSONL_ROW || self.nodes > 65536 {
            return Err(E::custom("oversized retained image row"));
        }
        Ok(())
    }
}
struct Seed<'a> {
    budget: &'a mut Budget,
    path: Path,
}
impl<'de> DeserializeSeed<'de> for Seed<'_> {
    type Value = Value;
    fn deserialize<D: de::Deserializer<'de>>(self, d: D) -> Result<Value, D::Error> {
        d.deserialize_any(self)
    }
}
impl<'de> Visitor<'de> for Seed<'_> {
    type Value = Value;
    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("bounded JSON")
    }
    fn visit_bool<E: de::Error>(self, v: bool) -> Result<Value, E> {
        self.budget.charge::<E>(5)?;
        Ok(Value::Bool(v))
    }
    fn visit_unit<E: de::Error>(self) -> Result<Value, E> {
        self.budget.charge::<E>(4)?;
        Ok(Value::Null)
    }
    fn visit_i64<E: de::Error>(self, v: i64) -> Result<Value, E> {
        self.budget.charge::<E>(24)?;
        Ok(v.into())
    }
    fn visit_u64<E: de::Error>(self, v: u64) -> Result<Value, E> {
        self.budget.charge::<E>(24)?;
        Ok(v.into())
    }
    fn visit_f64<E: de::Error>(self, v: f64) -> Result<Value, E> {
        self.budget.charge::<E>(24)?;
        serde_json::Number::from_f64(v)
            .map(Value::Number)
            .ok_or_else(|| E::custom("invalid number"))
    }
    fn visit_str<E: de::Error>(self, v: &str) -> Result<Value, E> {
        if matches!(self.path, Path::Image) && v.starts_with("data:image/") {
            let data = ["jpeg", "png", "gif", "webp"]
                .iter()
                .find_map(|mime| v.strip_prefix(&format!("data:image/{mime};base64,")))
                .ok_or_else(|| E::custom("unknown image MIME"))?;
            let raw = data.trim_end_matches('=');
            if data.is_empty()
                || data.len() % 4 != 0
                || data.len() - raw.len() > 2
                || !raw
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'+' || b == b'/')
            {
                return Err(E::custom("invalid image base64"));
            }
            self.budget.images += 1;
            self.budget.charge::<E>(24)?;
            return Ok(Value::String("[image data omitted]".into()));
        }
        let escaped = v
            .bytes()
            .map(|b| match b {
                b'"' | b'\\' | b'\n' | b'\r' | b'\t' | 8 | 12 => 2,
                0..=31 => 6,
                _ => 1,
            })
            .sum::<usize>();
        self.budget.charge::<E>(escaped + 3)?;
        Ok(Value::String(v.to_owned()))
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Value, A::Error> {
        self.budget.charge::<A::Error>(2)?;
        let mut values = Vec::new();
        while let Some(value) = seq.next_element_seed(Seed {
            budget: self.budget,
            path: if matches!(self.path, Path::Output) {
                Path::Block
            } else {
                Path::Other
            },
        })? {
            values.push(value);
        }
        Ok(Value::Array(values))
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Value, A::Error> {
        self.budget.charge::<A::Error>(2)?;
        let before = self.budget.images;
        let mut values = Map::new();
        while let Some(key) = map.next_key_seed(Seed {
            budget: self.budget,
            path: Path::Other,
        })? {
            let key = key
                .as_str()
                .ok_or_else(|| de::Error::custom("nonstring key"))?
                .to_owned();
            if values.contains_key(&key) {
                return Err(de::Error::custom("duplicate key"));
            }
            let path = match (self.path, key.as_str()) {
                (Path::Root, "payload") => Path::Payload,
                (Path::Payload, "output") => Path::Output,
                (Path::Block, "image_url") => Path::Image,
                _ => Path::Other,
            };
            values.insert(
                key,
                map.next_value_seed(Seed {
                    budget: self.budget,
                    path,
                })?,
            );
        }
        if matches!(self.path, Path::Block)
            && before != self.budget.images
            && values.get("type").and_then(Value::as_str) != Some("input_image")
        {
            return Err(de::Error::custom("image in wrong block"));
        }
        Ok(Value::Object(values))
    }
}
pub(super) fn read<'de, D: de::Deserializer<'de>>(d: D) -> Result<Value, D::Error> {
    let mut budget = Budget {
        bytes: 0,
        nodes: 0,
        images: 0,
    };
    let row = Seed {
        budget: &mut budget,
        path: Path::Root,
    }
    .deserialize(d)?;
    let payload = &row["payload"];
    let valid = row["type"] == "response_item"
        && payload["type"] == "custom_tool_call_output"
        && payload["call_id"].as_str().is_some_and(|s| !s.is_empty())
        && payload["output"].as_array().is_some_and(|blocks| {
            !blocks.is_empty()
                && blocks.len() <= 64
                && blocks.iter().all(|b| match b["type"].as_str() {
                    Some("input_text") => b["text"].is_string(),
                    Some("input_image") => b["image_url"].is_string(),
                    _ => false,
                })
        })
        && budget.images > 0;
    if !valid || serde_json::to_vec(&row).map_err(de::Error::custom)?.len() > super::MAX_JSONL_ROW {
        return Err(de::Error::custom("unsupported oversized row"));
    }
    Ok(row)
}
