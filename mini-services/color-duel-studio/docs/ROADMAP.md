# From local studio to production authoring service

## Delivered MVP

Image upload and sanitization; optional art-direction chat; optional generation/edit calls; real offline vector compilation; immutable project revisions; before/after/play/edit preview; adjacency-only merge; palette/object assignment; label placement; precolored-detail conversion; geometry report; runtime and authoring ZIP export.

## Next: improve artwork quality

- Add a semantic boundary proposal service or segmentation model: roof, feathers, petals, animal anatomy. Treat model output as proposals and verify topology after each edit.
- Add a pen tool, split region tool, multi-region brush and per-object simplification. Rebuild topology from a shared planar boundary graph; don't independently simplify adjacent shapes.
- Add object-aware grouped zoom previews, contour snapping and a low-contrast palette warning.
- Offer a hybrid rendering mode (detailed texture + vector interaction masks) for source fidelity and a pure-vector mode for fully vector delivery. Current exporter is pure vector and approximates texture.
- Include difficulty playtests and accessibility targets, not a promise that a larger region count alone makes a good challenge.

## Next: reliable publishing

- Authentication, role separation for creator/reviewer/publisher, per-project access control and audit log.
- PostgreSQL metadata; object storage for immutable masters/revisions; queue with durable jobs, restart recovery and idempotent publish operations.
- Per-user AI spending limits, rate limits, cancellation and provider timeout reconciliation. Do not automatically replay billable requests after crashes.
- Private master/reference storage; public sanitized runtime assets only. Record provenance, user permissions and review status. These records are not an automated legal guarantee.
- Versioned catalog API/CDN, hashes, compression, checksum validation and game-side schema migrations.
- Mobile real-device profiling: cache static painting; tile large assets; measure memory, frame time and touch latency on lower-end devices.

## Suggested acceptance gate for a published puzzle

No geometry errors; no unreviewed microscopic targets; all meaningful object groups reviewed; approved final appearance matches the playable reveal; comfortable label behavior at fit and zoom; no competitor screenshots/logos/UI in source; permission/provenance reviewed; device tests completed; ranked puzzles separately balanced and approved.

## UI refinement plan

Keep this a separate desktop-friendly creator studio. Left = reference/chat, center = artwork and stage tabs, right = compiler/review/export. The consumer game remains mobile-first with a quiet canvas, large scrollable palette, clear zoom and progress. Avoid squeezing hundreds of labels or dozens of swatches into a single mobile row.
