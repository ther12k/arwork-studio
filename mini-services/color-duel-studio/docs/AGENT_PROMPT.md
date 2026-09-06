# Handoff prompt

Build on the attached Color Duel Art Studio instead of generating another image-only preview pack. Inspect the current game repository first and preserve its stack and VS features. This starter has a local Python/FastAPI compiler and a framework-free browser studio; it is a separate authoring tool, not a mandate to rewrite the game.

Run the included tests and sample first. Users can upload inspiration references, chat to refine an original brief, explicitly generate/edit a master through the server-side AI provider, then compile locally into `color-duel-detailed-vector-1`. Keep inspiration-only reference analysis separate from direct owned-image editing. Keep keys private, retain explicit paid-request confirmations, and never claim copyright clearance.

Import the exact emitted format: `artwork.json`, `regions.json`, `palette.json`, `paint.json`, colored/numbered/linework/ink SVGs and thumbnail. SVGs must contain real paths; regions must be complete closed geometry, not optional stubs. Use a shared viewBox and even-odd holes. Fine paint subshapes are not gameplay taps. Reveal detailed underpainting by removing completed region masks; hide each completed region's number. Save region IDs with artwork version/hash; reject stale geometry sessions. In Memory Duel, hide matching-color highlights as well as labels.

Integrate the included detailed-vector adapter into the existing game rather than replacing rich artwork with flat swatch fills. Improve gallery thumbnails and mobile canvas layout, palette scrolling, pan/zoom and loading/error states. Preserve Arena and server-authoritative scoring. Do not silently use new region layouts in saved sessions or active matches.

Next improve the studio with semantic region proposals, shared-boundary editing/splitting, device performance profiling and reviewer-controlled publication. Add authentication, durable jobs and versioned object storage before hosting it publicly. Automatic drafts must stay marked draft until a person reviews them. Deliver tested code and document any untested live-AI or device paths without inventing test counts.
