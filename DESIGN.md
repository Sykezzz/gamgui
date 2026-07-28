# GamGUI interface contract

GamGUI is a dense local administration console. Its interface should feel calm,
compact, and operational rather than promotional.

## Layout

- The global header stays on one compact row. At narrow widths, navigation scrolls
  horizontally instead of wrapping into a tall banner.
- Pages use the browser's document scroll. Full-page tab panels must not introduce a
  second viewport-height scroll region.
- Bounded lists inside a focused control (permission lists, pickers, logs) may scroll
  independently when their boundary and item count are clear.
- Primary actions remain visible without horizontal table scrolling. On narrow
  screens, data rows become native full-row controls with secondary metadata stacked.

## Interaction

- Rows that open management detail are native buttons or links across their full hit
  area; a small action in the final table column is not the only path.
- Changed content receives programmatic focus without trapping focus or forcing the
  user away from nearby controls.
- Destructive and access-changing operations use preview/confirm steps and state
  residual access explicitly.
- Browser-account actions and delegated-admin actions must be named differently.
  Never imply that a Google editor link inherits GamGUI's delegated identity.

## Visual system

- Use the existing Source Sans/Source Serif typography and committed brand tokens in
  `gamgui/web/static/app.source.css`.
- Use the existing spacing, radius, border, and focus patterns. Do not add a parallel
  color or component system.
- Operational numbers and identifiers use tabular or monospace presentation where
  comparison matters.

## Accessibility

- Native controls first, visible focus states, logical tab order, and keyboard-complete
  tabs and selectors.
- Normal text meets 4.5:1 contrast; controls and focus indicators meet 3:1.
- Status and risk are communicated in text, not color alone.
- Motion is optional and respects `prefers-reduced-motion`.
