"""Task 35 — End-to-end smoke suite (acceptance for Task 31 hardening).

Runs the FULL in-process API journey against a deterministic OpenAI-compatible
mock, covering the reviewer's smoke matrix. This is the acceptance layer above
unit tests: every journey goes create → paid steps → terminal state, and the
lost-response scenarios simulate the response being discarded AFTER the server
accepted the work (exactly the condition idempotent recovery must survive).
"""

import io, json, tempfile, threading, time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from studio.app import create_app

H = {'X-Studio-Request': '1'}


def _fixture_png():
    """Deterministic 3-band illustration: sky / house (bottom-left) / grass."""
    buf = io.BytesIO()
    im = Image.new('RGB', (288, 288))
    px = im.load()
    for y in range(288):
        for x in range(288):
            if y < 144:
                px[x, y] = (0x91, 0xCC, 0xDD)
            elif x < 144:
                px[x, y] = (0xEB, 0xC6, 0x81)
            else:
                px[x, y] = (0x41, 0xA5, 0x82)
    im.save(buf, format='PNG')
    return buf.getvalue()


def _transport(recorder, fail_svg_bboxes=None):
    """Deterministic provider mock. `fail_svg_bboxes` is a set/list of bbox
    tuples that fail ONCE (server accepted, response 'lost' as a 500) — the
    lost-response simulation for idempotent recovery."""
    fail = list(fail_svg_bboxes or [])
    def respond(req):
        recorder.append({'path': req.url.path})
        if req.url.path.endswith('/json'):
            objects = [
                {'name': 'sky', 'description': 'blue sky', 'z': 0,
                 'bbox': [0, 0, 576, 300], 'shapes': 10, 'fills': ['#91CCDD']},
                {'name': 'house', 'description': 'yellow house', 'z': 1,
                 'bbox': [0, 380, 288, 388], 'shapes': 14, 'fills': ['#EBC681']},
                {'name': 'grass', 'description': 'green field', 'z': 2,
                 'bbox': [288, 380, 288, 388], 'shapes': 10, 'fills': ['#41A582']},
            ]
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text',
                'text': json.dumps({'objects': objects})}]}], 'usage': {'input_tokens': 120}})
        if req.url.path.endswith('/svg'):
            body = json.loads(req.content)
            import re as _re
            m = _re.search(r'viewBox="(\d+) (\d+) (\d+) (\d+)"', body.get('instructions', ''))
            bx, by, bw, bh = (int(v) for v in m.groups()) if m else (0, 0, 100, 100)
            if (bx, by) in fail:
                fail.remove((bx, by))
                recorder.append({'path': req.url.path, 'lost_response': True})
                return httpx.Response(502, json={'error': {'code': 'connection_dropped'}})
            pad = max(4, min(bw, bh) // 8)
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{bx} {by} {bw} {bh}">'
                   f'<rect x="{bx+pad}" y="{by+pad}" width="{bw-2*pad}" height="{bh-2*pad}" fill="#3366AA"/>'
                   f'<path d="M {bx+pad},{by+pad} Q {bx+bw/2},{by+pad+(bh-2*pad)/2} {bx+bw-pad},{by+pad} Z" '
                   f'fill="#AA3355" fill-opacity="0.5"/></svg>')
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text', 'text': svg}]}],
                                             'usage': {'input_tokens': 80}})
        return httpx.Response(404, json={'error': {'code': 'no_route'}})
    return httpx.MockTransport(respond)


def _wait(c, pid, timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        p = c.get(f'/api/projects/{pid}').json()
        if p['job']['status'] in ('done', 'failed', 'canceled', 'interrupted'):
            return p
        time.sleep(0.05)
    raise AssertionError('job timeout')


@pytest.fixture()
def studio(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    recorder = []
    app = create_app(tmp_path, transport=_transport(recorder))
    with TestClient(app) as c:
        yield c, recorder
    # acceptance: no workspace leaks inside the repo
    for probe in ('.env', 'db'):
        assert not (tmp_path / probe).exists()


def _new_project(c, title='Smoke artwork'):
    return c.post('/api/projects', headers=H, json={'title': title}).json()['id']


def _create_session(c, pid, mode='ai_chat', tier='medium'):
    return c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                  json={'mode': mode, 'requested_difficulty': tier}).json()['id']


# ---------------------------------------------------------------------------
# Smoke 1 — AI Create: plan → generate → commit via the API journey
# ---------------------------------------------------------------------------

def test_smoke_1_ai_create_plan_generate_commit(studio):
    c, recorder = studio
    pid = _new_project(c)
    sid = _create_session(c, pid)
    base = f'/api/projects/{pid}/generation/sessions/{sid}'
    r = c.post(f'{base}/plan', headers=H,
               json={'confirm_paid': True, 'idempotency_key': 's1-plan'})
    assert r.status_code == 200
    p = _wait(c, pid)
    assert p['job']['status'] == 'done'
    r = c.post(f'{base}/generate', headers=H,
               json={'confirm_paid': True, 'idempotency_key': 's1-gen'})
    assert r.status_code == 200
    p = _wait(c, pid)
    assert p['job']['status'] == 'done', p['job']
    sess = c.get(base).json()
    assert sess['status'] == 'ready_to_commit'
    r = c.post(f'{base}/commit', headers=H, json={'idempotency_key': 's1-commit'})
    assert r.status_code == 200
    rev = r.json()['revision']['id']
    p = c.get(f'/api/projects/{pid}').json()
    assert p['currentRevision'] == rev
    # the committed revision carries the semantic objects
    objs = c.get(f'/api/projects/{pid}/revisions/{rev}/files/objects.json').json()
    assert {o['id'] for o in objs['objects']} >= {'obj-sky', 'obj-house', 'obj-grass'}


# ---------------------------------------------------------------------------
# Smoke 2 — Generate response lost AFTER the server accepted the work:
#           the retry with the SAME key replays; provider calls do not grow.
# ---------------------------------------------------------------------------

def test_smoke_2_generate_response_lost_recovery(studio):
    c, recorder = studio
    pid = _new_project(c)
    sid = _create_session(c, pid)
    base = f'/api/projects/{pid}/generation/sessions/{sid}'
    c.post(f'{base}/plan', headers=H,
           json={'confirm_paid': True, 'idempotency_key': 's2-plan'})
    _wait(c, pid)
    body = {'confirm_paid': True, 'idempotency_key': 's2-gen'}
    r = c.post(f'{base}/generate', headers=H, json=body)
    assert r.status_code == 200
    _wait(c, pid)
    svg_accepted = sum(1 for x in recorder
                       if x['path'].endswith('/svg') and not x.get('lost_response'))
    # response "lost": N resends with the same key
    for _ in range(3):
        r = c.post(f'{base}/generate', headers=H, json=body)
        assert r.status_code == 200
        assert r.json()['idempotentReplay'] is True
    svg_after = sum(1 for x in recorder
                    if x['path'].endswith('/svg') and not x.get('lost_response'))
    assert svg_after == svg_accepted, 'replays must not buy provider work'


# ---------------------------------------------------------------------------
# Smoke 3 — Commit response lost: recovery returns the SAME revision.
# ---------------------------------------------------------------------------

def test_smoke_3_commit_response_lost_same_revision(studio):
    c, recorder = studio
    pid = _new_project(c)
    sid = _create_session(c, pid)
    base = f'/api/projects/{pid}/generation/sessions/{sid}'
    c.post(f'{base}/plan', headers=H, json={'confirm_paid': True, 'idempotency_key': 's3-plan'})
    _wait(c, pid)
    c.post(f'{base}/generate', headers=H,
           json={'confirm_paid': True, 'idempotency_key': 's3-gen'})
    _wait(c, pid)
    body = {'idempotency_key': 's3-commit'}
    r1 = c.post(f'{base}/commit', headers=H, json=body)
    assert r1.status_code == 200
    rev = r1.json()['revision']['id']
    # lost response: resend → same revision, no duplicate
    r2 = c.post(f'{base}/commit', headers=H, json=body)
    assert r2.status_code == 200
    assert r2.json()['idempotentReplay'] is True
    assert r2.json()['revision']['id'] == rev
    assert len(c.get(f'/api/projects/{pid}').json()['revisions']) == 1


# ---------------------------------------------------------------------------
# Smoke 4 — Cancel then continue: cancel carries the live jobId; the session
#           recovers to draft_plan; the retry (new key) completes. No manual
#           reload anywhere — the project poll reflects every stage.
# ---------------------------------------------------------------------------

def test_smoke_4a_cancel_before_finish_is_deterministic(tmp_path, monkeypatch):
    """Deterministic cancel-before-finish: the provider holds the FIRST
    fragment response until the cancel has been stored — no timing race.
    Job ends 'canceled', session returns to draft_plan, retry completes."""
    import threading
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    recorder = []
    first_svg_seen = threading.Event()
    release_first_svg = threading.Event()

    def respond(req):
        recorder.append({'path': req.url.path})
        if req.url.path.endswith('/json'):
            objects = [
                {'name': 'sky', 'description': 'blue sky', 'z': 0,
                 'bbox': [0, 0, 576, 300], 'shapes': 10, 'fills': ['#91CCDD']},
                {'name': 'house', 'description': 'yellow house', 'z': 1,
                 'bbox': [0, 380, 288, 388], 'shapes': 14, 'fills': ['#EBC681']},
                {'name': 'grass', 'description': 'green field', 'z': 2,
                 'bbox': [288, 380, 288, 388], 'shapes': 10, 'fills': ['#41A582']},
            ]
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text',
                'text': json.dumps({'objects': objects})}]}], 'usage': {'input_tokens': 120}})
        if req.url.path.endswith('/svg'):
            body = json.loads(req.content)
            import re as _re
            m = _re.search(r'viewBox="(\d+) (\d+) (\d+) (\d+)"', body.get('instructions', ''))
            bx, by, bw, bh = (int(v) for v in m.groups()) if m else (0, 0, 100, 100)
            if not first_svg_seen.is_set():
                # HOLD the response until the cancel request has been stored:
                # the provider work is provably in flight when cancel arrives.
                first_svg_seen.set()
                release_first_svg.wait(timeout=30)
            pad = max(4, min(bw, bh) // 8)
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{bx} {by} {bw} {bh}">'
                   f'<rect x="{bx+pad}" y="{by+pad}" width="{bw-2*pad}" height="{bh-2*pad}" fill="#3366AA"/></svg>')
            return httpx.Response(200, json={'output': [{'content': [{'type': 'output_text', 'text': svg}]}],
                                             'usage': {'input_tokens': 10}})
        return httpx.Response(404, json={'error': {'code': 'no_route'}})

    with TestClient(create_app(tmp_path, transport=httpx.MockTransport(respond))) as c:
        pid = _new_project(c)
        sid = _create_session(c, pid)
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/plan', headers=H, json={'confirm_paid': True, 'idempotency_key': 's4a-plan'})
        _wait(c, pid)
        r = c.post(f'{base}/generate', headers=H,
                   json={'confirm_paid': True, 'idempotency_key': 's4a-gen'})
        job_id = r.json()['jobId']
        # deterministically wait for provider work to be IN FLIGHT, then cancel
        assert first_svg_seen.wait(timeout=15), 'provider work never started'
        c.post(f'/api/projects/{pid}/job/cancel', headers=H, json={'jobId': job_id})
        release_first_svg.set()
        p = _wait(c, pid)
        assert p['job']['status'] == 'canceled', p['job']
        assert p['job']['message'] == 'Cancellation requested. No further generation steps will start.'
        sess = c.get(base).json()
        assert sess['status'] == 'draft_plan'
        # retry with a new key completes (checkpointed fragments may be reused)
        r = c.post(f'{base}/generate', headers=H,
                   json={'confirm_paid': True, 'idempotency_key': 's4a-retry'})
        assert r.status_code == 200
        p = _wait(c, pid)
        assert p['job']['status'] == 'done', p['job']


def test_smoke_4b_cancel_after_finish_is_a_noop(tmp_path, monkeypatch):
    """Deterministic cancel-after-finish: the job is already terminal when the
    cancel arrives — the route is a no-op and the session stays
    ready_to_commit (never forced back to draft, never 'canceled')."""
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-key')
    with TestClient(create_app(tmp_path, transport=_transport([]))) as c:
        pid = _new_project(c)
        sid = _create_session(c, pid)
        base = f'/api/projects/{pid}/generation/sessions/{sid}'
        c.post(f'{base}/plan', headers=H, json={'confirm_paid': True, 'idempotency_key': 's4b-plan'})
        _wait(c, pid)
        r = c.post(f'{base}/generate', headers=H,
                   json={'confirm_paid': True, 'idempotency_key': 's4b-gen'})
        done_job = r.json()['jobId']
        p = _wait(c, pid)
        assert p['job']['status'] == 'done'
        # late cancel targeted at the FINISHED job: no-op
        r = c.post(f'/api/projects/{pid}/job/cancel', headers=H, json={'jobId': done_job})
        assert r.status_code == 200
        p = c.get(f'/api/projects/{pid}').json()
        assert p['job']['status'] == 'done'
        sess = c.get(base).json()
        assert sess['status'] == 'ready_to_commit'


def test_smoke_5_stale_cancel_does_not_kill_new_job(studio):
    c, recorder = studio
    pid = _new_project(c)
    sid = _create_session(c, pid)
    base = f'/api/projects/{pid}/generation/sessions/{sid}'
    c.post(f'{base}/plan', headers=H, json={'confirm_paid': True, 'idempotency_key': 's5-plan'})
    _wait(c, pid)
    r1 = c.post(f'{base}/generate', headers=H,
                json={'confirm_paid': True, 'idempotency_key': 's5-gen-1'})
    old_job = r1.json()['jobId']
    _wait(c, pid)
    c.post(f'{base}/mutate', headers=H,
           json={'mutations': [{'op': 'set_difficulty', 'difficulty': 'hard'}]})
    r2 = c.post(f'{base}/generate', headers=H,
                json={'confirm_paid': True, 'idempotency_key': 's5-gen-2'})
    new_job = r2.json()['jobId']
    assert new_job != old_job
    # stale cancel targeted at the OLD job
    c.post(f'/api/projects/{pid}/job/cancel', headers=H, json={'jobId': old_job})
    p = _wait(c, pid)
    assert p['job']['status'] == 'done', \
        f'stale cancel must not cancel the new job: {p["job"]["status"]}'


# ---------------------------------------------------------------------------
# Smoke 6 — Reference: upload → reload-equivalent (plain session get) →
#           analyze → generate → commit. Source and provenance stay aligned.
# ---------------------------------------------------------------------------

def test_smoke_6_reference_full_journey(studio):
    c, recorder = studio
    pid = _new_project(c, 'Reference journey')
    sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                 json={'mode': 'image_reference', 'requested_difficulty': 'medium'}).json()['id']
    base = f'/api/projects/{pid}/generation/sessions/{sid}'
    # upload the source (free) — this is exactly what the UI shell sends
    r = c.post(f'{base}/source', headers=H,
               files={'file': ('ref.png', _fixture_png(), 'image/png')})
    assert r.status_code == 200
    sha = r.json()['meta']['source']['sha256']
    # reload-equivalent: no client state carried over
    sess = c.get(base).json()
    assert sess['meta']['source']['sha256'] == sha
    # analyze the STORED source (no multipart file)
    r = c.post(f'{base}/reference-plan', headers=H,
               files={'body': (None, json.dumps({'confirm_paid': True, 'idempotency_key': 's6-analyze'}))})
    assert r.status_code == 200
    _wait(c, pid)
    sess = c.get(base).json()
    assert sess['meta']['planOriginSourceSha256'] == sha
    assert sess['scenePlan']['objects']
    # generate → ready → commit
    r = c.post(f'{base}/generate', headers=H,
               json={'confirm_paid': True, 'idempotency_key': 's6-gen'})
    assert r.status_code == 200
    p = _wait(c, pid)
    assert p['job']['status'] == 'done', p['job']
    r = c.post(f'{base}/commit', headers=H, json={'idempotency_key': 's6-commit'})
    assert r.status_code == 200
    rev = r.json()['revision']['id']
    manifest = c.get(f'/api/projects/{pid}/revisions/{rev}/files/artwork.json').json()
    assert manifest['generation']['mode'] == 'image_reference'


# ---------------------------------------------------------------------------
# Smoke 7 — Convert: run → change fidelity (stale) → restore → commit
#           eligibility returns, backend AND UI flag agree.
# ---------------------------------------------------------------------------

def test_smoke_7_convert_fidelity_restore(studio):
    c, recorder = studio
    pid = _new_project(c, 'Convert journey')
    sid = c.post(f'/api/projects/{pid}/generation/sessions', headers=H,
                 json={'mode': 'image_convert', 'requested_difficulty': 'hard',
                       'fidelity': 'balanced'}).json()['id']
    base = f'/api/projects/{pid}/generation/sessions/{sid}'
    r = c.post(f'{base}/source', headers=H,
               files={'file': ('src.png', _fixture_png(), 'image/png')})
    assert r.status_code == 200
    r = c.post(f'{base}/convert', headers=H,
               files={'body': (None, json.dumps({'confirm_paid': True, 'idempotency_key': 's7-convert'}))})
    assert r.status_code == 200
    p = _wait(c, pid)
    assert p['job']['status'] == 'done', p['job']
    sess = c.get(base).json()
    assert sess['status'] == 'ready_to_commit'
    # flag is ABSENT (or False) right after a fresh convert: not stale
    assert not sess['meta'].get('buildInputsStale')
    # change fidelity → stale flag True (UI disables Commit)
    r = c.post(f'{base}/settings', headers=H, json={'fidelity': 'faithful'})
    assert r.status_code == 200
    sess = c.get(base).json()
    assert sess['meta']['buildInputsStale'] is True
    r = c.post(f'{base}/commit', headers=H, json={})
    assert r.status_code == 400
    # restore fidelity → flag False again, no extra provider run
    svg_before = sum(1 for x in recorder if x['path'].endswith('/svg'))
    r = c.post(f'{base}/settings', headers=H, json={'fidelity': 'balanced'})
    assert r.status_code == 200
    sess = c.get(base).json()
    assert sess['meta']['buildInputsStale'] is False
    r = c.post(f'{base}/commit', headers=H, json={})
    assert r.status_code == 200, r.text
    assert sum(1 for x in recorder if x['path'].endswith('/svg')) == svg_before
