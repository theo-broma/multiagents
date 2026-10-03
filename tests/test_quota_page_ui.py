"""QD-R3/QD-R7: quota cards and explicit, memory-only identity reveals."""

import http.client
import json
import re
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer

import pytest

from multiagents.monitor import server


@pytest.fixture
def quota_html(monkeypatch):
    monkeypatch.setattr(server.Handler, "token", "quota-ui-token")
    monkeypatch.setattr(server.Handler, "bound_host", "127.0.0.1")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=10)
    try:
        conn.request("POST", "/quota", body="token=quota-ui-token")
        response = conn.getresponse()
        assert response.status == 200
        yield response.read().decode()
    finally:
        conn.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_qd_r3_providers_use_a_responsive_card_grid(quota_html):
    css = re.search(r"#providers\s*\{([^}]+)\}", quota_html).group(1)
    assert re.search(r"display:\s*grid", css)
    assert "grid-template-columns:" in css
    assert "auto-fill" in css and "minmax(" in css
    assert "gap:" in css


def test_qd_r3_masking_has_no_timer_and_refresh_preserves_clear_values(quota_html):
    timers = re.findall(r"set(?:Timeout|Interval)\([^;]+", quota_html)
    assert timers == ["setInterval(refresh, 2000)"]
    refresh = quota_html.split("async function refresh()", 1)[1]
    assert "mask()" not in refresh
    assert "revealed.delete" not in refresh and "revealed.clear" not in refresh
    assert "const revealed = new Map();" in quota_html
    assert "JSON.stringify([provider, entry.account])" in quota_html
    assert "revealed.set(key, value.textContent)" in quota_html
    assert "revealed.get(key)" in quota_html


def test_qd_r3_eye_precedes_identity_and_has_fixed_width(quota_html):
    assert "row.append(label, eye, value)" in quota_html
    css = re.search(r"\.identity button\s*\{([^}]+)\}", quota_html).group(1)
    assert "width: 36px" in css and "flex: 0 0 36px" in css


def test_qd_r7_rendering_uses_no_html_or_browser_storage(quota_html):
    for forbidden in ("innerHTML", "localStorage", "sessionStorage"):
        assert forbidden not in quota_html
    assert "textContent" in quota_html


def test_qd_r7_stale_and_error_paths_sync_button_from_revealed_map(quota_html):
    assert 'eye.setAttribute("aria-pressed", String(shown))' in quota_html
    assert "const shown = revealed.has(key)" in quota_html
    stale = quota_html.split("if (sequence !== request", 1)[1].split("pending = false;", 1)[0]
    assert "sync();" in stale
    error = quota_html.split("} catch (_) {", 1)[1].split("row.append", 1)[0]
    assert 'sync("unknown")' in error and "sync();" in error


@pytest.mark.parametrize("scenario", ["error", "busy", "stale", "masked", "preserved"])
def test_qd_r7_reveal_retry_and_refresh_states_in_node(quota_html, scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    script = re.search(r"<script>([\s\S]*)</script>", quota_html).group(1)
    harness = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
function element() {
  return {
    children: [], attrs: {}, listeners: {}, isConnected: true,
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(k, v) { this.listeners[k] = v; },
    append(...v) { this.children.push(...v); },
    replaceChildren() { this.children = []; }
  };
}
const ids = {providers: element(), refresh: element(), status: element()};
global.document = {createElement: element, getElementById: id => ids[id]};
global.setInterval = () => 0;
const data = {providers: [{name: "provider", identities: [{account: "a", identity_available: true}]}]};
let failRefresh = false;
let requests = [];
global.fetch = async path => {
  if (path === "/api/quota") {
    if (failRefresh) throw new Error("offline");
    return {ok: true, json: async () => data};
  }
  return new Promise((resolve, reject) => requests.push({
    resolve: result => resolve({ok: true, json: async () => result}), reject
  }));
};
vm.runInThisContext(SCRIPT);
(async () => {
  await new Promise(resolve => setImmediate(resolve));
  const row = ids.providers.children[0].children.find(n => n.className === "identity");
  const eye = row.children[1], value = row.children[2];
  const click = () => eye.listeners.click();
  const pending = click();
  assert.equal(eye.attrs["aria-pressed"], "false");
  if (SCENARIO === "error") requests[0].reject(new Error("offline"));
  if (SCENARIO === "busy") requests[0].resolve({status: "busy"});
  if (SCENARIO === "stale") {
    failRefresh = true;
    await ids.refresh.listeners.click();
    assert.equal(value.textContent, "*****");
    assert.equal(eye.attrs["aria-pressed"], "false");
    requests[0].resolve({identity: "stale@example.com"});
  }
  if (SCENARIO === "masked") {
    await click();
    requests[0].resolve({identity: "stale@example.com"});
  }
  if (SCENARIO === "preserved") requests[0].resolve({identity: "clear@example.com"});
  await pending;
  if (SCENARIO === "preserved") {
    failRefresh = true;
    await ids.refresh.listeners.click();
    assert.equal(value.textContent, "clear@example.com");
    assert.equal(eye.attrs["aria-pressed"], "true");
    await click();
    assert.equal(value.textContent, "*****");
    assert.equal(eye.attrs["aria-pressed"], "false");
  } else {
    assert.equal(eye.attrs["aria-pressed"], "false");
    assert.notEqual(value.textContent, "stale@example.com");
    const retry = click();
    assert.equal(requests.length, 2);
    requests[1].resolve({identity: "retry@example.com"});
    await retry;
    assert.equal(value.textContent, "retry@example.com");
    assert.equal(eye.attrs["aria-pressed"], "true");
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    harness = "const SCRIPT = " + json.dumps(script) + ";\nconst SCENARIO = " + json.dumps(scenario) + ";\n" + harness
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
