//! Pure geometry helpers for terminal mouse interaction.
//!
//! Rectangles use ratatui's half-open convention: the left and top edges are
//! included, while `right()` and `bottom()` are excluded. Camera coordinates
//! identify the world point drawn at the viewport's top-left corner.

use ratatui::layout::Rect;

/// A position in terminal-cell coordinates.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct ScreenPoint {
    pub x: f32,
    pub y: f32,
}

impl ScreenPoint {
    pub const fn new(x: f32, y: f32) -> Self {
        Self { x, y }
    }
}

/// A position or delta in graph-world coordinates.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct WorldPoint {
    pub x: f32,
    pub y: f32,
}

impl WorldPoint {
    pub const fn new(x: f32, y: f32) -> Self {
        Self { x, y }
    }
}

/// Inclusive world-space bounds used by the minimap.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct WorldBounds {
    pub min: WorldPoint,
    pub max: WorldPoint,
}

impl WorldBounds {
    pub const fn new(min: WorldPoint, max: WorldPoint) -> Self {
        Self { min, max }
    }

    fn is_valid(self) -> bool {
        self.min.x.is_finite()
            && self.min.y.is_finite()
            && self.max.x.is_finite()
            && self.max.y.is_finite()
            && self.min.x <= self.max.x
            && self.min.y <= self.max.y
    }
}

/// Returns whether a screen point is inside a ratatui rectangle.
///
/// The test is half-open, matching [`Rect`]: `(x, y)` must be at least the
/// rectangle origin and strictly before its right and bottom edges.
pub fn point_in_rect(point: ScreenPoint, area: Rect) -> bool {
    point.x.is_finite()
        && point.y.is_finite()
        && point.x >= f32::from(area.x)
        && point.y >= f32::from(area.y)
        && point.x < f32::from(area.right())
        && point.y < f32::from(area.bottom())
}

/// Projects a world point into terminal coordinates.
///
/// Returns `None` when the origin, point, or zoom is non-finite, or when zoom
/// is not positive.
pub fn world_to_screen(
    area: Rect,
    camera_origin: WorldPoint,
    zoom: f32,
    point: WorldPoint,
) -> Option<ScreenPoint> {
    if !valid_transform(camera_origin, zoom) || !finite_world(point) {
        return None;
    }
    Some(ScreenPoint {
        x: f32::from(area.x) + (point.x - camera_origin.x) * zoom,
        y: f32::from(area.y) + (point.y - camera_origin.y) * zoom,
    })
}

/// Converts terminal coordinates to a world point.
pub fn screen_to_world(
    area: Rect,
    camera_origin: WorldPoint,
    zoom: f32,
    point: ScreenPoint,
) -> Option<WorldPoint> {
    if !valid_transform(camera_origin, zoom) || !finite_screen(point) {
        return None;
    }
    Some(WorldPoint {
        x: camera_origin.x + (point.x - f32::from(area.x)) / zoom,
        y: camera_origin.y + (point.y - f32::from(area.y)) / zoom,
    })
}

/// Computes a new camera origin for cursor-centred zooming.
///
/// The world point below `cursor` before the zoom remains below the same
/// terminal cell afterwards. The cursor may be outside `area`, which is useful
/// for callers that deliberately zoom relative to an off-screen anchor.
pub fn cursor_centered_zoom(
    area: Rect,
    camera_origin: WorldPoint,
    old_zoom: f32,
    new_zoom: f32,
    cursor: ScreenPoint,
) -> Option<WorldPoint> {
    if !new_zoom.is_finite() || new_zoom <= 0.0 {
        return None;
    }
    let anchor = screen_to_world(area, camera_origin, old_zoom, cursor)?;
    Some(WorldPoint {
        x: anchor.x - (cursor.x - f32::from(area.x)) / new_zoom,
        y: anchor.y - (cursor.y - f32::from(area.y)) / new_zoom,
    })
}

/// Converts a screen-space drag into the delta to add to a camera origin.
///
/// Dragging right/down moves graph content right/down, so the camera origin
/// moves left/up. `start == end` therefore produces a zero delta.
pub fn drag_pan_delta(start: ScreenPoint, end: ScreenPoint, zoom: f32) -> Option<WorldPoint> {
    if !finite_screen(start) || !finite_screen(end) || !zoom.is_finite() || zoom <= 0.0 {
        return None;
    }
    Some(WorldPoint {
        x: (start.x - end.x) / zoom,
        y: (start.y - end.y) / zoom,
    })
}

/// Maps a terminal column in a timeline rectangle to its nearest event index.
///
/// Both ends are exact: the first column maps to event zero and the final
/// column maps to `event_count - 1`. A one-column timeline maps to zero.
pub fn timeline_column_to_index(area: Rect, column: u16, event_count: usize) -> Option<usize> {
    if event_count == 0 || area.width == 0 || column < area.x || column >= area.right() {
        return None;
    }
    if event_count == 1 || area.width == 1 {
        return Some(0);
    }
    let offset = u128::from(column - area.x);
    let columns = u128::from(area.width - 1);
    let last_index = (event_count - 1) as u128;
    // Add half a cell's scaled width to choose the nearest event rather than
    // biasing every click toward the preceding event.
    Some(((offset * last_index + columns / 2) / columns) as usize)
}

/// Maps a point in a minimap rectangle to the corresponding world-space
/// camera centre.
///
/// Callers should pass the minimap's drawable inner rectangle when its border
/// must not be clickable. The first and final cells map exactly to the minimum
/// and maximum bounds. Degenerate one-cell or zero-span axes map to the axis
/// midpoint.
pub fn minimap_point_to_camera_center(
    minimap: Rect,
    point: ScreenPoint,
    bounds: WorldBounds,
) -> Option<WorldPoint> {
    if minimap.width == 0
        || minimap.height == 0
        || !point_in_rect(point, minimap)
        || !bounds.is_valid()
    {
        return None;
    }
    Some(WorldPoint {
        x: map_axis(
            point.x - f32::from(minimap.x),
            minimap.width,
            bounds.min.x,
            bounds.max.x,
        ),
        y: map_axis(
            point.y - f32::from(minimap.y),
            minimap.height,
            bounds.min.y,
            bounds.max.y,
        ),
    })
}

fn map_axis(offset: f32, cells: u16, min: f32, max: f32) -> f32 {
    if cells <= 1 || min == max {
        return (min + max) / 2.0;
    }
    min + (offset / f32::from(cells - 1)) * (max - min)
}

fn finite_screen(point: ScreenPoint) -> bool {
    point.x.is_finite() && point.y.is_finite()
}

fn finite_world(point: WorldPoint) -> bool {
    point.x.is_finite() && point.y.is_finite()
}

fn valid_transform(origin: WorldPoint, zoom: f32) -> bool {
    finite_world(origin) && zoom.is_finite() && zoom > 0.0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn assert_point_close(actual: WorldPoint, expected: WorldPoint) {
        assert!((actual.x - expected.x).abs() < 1.0e-5, "x: {actual:?}");
        assert!((actual.y - expected.y).abs() < 1.0e-5, "y: {actual:?}");
    }

    #[test]
    fn rect_hit_test_uses_half_open_edges() {
        let area = Rect::new(10, 20, 4, 3);
        assert!(point_in_rect(ScreenPoint::new(10.0, 20.0), area));
        assert!(point_in_rect(ScreenPoint::new(13.999, 22.999), area));
        assert!(!point_in_rect(ScreenPoint::new(14.0, 22.0), area));
        assert!(!point_in_rect(ScreenPoint::new(13.0, 23.0), area));
        assert!(!point_in_rect(ScreenPoint::new(9.999, 20.0), area));
        assert!(!point_in_rect(ScreenPoint::new(f32::NAN, 20.0), area));
        assert!(!point_in_rect(
            ScreenPoint::new(10.0, 20.0),
            Rect::new(10, 20, 0, 0)
        ));
    }

    #[test]
    fn screen_world_conversion_round_trips_with_offset_area() {
        let area = Rect::new(8, 5, 80, 24);
        let origin = WorldPoint::new(-12.5, 3.25);
        let world = WorldPoint::new(7.5, 13.25);
        let screen = world_to_screen(area, origin, 1.5, world).unwrap();
        assert_eq!(screen, ScreenPoint::new(38.0, 20.0));
        assert_point_close(screen_to_world(area, origin, 1.5, screen).unwrap(), world);
    }

    #[test]
    fn conversions_reject_invalid_zoom_and_non_finite_input() {
        let area = Rect::new(0, 0, 10, 10);
        let origin = WorldPoint::default();
        assert!(world_to_screen(area, origin, 0.0, origin).is_none());
        assert!(screen_to_world(area, origin, -1.0, ScreenPoint::default()).is_none());
        assert!(world_to_screen(area, origin, 1.0, WorldPoint::new(f32::INFINITY, 0.0)).is_none());
    }

    #[test]
    fn cursor_zoom_preserves_anchored_world_point() {
        let area = Rect::new(10, 4, 100, 50);
        let origin = WorldPoint::new(-5.0, 3.0);
        let cursor = ScreenPoint::new(40.0, 19.0);
        let anchor = screen_to_world(area, origin, 1.5, cursor).unwrap();
        let zoomed_origin = cursor_centered_zoom(area, origin, 1.5, 3.0, cursor).unwrap();
        let projected = world_to_screen(area, zoomed_origin, 3.0, anchor).unwrap();
        assert!((projected.x - cursor.x).abs() < 1.0e-5);
        assert!((projected.y - cursor.y).abs() < 1.0e-5);
        assert!(cursor_centered_zoom(area, origin, 1.5, f32::NAN, cursor).is_none());
    }

    #[test]
    fn drag_pan_is_inverse_screen_motion_scaled_by_zoom() {
        assert_point_close(
            drag_pan_delta(
                ScreenPoint::new(10.0, 10.0),
                ScreenPoint::new(16.0, 6.0),
                2.0,
            )
            .unwrap(),
            WorldPoint::new(-3.0, 2.0),
        );
        assert_eq!(
            drag_pan_delta(ScreenPoint::default(), ScreenPoint::default(), 1.0),
            Some(WorldPoint::default())
        );
        assert!(drag_pan_delta(ScreenPoint::default(), ScreenPoint::default(), 0.0).is_none());
    }

    #[test]
    fn timeline_mapping_includes_both_boundaries_and_rounds_nearest() {
        let area = Rect::new(10, 3, 11, 1);
        assert_eq!(timeline_column_to_index(area, 10, 6), Some(0));
        assert_eq!(timeline_column_to_index(area, 12, 6), Some(1));
        assert_eq!(timeline_column_to_index(area, 15, 6), Some(3));
        assert_eq!(timeline_column_to_index(area, 20, 6), Some(5));
        assert_eq!(timeline_column_to_index(area, 9, 6), None);
        assert_eq!(timeline_column_to_index(area, 21, 6), None);
    }

    #[test]
    fn timeline_mapping_handles_empty_and_single_cell_ranges() {
        assert_eq!(
            timeline_column_to_index(Rect::new(7, 0, 1, 1), 7, 50),
            Some(0)
        );
        assert_eq!(timeline_column_to_index(Rect::new(7, 0, 1, 1), 7, 0), None);
        assert_eq!(timeline_column_to_index(Rect::new(7, 0, 0, 1), 7, 1), None);
        assert_eq!(
            timeline_column_to_index(Rect::new(7, 0, 5, 1), 9, 1),
            Some(0)
        );
    }

    #[test]
    fn minimap_maps_corners_and_centre_to_world_bounds() {
        let map = Rect::new(5, 7, 11, 5);
        let bounds = WorldBounds::new(WorldPoint::new(-50.0, 20.0), WorldPoint::new(50.0, 60.0));
        assert_point_close(
            minimap_point_to_camera_center(map, ScreenPoint::new(5.0, 7.0), bounds).unwrap(),
            bounds.min,
        );
        assert_point_close(
            minimap_point_to_camera_center(map, ScreenPoint::new(15.0, 11.0), bounds).unwrap(),
            bounds.max,
        );
        assert_point_close(
            minimap_point_to_camera_center(map, ScreenPoint::new(10.0, 9.0), bounds).unwrap(),
            WorldPoint::new(0.0, 40.0),
        );
    }

    #[test]
    fn minimap_rejects_outside_or_invalid_inputs_and_handles_degenerate_axes() {
        let map = Rect::new(2, 3, 1, 1);
        let bounds = WorldBounds::new(WorldPoint::new(2.0, 4.0), WorldPoint::new(8.0, 10.0));
        assert_point_close(
            minimap_point_to_camera_center(map, ScreenPoint::new(2.0, 3.0), bounds).unwrap(),
            WorldPoint::new(5.0, 7.0),
        );
        assert!(minimap_point_to_camera_center(map, ScreenPoint::new(3.0, 3.0), bounds).is_none());
        let reversed = WorldBounds::new(WorldPoint::new(8.0, 4.0), WorldPoint::new(2.0, 10.0));
        assert!(
            minimap_point_to_camera_center(map, ScreenPoint::new(2.0, 3.0), reversed).is_none()
        );
    }
}
