//! What a harness says about its own run: the model, the permission mode, and
//! the tokens it reports. Read from the same transcript rows the mirror already
//! reads, never from anywhere else; a harness that does not write a fact leaves
//! it unknown rather than guessed.
use super::*;

/// Recent Claude message ids already counted. One API message is written as
/// several rows (one per content block) that repeat its usage.
const SEEN_MAX: usize = 64;

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct Usage {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cache_read_tokens: u64,
    pub cache_write_tokens: u64,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct Stats {
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub mode: Option<String>,
    #[serde(default)]
    pub usage: Option<Usage>,
    #[serde(default)]
    seen: Vec<String>,
}

fn label(value: &Value, max: usize) -> Option<String> {
    value
        .as_str()
        .map(str::trim)
        .filter(|s| !s.is_empty() && s.len() <= max && s.chars().all(|c| c.is_ascii_graphic()))
        .map(str::to_owned)
}

fn count(value: &Value) -> u64 {
    value.as_u64().unwrap_or(0)
}

impl Stats {
    pub(crate) fn observe(&mut self, provider: &str, row: &Value) {
        if provider == "claude" {
            self.claude(row)
        } else {
            self.codex(row)
        }
    }

    fn claude(&mut self, row: &Value) {
        match row["type"].as_str() {
            Some("permission-mode") => {
                if let Some(mode) = label(&row["permissionMode"], 32) {
                    self.mode = Some(mode);
                }
            }
            Some("assistant") => {
                let message = &row["message"];
                if let Some(model) = label(&message["model"], 64).filter(|m| !m.starts_with('<')) {
                    self.model = Some(model);
                }
                let usage = &message["usage"];
                let Some(id) = message["id"].as_str().filter(|_| usage.is_object()) else {
                    return;
                };
                if self.seen.iter().any(|seen| seen == id) {
                    return;
                }
                self.seen.push(id.to_owned());
                if self.seen.len() > SEEN_MAX {
                    self.seen.remove(0);
                }
                let total = self.usage.get_or_insert_with(Usage::default);
                total.input_tokens += count(&usage["input_tokens"]);
                total.output_tokens += count(&usage["output_tokens"]);
                total.cache_read_tokens += count(&usage["cache_read_input_tokens"]);
                total.cache_write_tokens += count(&usage["cache_creation_input_tokens"]);
            }
            _ => {}
        }
    }

    fn codex(&mut self, row: &Value) {
        let payload = &row["payload"];
        match (row["type"].as_str(), payload["type"].as_str()) {
            (Some("turn_context"), _) => {
                if let Some(model) = label(&payload["model"], 64) {
                    self.model = Some(model);
                }
            }
            (Some("event_msg"), Some("task_started")) => {
                if let Some(mode) = label(&payload["collaboration_mode_kind"], 32) {
                    self.mode = Some(mode);
                }
            }
            (Some("event_msg"), Some("token_count")) => {
                // Codex reports running totals: the latest one is the session's.
                let total = &payload["info"]["total_token_usage"];
                if total.is_object() {
                    self.usage = Some(Usage {
                        input_tokens: count(&total["input_tokens"])
                            .saturating_sub(count(&total["cached_input_tokens"])),
                        output_tokens: count(&total["output_tokens"]),
                        cache_read_tokens: count(&total["cached_input_tokens"]),
                        cache_write_tokens: count(&total["cache_write_input_tokens"]),
                    });
                }
            }
            _ => {}
        }
    }

    /// The heartbeat's `stats`, or nothing when the harness said nothing.
    pub(crate) fn wire(&self) -> Option<Value> {
        if self.model.is_none() && self.mode.is_none() && self.usage.is_none() {
            return None;
        }
        Some(json!({"model": self.model, "permissionMode": self.mode, "usage": self.usage}))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_sessions_stats_claude_reads_model_mode_and_counts_each_message_once() {
        let mut stats = Stats::default();
        let assistant = |id: &str, model: &str| {
            json!({"type":"assistant","message":{"id":id,"model":model,
                "usage":{"input_tokens":4,"output_tokens":55,"cache_read_input_tokens":900,"cache_creation_input_tokens":615}}})
        };
        stats.observe(
            "claude",
            &json!({"type":"permission-mode","permissionMode":"acceptEdits"}),
        );
        stats.observe("claude", &assistant("msg_1", "claude-opus-5-5"));
        stats.observe("claude", &assistant("msg_1", "claude-opus-5-5")); // the same message's next block
        stats.observe("claude", &assistant("msg_2", "<synthetic>"));
        assert_eq!(
            stats.model.as_deref(),
            Some("claude-opus-5-5"),
            "a synthetic row is not a model"
        );
        assert_eq!(stats.mode.as_deref(), Some("acceptEdits"));
        assert_eq!(
            stats.usage,
            Some(Usage {
                input_tokens: 8,
                output_tokens: 110,
                cache_read_tokens: 1800,
                cache_write_tokens: 1230
            })
        );
        assert_eq!(
            stats.wire().unwrap(),
            json!({"model":"claude-opus-5-5","permissionMode":"acceptEdits",
                "usage":{"inputTokens":8,"outputTokens":110,"cacheReadTokens":1800,"cacheWriteTokens":1230}})
        );
    }

    #[test]
    fn local_sessions_stats_codex_takes_the_latest_running_total() {
        let mut stats = Stats::default();
        let tokens = |input: u64, cached: u64, output: u64| {
            json!({"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":
                {"input_tokens":input,"cached_input_tokens":cached,"output_tokens":output,"total_tokens":input + output}}}})
        };
        stats.observe(
            "codex",
            &json!({"type":"turn_context","payload":{"model":"gpt-5.5-codex"}}),
        );
        stats.observe("codex", &json!({"type":"event_msg","payload":{"type":"task_started","collaboration_mode_kind":"plan"}}));
        stats.observe("codex", &tokens(100, 40, 10));
        stats.observe("codex", &tokens(300, 200, 30));
        assert_eq!(stats.model.as_deref(), Some("gpt-5.5-codex"));
        assert_eq!(stats.mode.as_deref(), Some("plan"));
        assert_eq!(
            stats.usage,
            Some(Usage {
                input_tokens: 100,
                output_tokens: 30,
                cache_read_tokens: 200,
                cache_write_tokens: 0
            })
        );
    }

    #[test]
    fn local_sessions_stats_say_nothing_until_the_harness_does_and_refuse_odd_labels() {
        let mut stats = Stats::default();
        assert!(stats.wire().is_none());
        stats.observe(
            "claude",
            &json!({"type":"permission-mode","permissionMode":"a\nb"}),
        );
        stats.observe(
            "claude",
            &json!({"type":"assistant","message":{"model":"x".repeat(65)}}),
        );
        assert!(stats.wire().is_none());
        for i in 0..(SEEN_MAX + 5) {
            stats.observe("claude", &json!({"type":"assistant","message":{"id":format!("m{i}"),"usage":{"output_tokens":1}}}));
        }
        assert_eq!(stats.seen.len(), SEEN_MAX);
        assert_eq!(stats.usage.unwrap().output_tokens, (SEEN_MAX + 5) as u64);
    }
}
