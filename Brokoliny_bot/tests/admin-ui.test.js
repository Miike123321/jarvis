const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "..", "admin.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script, "Admin JavaScript not found");

function storage() {
  const values = new Map();
  return {
    getItem: key => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key)
  };
}

function setup() {
  const elements = new Map();
  const handlers = {};
  const document = {
    documentElement: { dataset: {} },
    activeElement: null,
    querySelector: selector => elements.get(selector) || null,
    addEventListener: (event, handler) => { handlers[event] = handler; }
  };
  const status = { textContent: "", dataset: {}, setAttribute() {} };
  const themeToggle = { textContent: "", setAttribute() {}, addEventListener() {} };
  elements.set("#token", { value: "" });
  elements.set("#status", status);
  elements.set("#theme-toggle", themeToggle);
  const textareas = {};
  for (const name of ["diagnostics", "daily_plan_task1", "daily_plan_task2", "daily_plan_advice"]) {
    const textarea = {
      id: `prompt-${name}`,
      value: "active",
      dataset: { original: "active", version: "1" },
      previousElementSibling: { textContent: "" }
    };
    textareas[name] = textarea;
    elements.set(`#prompt-${name}`, textarea);
    elements.set(`#prompt-state-${name}`, { textContent: "" });
  }
  const sessionStorage = storage();
  const window = { addEventListener() {}, confirm: () => true };
  const context = vm.createContext({ document, window, sessionStorage, localStorage: storage() });
  vm.runInContext(script, context);
  return { context, document, elements, handlers, sessionStorage, textareas };
}

test("drafts restore independently and preserve stale-version warning", () => {
  const { context, elements, sessionStorage, textareas } = setup();
  sessionStorage.setItem("promptDraft:diagnostics", JSON.stringify({ text: "draft one", version: "0" }));
  sessionStorage.setItem("promptDraft:daily_plan_task1", JSON.stringify({ text: "draft two", version: "1" }));
  context.restorePromptDraft(textareas.diagnostics, "diagnostics");
  context.restorePromptDraft(textareas.daily_plan_task1, "daily_plan_task1");
  context.updatePromptDirtyState();
  assert.match(elements.get("#prompt-state-diagnostics").textContent, /Активная версия изменилась/);
  assert.match(elements.get("#prompt-state-daily_plan_task1").textContent, /Несохранённый черновик/);
  textareas.diagnostics.value = "draft edited";
  context.handlePromptInput({ currentTarget: textareas.diagnostics });
  assert.equal(JSON.parse(sessionStorage.getItem("promptDraft:diagnostics")).version, "0");
  assert.equal(textareas.daily_plan_task1.value, "draft two");
});

test("saving preserves edits made while the server request is pending", async () => {
  const { context, sessionStorage, textareas } = setup();
  const textarea = textareas.diagnostics;
  textarea.value = "first draft";
  context.handlePromptInput({ currentTarget: textarea });
  let respond;
  context.fetch = () => new Promise(resolve => { respond = resolve; });
  const saving = context.savePrompt("diagnostics");
  textarea.value = "later edit";
  context.handlePromptInput({ currentTarget: textarea });
  respond({ ok: true, json: async () => ({ version: 2 }) });
  await saving;
  assert.equal(textarea.value, "later edit");
  assert.equal(textarea.dataset.original, "first draft");
  assert.equal(textarea.previousElementSibling.textContent, "Промт-шаблон · версия 2");
  const saved = JSON.parse(sessionStorage.getItem("promptDraft:diagnostics"));
  assert.equal(saved.text, "later edit");
  assert.equal(saved.version, "2");
});

test("a failed save keeps the draft and reports the server error", async () => {
  const { context, elements, sessionStorage, textareas } = setup();
  const textarea = textareas.diagnostics;
  textarea.value = "unsaved draft";
  context.handlePromptInput({ currentTarget: textarea });
  context.fetch = async () => ({ ok: false, status: 422, json: async () => ({ detail: "Invalid prompt" }) });
  await context.savePrompt("diagnostics");
  assert.equal(textarea.dataset.original, "active");
  assert.equal(JSON.parse(sessionStorage.getItem("promptDraft:diagnostics")).text, "unsaved draft");
  assert.equal(elements.get("#status").textContent, "Invalid prompt");
});

test("trace drawer moves focus and Escape returns it to the trigger", () => {
  const { context, document, elements, handlers } = setup();
  const trigger = { isConnected: true, focus() { document.activeElement = this; } };
  const close = { focus() { document.activeElement = this; } };
  const drawer = { hidden: true };
  elements.set("#trace-content", { innerHTML: "" });
  elements.set("#trace-drawer", drawer);
  elements.set("#trace-drawer button", close);
  context.showTrace([], trigger);
  assert.equal(drawer.hidden, false);
  assert.equal(document.activeElement, close);
  handlers.keydown({ key: "Escape" });
  assert.equal(drawer.hidden, true);
  assert.equal(document.activeElement, trigger);
});
