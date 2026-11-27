import test from "node:test";
import { runDashboardBrowserTest } from "./browser_runner.mjs";

function setup() {
  const product = {
    id: "soda", name: "Soda Package", product: "soda", prefix: "bev",
    category: "Drinks", currency: "USD", price: 80, price_available: true,
  };
  const cruises = ["A", "B"].map((id) => ({ id, ship: `Ship ${id}`, sail_date: "2025-08-15", currency: "USD" }));
  const state = {
    active_cruise_id: "A", cruises, config: cruises[0],
    catalog: { items: [product], generated_at: "2025-08-15T12:00:00Z" },
    preferences: { watching: { soda: { target_price: 75 } }, pinned: [] },
    missing_watched_items: [], status: {},
  };
  const $ = (selector) => document.querySelector(selector);
  const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const responses = [];
  const mutations = [];
  const fixture = { $, tick, assert, state, responses, mutations };
  window.dashboardTest = fixture;
  const realInterval = window.setInterval.bind(window);
  window.setInterval = (callback, delay) => {
    if (delay !== 30_000) return realInterval(callback, delay);
    fixture.poll = callback;
    return 1;
  };
  const json = (value) => ({
    ok: true, headers: { get: () => "application/json" }, json: async () => structuredClone(value),
  });
  window.fetch = async (url, options = {}) => {
    const path = url.split("./api/")[1].split("?")[0];
    if (path === "state") return json(state);
    const [, cruise, action, item, method] = path.split("/");
    if (action === "activate") {
      state.active_cruise_id = cruise;
      state.config = cruises.find((entry) => entry.id === cruise);
      return json({ state });
    }
    if (action === "changes") return json({
      changes: [{ item: product, available: true, previous_available: true, price: 80,
        previous_price: 85, price_delta: -5, observed_at: state.catalog.generated_at }],
      watched_latest: {}, price_stats: {},
    });
    if (method === "history") {
      return new Promise((resolve, reject) => {
        responses.push({ cruise, reject, finish() {
          const price = cruise === "A" ? 20 : 65;
          resolve(json({
            item: product, sailing: { currency: "USD" }, observed_through: state.catalog.generated_at,
            summary: { events: 1, current_price: price, lowest_price: price, highest_price: price },
            points: [{ available: true, price, observed_at: "2025-08-10T12:00:00Z" }],
          }));
        } });
      });
    }
    if (method === "target" || method === "watch") {
      const body = JSON.parse(options.body);
      mutations.push({ cruise, item, method, body });
      if (method === "target") state.preferences.watching[item].target_price = body.target_price;
      else if (!body.watching) {
        delete state.preferences.watching[item];
        state.missing_watched_items = state.missing_watched_items.filter((entry) => entry.id !== item);
      }
      return json({ state });
    }
    throw new Error(`Unexpected fixture endpoint: ${path}`);
  };
  fixture.switchTo = async (cruise) => {
    $("#cruise-select").value = cruise;
    $("#cruise-select").dispatchEvent(new Event("change"));
    await tick();
  };
}

test("history responses from an earlier sailing cannot fill or unlock the active history view", () =>
  runDashboardBrowserTest(setup, async () => {
    const { $, assert, tick, responses, switchTo } = window.dashboardTest;
    await tick();
    for (const staleFirst of [true, false]) {
      $(".history-action").click();
      await tick();
      await switchTo("B");
      $(".history-action").click();
      await tick();
      const [oldRequest, currentRequest] = responses.splice(0);
      assert(oldRequest.cruise === "A" && currentRequest.cruise === "B", "Both sailings must request their own history");
      if (staleFirst) {
        oldRequest.finish();
        await tick();
        assert($(".history-loading"), "A's response cleared B's loading state");
        assert(responses.length === 0, "A's response started a duplicate B request");
        currentRequest.finish();
      } else {
        currentRequest.finish();
        await tick();
        oldRequest.reject(new Error("old request failed"));
      }
      await tick();
      assert($(".history-stat-value")?.textContent.includes("65.00"), "Only B's price should appear");
      assert(!$(".history-error"), "A's failure must not replace B's history");
      await switchTo("A");
    }
  }));

test("late poll and Changes responses leave the selected sailing and dialog alone", () =>
  runDashboardBrowserTest(setup, async () => {
    const { $, assert, poll, responses, state, switchTo, tick } = window.dashboardTest;
    await tick();
    $('[data-tab="changes"]').click();
    $(".history-action").click();
    await tick();

    const previousFetch = window.fetch;
    let completePoll;
    window.fetch = (url, options) => {
      if (url !== "./api/state") return previousFetch(url, options);
      const snapshot = structuredClone(state);
      return new Promise((resolve) => {
        completePoll = () => resolve(new Response(JSON.stringify(snapshot), {
          headers: { "Content-Type": "application/json" },
        }));
      });
    };
    const pending = poll();
    await switchTo("B");
    completePoll();
    await pending;
    responses[0].finish();
    await tick();
    assert($("#cruise-select").value === "B", "An earlier poll reactivated A");
    assert(!$("#history-chart-dialog").open, "An old Changes history modal opened on B");
  }));

test("polling preserves the focused price editor and draft while other prices update", () =>
  runDashboardBrowserTest(setup, async () => {
    const { $, tick, assert, state, mutations, poll, switchTo } = window.dashboardTest;
    await tick();
    const input = $(".target-input");
    input.focus();
    input.value = "68.75";
    input.dispatchEvent(new Event("input"));
    state.catalog.items[0].price = 77;
    state.catalog.generated_at = "2025-08-16T12:00:00Z";
    await poll();
    assert(document.activeElement === input && input.isConnected, "Poll replaced the focused number input");
    assert(input.value === "68.75", "Poll overwrote the edited target");
    assert($(".price").textContent.includes("77.00"), "The edited row's current price was not refreshed");
    input.blur();
    await poll();
    assert($(".target-input").value === "68.75", "An unfocused pending edit was discarded");
    $(".target-editor button").click();
    await tick();
    assert(mutations[0]?.body.target_price === 68.75, "Save did not send the draft target");
    assert(state.preferences.watching.soda.target_price === 68.75, "Target did not persist");
    $(".target-input").value = "1";
    $(".target-input").dispatchEvent(new Event("input"));
    await switchTo("B");
    assert($(".target-input").value === "68.75", "An unsaved target leaked to another sailing");
  }));

test("a missing watch remains visible and removable without entering the catalog", () =>
  runDashboardBrowserTest(setup, async () => {
    const { $, tick, assert, state, mutations, poll } = window.dashboardTest;
    await tick();
    const missing = { ...state.catalog.items[0], id: "gone", product: "gone", name: "Old excursion",
      category: "Shore", price: null, price_available: false };
    state.missing_watched_items = [missing];
    state.preferences.watching.gone = { target_price: 20 };
    await poll();
    assert(!document.body.textContent.includes("Old excursion"), "Missing watch leaked into All");
    $('[data-tab="watching"]').click();
    const row = $('[data-item-id="gone"]');
    assert(row?.textContent.includes("Missing from latest catalog"), "Missing watch is not identified");
    assert(!row.querySelector(".pin-action"), "Missing product offered an unsupported pin action");
    row.querySelector(".unwatch").click();
    await tick();
    assert(mutations[0]?.item === "gone" && mutations[0].body.watching === false, "Removal did not target the missing watch");
    assert(!$('[data-item-id="gone"]') && $("#watch-count").textContent === "1 item", "Removed watch still appears");
  }));

test("history charts end at the last confirmed observation for a completed cruise", () =>
  runDashboardBrowserTest(setup, async () => {
    const { $, tick, assert, responses, state, poll } = window.dashboardTest;
    await tick();
    state.status = { completed: true, return_date: "2025-08-18" };
    await poll();
    $(".history-action").click();
    await tick();
    responses[0].finish();
    await tick();
    const expected = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" })
      .format(new Date(state.catalog.generated_at));
    assert($(".history-chart text[text-anchor=end]:last-child")?.textContent === expected, "Chart extends beyond the last check");
    $(".history-chart-view button").click();
    assert($("#history-chart-dialog .history-chart text:last-child")?.textContent === expected, "Expanded chart end differs");
  }));
