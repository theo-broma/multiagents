// A DOM small enough to run the monitor page's own JavaScript under node.
//
// Not a browser and not trying to be. It exists because the page is the one
// part of this project that nothing else in the test suite executes, so a typo
// or a wrong property access there would first show up as a blank panel at the
// moment somebody needed it. Two modes:
//
//   render_page.js <page.html> <fixture.json>
//       renders every view against a real snapshot and prints their sizes.
//
//   render_page.js <page.html> <fixture.json> --scroll
//       drives the 2s poll twice and reports whether the view was rebuilt and
//       whether scroll positions survived. This is the mode that would have
//       caught the activity log snapping back to the top while being read.
//
// Called from tests/test_core.py, skipped where node is not installed.

function makeNode(tag) {
  const node = {
    nodeType: 1, tagName: tag, className: "", children: [], attrs: {}, text: "",
    style: {}, dataset: {}, hidden: false, value: "", checked: false,
    scrollTop: 0, rebuilds: 0, listeners: {},
    classList: { toggle() {}, add() {}, contains: () => false },
    setAttribute(key, value) {
      this.attrs[key] = value;
      // The real DOM exposes data-* attributes through .dataset, and
      // keepingScroll() addresses its containers by exactly that.
      if (key.startsWith("data-")) this.dataset[key.slice(5)] = value;
    },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    append(...kids) { this.children.push(...kids.flat()); },
    replaceChildren(...kids) { this.rebuilds += 1; this.children = kids.flat(); },
    querySelector: () => makeNode("div"),
    querySelectorAll(selector) {
      const want = (selector.match(/^\[data-([\w-]+)\]$/) || [])[1];
      const found = [];
      (function walk(n) {
        if (!n || n.nodeType !== 1) return;
        if (want && n.dataset[want] !== undefined) found.push(n);
        (n.children || []).forEach(walk);
      })(this);
      return found;
    },
    get textContent() { return this.text; },
    set textContent(v) { this.text = v; },
    get innerHTML() { return ""; }, set innerHTML(v) { },
  };
  return node;
}

const ids = {};
global.document = {
  createElement: (tag) => makeNode(tag),
  createTextNode: (text) => ({ nodeType: 3, text: String(text) }),
  querySelector: (selector) => (ids[selector] ||= makeNode("div")),
  querySelectorAll: () => [],
  hidden: false,
};
global.window = { scrollY: 0, scrollTo(_x, y) { this.scrollY = y; } };
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.clearTimeout = () => { };
global.confirm = () => true;
global.prompt = () => "x";

const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const js = html.match(/<script>([\s\S]*)<\/script>/)[1];
const fixture = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const mode = process.argv[4] || "";

if (mode !== "--scroll") {
  const run = new Function(js + `
    STATE = arguments[0];
    SETTINGS = arguments[1];
    const painted = {};
    for (const tab of ["live", "config", "history", "costs"]) {
      TAB = tab;
      const view = { live: renderLive, config: renderConfig,
                     history: renderHistory, costs: renderCosts }[tab];
      painted[tab] = JSON.stringify(view()).length;
    }
    SELECTED = STATE.history[0] && STATE.history[0].id;
    TRANSCRIPT = { id: SELECTED, prompt: "p",
                   entries: [{ kind: "text", tool: "", text: "hello" }] };
    painted.transcript = JSON.stringify(transcriptPanel()).length;
    OPEN.add(SELECTED);
    painted.expanded = JSON.stringify(renderHistory()).length;
    return painted;
  `);
  console.log("rendered:", JSON.stringify(run(fixture.state, fixture.settings)));
} else {
  // Two polls of identical state, then one that differs, with the activity log
  // open and scrolled. Reports what the reader would have experienced.
  const run = new Function(js + `
    const first = arguments[0];
    const changed = JSON.parse(JSON.stringify(first));
    changed.counts.running = (changed.counts.running || 0) + 1;
    const answers = [first, first, changed];
    let call = 0;
    global.fetch = async () => ({ json: async () => answers[Math.min(call++, 2)] });

    const host = document.querySelector("#live");
    TAB = "live";
    OPEN_DETAILS.add("activity");
    EVENTS = [{ t: 1, agent: "a", kind: "start" }, { t: 2, agent: "b", kind: "end" }];

    return (async () => {
      await refresh();                       // first paint
      const events = host.querySelectorAll("[data-scroll]")[0];
      events.scrollTop = 240;
      window.scrollY = 900;
      const afterFirst = host.rebuilds;

      await refresh();                       // identical state
      const idleRebuilds = host.rebuilds - afterFirst;

      await refresh();                       // something actually changed
      const changedRebuilds = host.rebuilds - afterFirst - idleRebuilds;
      const kept = host.querySelectorAll("[data-scroll]")[0];
      return {
        idleRebuilds, changedRebuilds,
        scrollKept: kept ? kept.scrollTop : -1,
        pageScrollKept: window.scrollY,
        eventsRendered: EVENTS.length,
      };
    })();
  `);
  run(fixture.state).then((out) => console.log("scroll:", JSON.stringify(out)));
}
