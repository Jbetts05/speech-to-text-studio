# Evidence Ledger Design

## Design intent

The Evidence Ledger is a calm, dense workspace for reviewing benchmark evidence. It
prioritizes traceability, comparison, and state clarity over decorative dashboards.
Every summary should lead to the records that produced it.

## Principles

1. Evidence before summary.
2. Stable context while details move.
3. Metric families remain visually distinct.
4. Status uses text and shape as well as color.
5. Dense information stays keyboard and screen-reader accessible.
6. Motion confirms state changes but never becomes required.
7. Modality viewers share a frame, not assumptions about their content.

## Information architecture

- **Home**: active benchmark, platform counts, current run, and primary actions.
- **Benchmarks**: installed packs, versions, modalities, compatibility, and evaluation
  contracts.
- **Create benchmark**: the safe scaffold workflow at the plugin boundary.
- **Solutions**: standalone/assisted pipelines, access mode, and comparability.
- **Configure run**: explicit solution, retry, and network policy before launch.
- **Evaluation detail**: whole-run metrics, service summaries, all sample/service
  results, on-demand input/comparison review, and collapsed operational evidence.
- **Runs**: immutable, URL-restorable run history.
- **Reports**: canonical JSON and derived CSV evidence exports.
- **Admin**: read-only runtime and active-contract status.

This navigation and page hierarchy is platform-owned. A benchmark pack changes the
nouns inside the workspace through typed terminology, not the navigation model.

## Visual language

- Use `"Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif`.
- Use canvas `#f5f7fa`, rail `#fafbfc`, surface `#ffffff`, quiet fill `#f3f6f9`,
  rules `#dfe4eb`/`#bec7d3`, text `#172033`, muted text `#596579`, and soft text
  `#737f90`.
- Use Azure blue `#0f5cc0` for actions, selection, and focus, with hover
  `#0b4da3` and wash `rgba(15, 92, 192, 0.08)`.
- Use success `#107c41`, warning `#7a4700`, and danger `#b42318`.
- Use navigation `#071b33`, raised navigation `#102d4c`, and navigation rule
  `#234563`.
- Use consistent status tokens: icon, text, and color together.
- Keep identifiers, hashes, numeric values, and code in a readable monospace style.
- Use borders and spacing to express grouping before shadows.
- Avoid gradients, decorative glass effects, and color-only heatmaps.

These tokens are normative, not suggestions for pack authors. Do not substitute
benchmark-themed palettes or serif/display typography.

## Shell and navigation

- Desktop uses a 58px top bar over `218px 224px minmax(0, 1fr)` tracks:
  primary navigation, context rail, and workspace.
- At `1220px` the tracks become `205px 218px minmax(0, 1fr)` and the five KPI
  cards reflow to three columns.
- At `1100px` the context rail is removed and recent runs move into primary
  navigation.
- At `900px` the shell becomes a 56px mobile header plus a focus-trapped
  navigation drawer. Escape closes the drawer and focus returns to its opener.
- At `640px` actions stack, KPI cards become a horizontal snap strip, and the
  artifact/comparison workspace stacks.

The top bar always identifies AI Bench, the selected benchmark, and the current
workspace. The context rail always prioritizes the active contract, evidence-family
invariant, current run, and recent runs.

## Evaluation composition

Evaluation detail uses this fixed order:

1. A slim section/action toolbar.
2. Five independent KPI cards: quality, cost, latency, usage, and reliability.
3. A sortable service-performance table scoped to the whole run.
4. All sample/service results in a sortable table, with an All services default filter.
5. An on-demand renderer-driven input/reference/comparison drill-down, followed by
   collapsed operational evidence and immutable manifest details.
6. A provenance footer.

KPI cards use five equal tracks, a 14px gap, 112px minimum height, and 14px by
15px padding. Values use declared units: ratios render as percentages, money as
currency, latency with time units, and counts with their own dimensions. Unknown
values say `Not available`; they never become zero. The performance table has a
900px minimum width, 36px rows, 6px by 10px cells, 9px headers, and 10px body
copy. A metric column appears only when at least one row records an applicable
value, except cost: cost always remains visible so absent pricing cannot look free.
Cost cells distinguish recorded amounts, real zero, unknown amounts and partial
subtotals. Separate current-rate estimates state their conditional basis and coverage.
Keep currencies separate; reveal pricing sources, dates, assumptions and retry-inclusive
usage in a compact disclosure below performance and in Reports. Original report
downloads remain unchanged. Per-sample costs appear alongside individual results;
recorded and conditional estimate columns are separate and never mix currencies.
Every Cost tab opens monetary run totals, per-service amounts, per-example costs,
coverage and pricing provenance, never an artifact-only ledger.
Service summaries expose separate recorded and conditional-estimate sort columns per
currency; they never sort an estimate as though it were recorded cost. Unknown amounts
are labelled once, while partial/conditional distinctions and coverage stay explicit.
Primary service/example cost headings use optional presentation names with stable-ID
fallback; pricing-source disclosures retain immutable identifiers. Pipeline mechanics
belong in manifest details, not a repetitive primary summary column.

Combined cost summaries in comparison, service/example details and Reports omit the
empty recorded-cost block when a current-rate estimate is available. Keep the estimate
label and any partial-subtotal warning visible; put recorded unknowns, attempt coverage
and billing caveats in the native Cost details disclosure. Known recorded amounts,
including zero, remain separate and visible. When neither amount is available, show
unknown rather than implying zero. Separate recorded/estimate table columns and
immutable pricing evidence are unchanged.

The results table has one row for each sample/service pair, including pending,
failed and missing results. Counts derive from structured run records, not a
pack-specific sample or service count. No input, transcript, or comparison artifact
is fetched to paint this table. Service filtering changes only the result rows;
whole-run and service summaries retain an explicit whole-run scope. Metric sorting
uses underlying numbers, not formatted percentages/currency, with unknowns last in
both directions. Short labels retain keyboard-accessible metric explanations.
Sample identifiers use a single opaque sticky track (146px, 100px at 640px),
with a service sublabel so every row remains identifiable at nonzero scroll.

Selecting a result opens only its source/reference/comparison on demand. Focus moves
to the detail heading; Back to results or Escape returns focus to the selected row.
Artifacts, hashes and attempt history remain in a closed-by-default Operational
details disclosure. Routine repeated usage/billing caveats share one quiet contextual
disclosure; runtime errors and actionable failures remain explicit.

Primary comparison data comes from canonical result-level `comparison` evidence.
Input and reference artifacts render through the pack's viewer descriptor. Raw,
parsed, provider, and retry-attempt artifacts remain one level lower in
Operational details.

Completed evaluations load the typed finalized-report view first, with an explicit
historical-record label and the complete recorded result matrix. Initial display
does not fetch live task detail, events or a second report. Incomplete historical
records explicitly use live detail; integrity errors never silently fall back.
Current diagnostics load only through a separate action in Operational details.
Their loading, retry and cancellation state cannot replace historical results;
confirmed expiry or removal still removes inaccessible data. Active runs retain live updates
and switch to finalized records when available.

Routine provider delivery and duplicate-billing risk stays in a quiet, accessible
context disclosure. Warning/error styling is reserved for actionable conditions;
the disclosure retains the full risk information and never promises exactly-once
delivery or billing.

## Modality frame

Each artifact viewer receives a typed descriptor, evidence reference, accessible title,
and fallback download action.

| Modality | Primary presentation |
|---|---|
| Image | Scaled image with zoom, dimensions, and alternate text |
| Document | Page navigation plus extracted or referenced structure |
| Audio | Native controls, duration, transcript/reference tracks |
| Video | Native controls, duration, key events or reference tracks |
| Text | Preserved whitespace with optional structured annotations |
| Structured | Expandable tree/table with copy and download actions |

Unknown or unsupported media must render metadata and a safe download link rather than
an empty panel.

## Horizontal Ledger Rule

Every horizontal table follows all of these requirements:

1. Put the semantic `<table>` in its own horizontal scroll container. The surrounding
   page must not become the table's scroll surface.
2. Never apply `display: grid`, `display: flex`, or `display: block` directly to
   `th` or `td`. Preserve native table layout and semantics.
3. Every sticky track must use identical `width`, `min-width`, and `max-width` values.
4. Sticky cells must have an opaque background and an explicit `z-index`.
5. Sticky cell content must clip overflow inside the fixed track.
6. The trailing edge of the last sticky track must have an explicit rule or shadow so
   fixed and scrolling regions never visually merge.
7. Header and body sticky offsets must be generated from the same track definition.
8. At every responsive breakpoint, sticky widths and offsets change together.
9. Tests must validate the rendered bounds of each sticky cell's child and text content
   at `scrollLeft = 0` and at a nonzero `scrollLeft` for every horizontal table.
10. A table is not complete if its cells appear aligned only in a screenshot; semantic,
    keyboard, and rendered-bound tests are required.

The comparison inspector uses a 220px source track, then a semantic table with a
104px sticky field track, a 158px sticky reference track at `left: 104px`, and
155px solution tracks. Header/body pairs share identical `width`, `min-width`,
and `max-width`. Corner, reference-header, header, reference-body, and
field-body layers use explicit z-index levels `7`, `6`, `4`, `3`, and `2`
respectively. Every sticky track has an opaque background, clipped overflow, and
a one-pixel trailing rule.

## Interaction and accessibility

- Tabs use their native tab roles, `aria-controls`, `aria-selected`, roving tab index,
  and Arrow/Home/End keys.
- Dialogs and drawers trap focus, close with Escape, and restore focus to the opener.
- Sort controls expose `aria-sort` and have a text label.
- Live run updates use a polite live region and do not repeatedly steal focus.
- Loading, empty, error, unsupported, and stale states are explicit.
- A disabled launch has an accessible reason beside the button. Retry selected dataset
  and Refresh datasets reload its details, not just the catalog, without admitting
  stale responses or bypassing expiry/readiness.
- In an authorized hosted runtime, Launch supplies explicit per-run network permission
  with a passive paid-usage note; there is no extra network checkbox or modal.
  Local operation retains its network opt-in and operator runtime authorization.
- Focus outlines remain visible in high contrast.
- Reduced-motion preferences disable nonessential transitions.
- Touch targets remain usable at narrow widths.
- Solutions and Configure use compact, plain service rows with native selection
  checkboxes only where selection is possible. Pack-declared display names are
  presentation metadata with stable-ID fallback, never inferred model or version
  claims. Routine pipeline/network/quality badges are omitted; genuine unsupported
  or unavailable reasons remain explicit.
- Public-demo catalog/navigation is scoped to the known runtime landing benchmark;
  unknown runtime or missing public scope fails closed. Non-public local and Azure
  catalogs, saved-run history, and pinned bookmarked evidence remain available. Paid-usage authorization is quiet
  contextual text; public rights and retention acknowledgments are unchanged.

## Responsive behavior

- Preserve the current benchmark/run identity before collapsing navigation.
- Let modality viewers and ledgers scroll inside bounded regions.
- Do not hide evidence columns solely to fit a viewport; offer a column control or
  stacked detail view.
- Sticky tracks may reduce in width only when content clipping and accessible labels
  remain valid.

## Regression contract

Frontend tests must assert:

- the exact light token values, Segoe-first font stack, desktop tracks, and four
  responsive breakpoints;
- all nine navigation destinations and URL restoration with `view`, `benchmark`,
  and `run` query parameters;
- five independent KPI cards with no blended score and ratio formatting as
  percentages;
- dynamic performance columns with no all-empty non-cost columns and always-visible
  cost coverage;
- result-comparison evidence as the default operational scope;
- whole-run sample/service coverage, numeric sorting, unknowns, polling, filtering,
  lazy artifact reads and drill-down focus restoration;
- renderer behavior for text, structured, image, audio, video, document, and safe
  download fallback;
- mobile drawer focus containment and restoration;
- sticky width/offset/background/z-index/trailing-rule geometry and rendered
  child/text bounds at zero and nonzero horizontal scroll.
