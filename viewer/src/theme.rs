//! Semantic colors shared by every observer surface.
//!
//! Indexed xterm colors keep the same hierarchy over SSH, tmux and terminals
//! that do not advertise truecolor support.

use rataflow::{Palette, Theme};
use ratatui::style::Color;

pub const CANVAS: Color = Color::Indexed(233);
pub const SURFACE: Color = Color::Indexed(234);
pub const SURFACE_RAISED: Color = Color::Indexed(235);
pub const GRID: Color = Color::Indexed(237);
pub const BORDER: Color = Color::Indexed(238);
pub const MUTED: Color = Color::Indexed(240);
pub const SUBTLE: Color = Color::Indexed(244);
pub const TEXT: Color = Color::Indexed(252);
pub const BRIGHT_TEXT: Color = Color::Indexed(255);

pub const GOLD: Color = Color::Indexed(220);
pub const GREEN: Color = Color::Indexed(71);
pub const AMBER: Color = Color::Indexed(214);
pub const RED: Color = Color::Indexed(167);
pub const CODEX_TEAL: Color = Color::Indexed(36);
pub const CLAUDE_ORANGE: Color = Color::Indexed(173);

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum StateTone {
    Live,
    Done,
    Waiting,
    Failed,
    #[default]
    Idle,
}

pub fn state_tone(state: &str) -> StateTone {
    match state {
        "running" | "active" | "claimed" | "launching" => StateTone::Live,
        "completed" | "accepted" => StateTone::Done,
        "pending" | "queued" | "awaiting_codex" | "awaiting_leader" => StateTone::Waiting,
        "failed" | "unknown" | "cancelled" | "blocked" | "launch_failed" => StateTone::Failed,
        _ => StateTone::Idle,
    }
}

pub fn state_color(state: &str) -> Color {
    match state_tone(state) {
        StateTone::Live => GREEN,
        StateTone::Done => GOLD,
        StateTone::Waiting => AMBER,
        StateTone::Failed => RED,
        StateTone::Idle => SUBTLE,
    }
}

pub fn flow_palette() -> Palette {
    let mut palette = Theme::Dark.palette();
    palette.accent = GOLD;
    palette.canvas_bg = CANVAS;
    palette.surface = SURFACE;
    palette.muted = BORDER;
    palette.subtle = SUBTLE;
    palette.text = TEXT;
    palette.success = GREEN;
    palette.error = RED;
    palette
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lifecycle_colors_remain_semantically_distinct() {
        assert_eq!(state_color("running"), GREEN);
        assert_eq!(state_color("completed"), GOLD);
        assert_eq!(state_color("queued"), AMBER);
        assert_eq!(state_color("failed"), RED);
        assert_eq!(state_color("idle"), SUBTLE);
    }
}
