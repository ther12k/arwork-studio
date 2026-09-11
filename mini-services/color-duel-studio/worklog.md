
---
Task ID: 23
Agent: main (ZCode)
Task: Phase 2C — Use as Reference: vision scene understanding drafts a NEW semantic ScenePlan; artwork generated as native vectors via the existing session pipeline. Source is never traced.

Work Log:
- generation.py: plan_session_from_image(session, provider, reference, instructions) — sends the uploaded image to Provider.scene_plan (vision analysis), maps the understood scene (subject/composition/mood categories) into ScenePlan records via the same _plan_objects_from_provider path as chat planning; records referenceFile + planUsage in session meta; guards: mode must be image_reference (ai_chat allowed for guidance images; image_convert rejected — Convert gets its own decomposition pipeline in 2D), reference file must exist, session must be mutable.
- app.py /reference-plan route (multipart): paid confirm gate + provider-configured gate; stores the reference under sessions/{sid}/reference-image.<ext> (12 MB cap, png/jpg/webp); runs as an async job like every paid step.
- Per the reviewer's blueprint this mode answers "what is in the image and what makes the composition recognizable" — the prompt explicitly instructs reuse of broad mood/palette/subject only, NOT composition tracing; generation then proceeds through the unchanged /generate → /commit machinery (provenance mode=image_reference, fidelity recorded).
- Tests (test_generation_reference_plan_mocked): asserts the image is actually attached (input_image part) to the /json vision call, the not-its-composition instruction is present, plan objects materialize with obj-* ids, usage + reference provenance recorded, generate → ready_to_commit, commit → manifest generation.mode=image_reference + fidelity, wrong-mode session (image_convert) fails the job with an actionable message. Suite: 87 passed / 7 skipped via Docker runner.

Stage Summary:
- Phase 2C complete: Use as Reference is live end-to-end on the session orchestrator — image in, semantic ScenePlan out, native vector generation afterwards, all isolation/commit/provenance guarantees inherited from 2A. Engineering cost stayed low as predicted: only the plan-input step differs from 2B.
- Next per the frozen order: Phase 2D Convert Artwork (semantic decomposition + color/edge segmentation associated into objects, fidelity presets as real algorithm parameters) — deliberately last as the hardest pipeline.
