// A DOM small enough to run the monitor page's render functions under node.
//
// Not a browser and not trying to be: it exists so that a typo or a wrong
// property access in the page's JavaScript fails in the test suite rather than
// as a blank panel somebody notices while something is going wrong. Called by
// test_the_page_renders_every_view_against_real_data, skipped where node is
// not installed.
const nodes = [];
function makeNode(tag) {
  return {
    nodeType: 1, tagName: tag, className: "", children: [], attrs: {}, text: "",
    style: {}, dataset: {}, hidden: false, value: "", checked: false,
    classList: { toggle() {}, add() {}, contains: () => false },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener() {},
    append(...kids) { this.children.push(...kids); },
    replaceChildren(...kids) { this.children = kids; },
    querySelector: () => makeNode("div"),
    get textContent() { return this.text; },
    set textContent(v) { this.text = v; },
    get innerHTML() { return ""; }, set innerHTML(v) {},
  };
}
const ids = {};
global.document = {
  createElement: (t) => { const n = makeNode(t); nodes.push(n); return n; },
  createTextNode: (t) => ({ nodeType: 3, text: String(t) }),
  querySelector: (s) => (ids[s] ||= makeNode("div")),
  querySelectorAll: () => [],
  hidden: false,
};
global.fetch = async () => ({ json: async () => ({}) });
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.clearTimeout = () => {};
global.confirm = () => true;
global.prompt = () => "x";

const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const js = html.match(/<script>([\s\S]*)<\/script>/)[1];
const fixture = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

// Run the page's code, then drive it with real data.
const run = new Function(js + `
  STATE = arguments[0];
  SETTINGS = arguments[1];
  const painted = {};
  for (const tab of ["live", "config", "history", "costs"]) {
    TAB = tab;
    const view = { live: renderLive, config: renderConfig,
                   history: renderHistory, costs: renderCosts }[tab];
    const out = view();
    painted[tab] = JSON.stringify(out).length;
  }
  SELECTED = STATE.history[0] && STATE.history[0].id;
  TRANSCRIPT = { id: SELECTED, prompt: "p", entries: [{ kind: "text", tool: "", text: "hello" }] };
  painted.transcript = JSON.stringify(transcriptPanel()).length;
  OPEN.add(SELECTED);
  painted.expanded = JSON.stringify(renderHistory()).length;
  return painted;
`);
console.log("rendered:", JSON.stringify(run(fixture.state, fixture.settings)));
