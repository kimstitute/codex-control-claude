use std::io;

use anyhow::{Context, Result};
use crossterm::cursor::{Hide, Show};
use crossterm::event::{
    self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, KeyEventKind, MouseButton,
    MouseEventKind,
};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Alignment, Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, List, ListItem, ListState, Paragraph};

use crate::theme::{BORDER, CANVAS, GOLD, GREEN, MUTED, SUBTLE, SURFACE, TEXT};

#[derive(Clone, Debug, Default)]
pub struct SessionChoice {
    pub selector: String,
    pub provider: String,
    pub session_id: String,
    pub title: String,
    pub project: String,
    pub file: String,
    pub live: bool,
    pub agents: u64,
    pub events: Option<u64>,
}

pub fn choose(sessions: &[SessionChoice]) -> Result<Option<String>> {
    if sessions.is_empty() {
        return Ok(None);
    }
    enable_raw_mode().context("could not enable terminal raw mode")?;
    let mut stdout = io::stdout();
    if let Err(error) = execute!(stdout, EnterAlternateScreen, EnableMouseCapture, Hide) {
        let _ = disable_raw_mode();
        return Err(error).context("could not open session picker");
    }
    let result = choose_inner(sessions);
    let _ = disable_raw_mode();
    let _ = execute!(
        io::stdout(),
        DisableMouseCapture,
        LeaveAlternateScreen,
        Show
    );
    result
}

fn choose_inner(sessions: &[SessionChoice]) -> Result<Option<String>> {
    let backend = CrosstermBackend::new(io::stdout());
    let mut terminal = Terminal::new(backend).context("could not initialize session picker")?;
    let mut filter = String::new();
    let mut selected = 0_usize;
    loop {
        let matches = matching(sessions, &filter);
        selected = selected.min(matches.len().saturating_sub(1));
        let mut list_area = Rect::default();
        let mut list_offset = 0_usize;
        terminal.draw(|frame| {
            frame.render_widget(Clear, frame.area());
            frame.render_widget(
                Block::default().style(Style::default().bg(CANVAS)),
                frame.area(),
            );
            let popup = centered(frame.area(), 86, 80);
            let block = Block::default()
                .title(" local sessions ")
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .border_style(Style::default().fg(GOLD))
                .style(Style::default().bg(SURFACE));
            let inner = block.inner(popup);
            frame.render_widget(block, popup);
            let [search, list, footer] = Layout::vertical([
                Constraint::Length(3),
                Constraint::Fill(1),
                Constraint::Length(2),
            ])
            .areas(inner);
            list_area = list;
            frame.render_widget(
                Paragraph::new(Line::from(vec![
                    Span::styled(" / ", Style::default().fg(CANVAS).bg(GOLD)),
                    Span::styled(
                        if filter.is_empty() {
                            "type to filter"
                        } else {
                            &filter
                        },
                        Style::default().fg(if filter.is_empty() { MUTED } else { TEXT }),
                    ),
                ]))
                .block(
                    Block::default()
                        .borders(Borders::BOTTOM)
                        .border_style(Style::default().fg(BORDER)),
                ),
                search,
            );
            let items = matches
                .iter()
                .map(|index| {
                    let item = &sessions[*index];
                    let state = if item.live { "● LIVE" } else { "○ saved" };
                    let counts = item
                        .events
                        .map(|events| format!(" · {events} events"))
                        .unwrap_or_default();
                    ListItem::new(vec![
                        Line::from(vec![
                            Span::styled(
                                format!(" {:<9}", item.provider),
                                Style::default().fg(GOLD),
                            ),
                            Span::styled(
                                state,
                                Style::default().fg(if item.live { GREEN } else { MUTED }),
                            ),
                            Span::styled(
                                format!(" · {} agent{counts}", item.agents),
                                Style::default().fg(SUBTLE),
                            ),
                        ]),
                        Line::from(vec![
                            Span::styled("   ", Style::default()),
                            Span::styled(
                                if item.title.is_empty() {
                                    &item.session_id
                                } else {
                                    &item.title
                                },
                                Style::default().fg(TEXT).add_modifier(Modifier::BOLD),
                            ),
                        ]),
                        Line::from(Span::styled(
                            format!("   {}", item.project),
                            Style::default().fg(SUBTLE),
                        )),
                    ])
                })
                .collect::<Vec<_>>();
            let mut state =
                ListState::default().with_selected((!matches.is_empty()).then_some(selected));
            frame.render_stateful_widget(
                List::new(items)
                    .highlight_style(
                        Style::default()
                            .fg(CANVAS)
                            .bg(GOLD)
                            .add_modifier(Modifier::BOLD),
                    )
                    .highlight_symbol("▶"),
                list,
                &mut state,
            );
            list_offset = state.offset();
            frame.render_widget(
                Paragraph::new("↑↓/wheel select · click or Enter open · Esc safe observer")
                    .alignment(Alignment::Center)
                    .style(Style::default().fg(MUTED).bg(SURFACE)),
                footer,
            );
        })?;
        match event::read()? {
            Event::Key(key) if key.kind != KeyEventKind::Release => match key.code {
                KeyCode::Esc => return Ok(None),
                KeyCode::Enter if !matches.is_empty() => {
                    return Ok(Some(sessions[matches[selected]].selector.clone()));
                }
                KeyCode::Up => selected = selected.saturating_sub(1),
                KeyCode::Down => selected = (selected + 1).min(matches.len().saturating_sub(1)),
                KeyCode::Backspace => {
                    filter.pop();
                    selected = 0;
                }
                KeyCode::Char(value)
                    if !key
                        .modifiers
                        .contains(crossterm::event::KeyModifiers::CONTROL) =>
                {
                    filter.push(value);
                    selected = 0;
                }
                _ => {}
            },
            Event::Mouse(mouse) => match mouse.kind {
                MouseEventKind::ScrollUp => selected = selected.saturating_sub(1),
                MouseEventKind::ScrollDown => {
                    selected = (selected + 1).min(matches.len().saturating_sub(1));
                }
                MouseEventKind::Down(MouseButton::Left)
                    if mouse.column >= list_area.x
                        && mouse.column < list_area.right()
                        && mouse.row >= list_area.y
                        && mouse.row < list_area.bottom() =>
                {
                    let row = usize::from(mouse.row.saturating_sub(list_area.y));
                    let index = list_offset + row / 3;
                    if index < matches.len() {
                        return Ok(Some(sessions[matches[index]].selector.clone()));
                    }
                }
                _ => {}
            },
            _ => {}
        }
    }
}

fn matching(sessions: &[SessionChoice], filter: &str) -> Vec<usize> {
    let needle = filter.trim().to_lowercase();
    sessions
        .iter()
        .enumerate()
        .filter(|(_, session)| {
            needle.is_empty()
                || [
                    &session.provider,
                    &session.session_id,
                    &session.title,
                    &session.project,
                ]
                .iter()
                .any(|value| value.to_lowercase().contains(&needle))
        })
        .map(|(index, _)| index)
        .collect()
}

fn centered(area: Rect, width: u16, height: u16) -> Rect {
    let horizontal = Layout::horizontal([
        Constraint::Percentage((100 - width) / 2),
        Constraint::Percentage(width),
        Constraint::Percentage((100 - width) / 2),
    ])
    .split(area);
    Layout::vertical([
        Constraint::Percentage((100 - height) / 2),
        Constraint::Percentage(height),
        Constraint::Percentage((100 - height) / 2),
    ])
    .split(horizontal[1])[1]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matching_is_case_insensitive_across_visible_fields() {
        let sessions = vec![SessionChoice {
            provider: "Claude".into(),
            project: "Viewer".into(),
            ..SessionChoice::default()
        }];
        assert_eq!(matching(&sessions, "viewer"), vec![0]);
        assert_eq!(matching(&sessions, "CODEX"), Vec::<usize>::new());
    }
}
