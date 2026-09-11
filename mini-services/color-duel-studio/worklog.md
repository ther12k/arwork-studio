
---
Task ID: 22
Agent: main (ZCode)
Task: Phase 2B — Create with AI: chat-first ScenePlan synthesis, per-object vector generation, targeted object regeneration — all as gated paid steps inside the 2A session orchestrator.

Work Log:
- ai.py refactor: extracted svg_multistage's fragment+compose loop into the new Provider.svg_compose_from_objects(objects, aspect, progress) — composes a master from ALREADY-PLANNED objects with NO planning call; object identity uses obj.get('id') when present (session plan objects carry stable obj-* ids) with the old name-slug fallback for the raw multistage path. svg_multistage now = scene_plan → _normalize_plan → svg_compose_from_objects; the existing multistage route/tests pass unchanged.
- generation.py additions:
  * _fragment_spec: translates ScenePlan objects into provider fragment specs (shapes count derives from detailWeight: clamp(round(dw*12), 6, 30) — the planner's shape count IS the complexity signal).
  * _plan_objects_from_provider: maps raw provider scene-plan objects into ScenePlan records (deterministic obj-<slug> ids, detailWeight = clamp(shapes/12, 0.2, 4)).
  * plan_session_with_ai: one strict-JSON planning call from the session prompt + accumulated brief + planning instructions; REPLACES the draft's object list; title/description/difficulty from prior mutations survive; plan usage stored in session meta.
  * generate_session_master: guards (not committed, plan has objects) → status 'generating' → svg_compose_from_objects over the plan → master sanitized via clean_svg into sessions/{sid}/source-master.svg → compile_session (target regions from the plan's difficulty) → ready_to_commit. Any failure → status 'failed', revisions untouched.
  * replace_object_shapes + regenerate_session_object (targeted regeneration, reviewer design): load session master → import → drop the target object's shapes → splice a freshly generated fragment one-for-one into the old z slots (surplus appended above) → re-emit → full recompile re-derives neighbours' visible surfaces. objectId is PRESERVED while internal shapeIds change; new shape/gradient ids carry a deterministic per-(object, fragment) sha1 prefix so ids stay collision-free across objects and repeated regenerations.
- app.py routes (all with explicit confirm_paid + provider-configured gates, mirroring the existing paid-action pattern): POST …/sessions/{sid}/plan, POST …/generate, POST …/regenerate-object (objectId required; unknown ids fail the job with an actionable message).
- Tests (2 new mocked end-to-end via the multistage MockTransport; suite: 86 passed / 7 skipped):
  * test_generation_plan_and_generate_mocked: paid gates on plan+generate; exactly one /json planning call; 6 planned objects with obj-* ids and derived detailWeights; planUsage + aiUsage recorded; user mutations (add cat, remove path) reflected; generate = exactly one /svg call per planned object; ready_to_commit with QA passed; isolation until commit; commit → revision with generation manifest block + objects.json honouring the mutations.
  * test_generation_targeted_regeneration_mocked: ready session → regenerate obj-house with instructions → exactly ONE extra /svg call → still ready_to_commit with QA passed → untouched objects keep their exact shapeIds (obj-sky, obj-flowers unchanged) → obj-house still owns shapes under its stable objectId → master file carries the deterministic regen prefix → unknown object fails the job ('not part of this session plan') → still zero project revisions touched.
- Noted behavior: compile-time shapeIds are re-derived from master document order, so a one-for-one slot replacement keeps untouched objects' shapeIds stable; a fragment that GROWS the object appends surplus shapes above the previous layer (documented in replace_object_shapes).
- Docs: FORMAT.md generation section now documents the plan → mutate → generate → commit flow and targeted regeneration semantics; AGENTS.md orchestrator bullet extended with the Phase 2B steps.

Stage Summary:
- Phase 2B (Create with AI) is functionally complete end-to-end on the orchestrator contract: chat-first plan (cheap) → revisable structured ScenePlan → per-object native vector synthesis (paid per object) → isolated compile + QA → atomic commit; targeted object regeneration replaces one object without regenerating the scene and without ever touching healthy revisions. Reviewer gates 'creates native semantic master' and 'chat revision changes one object without regenerating the whole scene' are provable from the suite; the UI layer (2B frontend) can now be built on these routes.
