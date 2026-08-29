"use strict";

const APP_VERSION = "0.6.2";
const ALERT_TIPS_STORAGE_KEY = "royal-price-dashboard.alert-tips-dismissed";
const CHANGE_VISIT_STORAGE_PREFIX = "royal-price-dashboard.change-visit.";

const model = {
  state: null,
  switchingCruise: false,
  removingCruise: false,
  activeTab: "all",
  search: "",
  category: "",
  sort: "category",
  busyItems: new Set(),
  openDescriptions: new Set(),
  openHistory: new Set(),
  busyHistory: new Set(),
  historyCache: new Map(),
  changeScope: "all",
  changePeriod: "since",
  changeSessions: new Map(),
  changesVisitedAt: null,
  changesSince: null,
  changes: null,
  changesLoading: false,
  changesError: "",
  changesRequestKey: null,
  watchedLatestChanges: new Map(),
  watchedPriceStats: new Map(),
  alertTipsDismissed: false,
  onboarding: {
    open: false,
    step: "line",
    cruiseLine: null,
    ships: [],
    shipSearch: "",
    selectedShip: null,
    sailings: [],
    selectedSailing: null,
    currency: "USD",
    notificationsEnabled: true,
    busy: false,
    error: "",
  },
};

const elements = {
  dashboard: document.querySelector("#dashboard"),
  sailingSummary: document.querySelector("#sailing-summary"),
  cruisePicker: document.querySelector("#cruise-picker"),
  cruiseSelect: document.querySelector("#cruise-select"),
  addCruiseButton: document.querySelector("#add-cruise-button"),
  removeCruiseButton: document.querySelector("#remove-cruise-button"),
  completedCruiseNotice: document.querySelector("#completed-cruise-notice"),
  completedCruiseCopy: document.querySelector("#completed-cruise-copy"),
  completedRemoveButton: document.querySelector("#completed-remove-button"),
  alertTips: document.querySelector("#alert-tips"),
  dismissAlertTips: document.querySelector("#dismiss-alert-tips"),
  showAlertTips: document.querySelector("#show-alert-tips"),
  setupPanel: document.querySelector("#setup-panel"),
  setupTitle: document.querySelector("#setup-title"),
  setupIntro: document.querySelector("#setup-intro"),
  setupCancel: document.querySelector("#setup-cancel"),
  setupError: document.querySelector("#setup-error"),
  setupBody: document.querySelector("#setup-body"),
  setupProgress: [...document.querySelectorAll("[data-setup-progress]")],
  lastRefresh: document.querySelector("#last-refresh"),
  catalogCount: document.querySelector("#catalog-count"),
  watchCount: document.querySelector("#watch-count"),
  pinnedCount: document.querySelector("#pinned-count"),
  storageStatus: document.querySelector("#storage-status"),
  storageUsage: document.querySelector("#storage-usage"),
  storageFree: document.querySelector("#storage-free"),
  errorBanner: document.querySelector("#error-banner"),
  warningBanner: document.querySelector("#warning-banner"),
  searchInput: document.querySelector("#search-input"),
  categorySelect: document.querySelector("#category-select"),
  sortSelect: document.querySelector("#sort-select"),
  filters: document.querySelector("#catalog-filters"),
  changesControls: document.querySelector("#changes-controls"),
  changesHeading: document.querySelector("#changes-heading"),
  changesPeriod: document.querySelector("#changes-period"),
  changePeriodButtons: [...document.querySelectorAll("[data-change-period]")],
  changeScopeButtons: [...document.querySelectorAll("[data-change-scope]")],
  resultsSummary: document.querySelector("#results-summary"),
  catalog: document.querySelector("#catalog"),
  emptyState: document.querySelector("#empty-state"),
  emptyTitle: document.querySelector("#empty-title"),
  emptyCopy: document.querySelector("#empty-copy"),
  refreshButton: document.querySelector("#refresh-button"),
  exportButton: document.querySelector("#export-button"),
  toast: document.querySelector("#toast"),
  tabs: [...document.querySelectorAll(".tab")],
  historyChartDialog: document.querySelector("#history-chart-dialog"),
  historyChartDialogTitle: document.querySelector("#history-chart-dialog-title"),
  historyChartDialogBody: document.querySelector("#history-chart-dialog-body"),
  historyChartDialogClose: document.querySelector("#history-chart-dialog-close"),
};

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}

function countLabel(count) {
  return `${count} item${count === 1 ? "" : "s"}`;
}

function formatBytes(value) {
  if (value === null || value === undefined || value === "") return "Unavailable";
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) return "Unavailable";
  const units = ["bytes", "KiB", "MiB", "GiB", "TiB"];
  let amount = parsed;
  for (const unit of units) {
    if (amount < 1024 || unit === "TiB") {
      return unit === "bytes"
        ? `${Math.round(amount)} ${unit}`
        : `${amount.toFixed(1)} ${unit}`;
    }
    amount /= 1024;
  }
  return "Unavailable";
}

function readAlertTipsPreference() {
  try {
    return window.localStorage.getItem(ALERT_TIPS_STORAGE_KEY) === "true";
  } catch (_error) {
    return false;
  }
}

function renderAlertTips() {
  elements.alertTips.classList.toggle("hidden", model.alertTipsDismissed);
  elements.showAlertTips.classList.toggle("hidden", !model.alertTipsDismissed);
}

function setAlertTipsDismissed(dismissed) {
  model.alertTipsDismissed = dismissed;
  try {
    if (dismissed) {
      window.localStorage.setItem(ALERT_TIPS_STORAGE_KEY, "true");
    } else {
      window.localStorage.removeItem(ALERT_TIPS_STORAGE_KEY);
    }
  } catch (_error) {
    // The dashboard remains usable when a WebView blocks local storage.
  }
  renderAlertTips();
}

function formatCurrency(value, currency) {
  if (value === null || value === undefined) return "Unavailable";
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
    }).format(value);
  } catch (_error) {
    return `${Number(value).toFixed(2)} ${currency}`;
  }
}

function formatPrice(item) {
  if (item.price === null || item.price === undefined) return "Price unavailable";
  return formatCurrency(item.price, item.currency);
}

function formatTimestamp(value) {
  if (!value) return "Never";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsed);
}

function formatSailingDate(value, options = {}) {
  if (!value) return "Date unavailable";
  const parsed = new Date(`${value}T12:00:00`);
  if (Number.isNaN(parsed.valueOf())) return value;
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: options.short ? "short" : "long",
    day: "numeric",
  }).format(parsed);
}

function activeCruiseId() {
  return model.state?.active_cruise_id || null;
}

function preferredTabForState(state) {
  return state?.preferences?.pinned?.length ? "pinned" : "all";
}

function setActiveTab(tabName) {
  model.activeTab = tabName;
  elements.tabs.forEach((tab) =>
    tab.classList.toggle("active", tab.dataset.tab === tabName));
}

function boundedChangeSince(visitedAt, now = new Date()) {
  if (!visitedAt) return null;
  const visited = new Date(visitedAt);
  if (Number.isNaN(visited.valueOf())) return null;
  const startOfYesterday = new Date(now.valueOf());
  startOfYesterday.setHours(0, 0, 0, 0);
  startOfYesterday.setDate(startOfYesterday.getDate() - 1);
  return new Date(
    Math.min(visited.valueOf(), startOfYesterday.valueOf()),
  ).toISOString();
}

function initializeChangeSession(cruiseId) {
  model.changesVisitedAt = null;
  model.changesSince = null;
  if (!cruiseId) return;
  if (model.changeSessions.has(cruiseId)) {
    const session = model.changeSessions.get(cruiseId);
    model.changesVisitedAt = session.visitedAt;
    model.changesSince = session.since;
    return;
  }

  let visitedAt = null;
  const openedAt = new Date().toISOString();
  try {
    const key = `${CHANGE_VISIT_STORAGE_PREFIX}${cruiseId}`;
    visitedAt = window.localStorage.getItem(key);
    if (visitedAt && Number.isNaN(new Date(visitedAt).valueOf())) visitedAt = null;
  } catch (_error) {
    visitedAt = null;
  }
  const since = boundedChangeSince(visitedAt);
  const session = { visitedAt, since, openedAt, committed: false };
  model.changeSessions.set(cruiseId, session);
  model.changesVisitedAt = visitedAt;
  model.changesSince = since;
}

function commitChangeSession(cruiseId) {
  const session = model.changeSessions.get(cruiseId);
  if (!session || session.committed) return;
  try {
    window.localStorage.setItem(
      `${CHANGE_VISIT_STORAGE_PREFIX}${cruiseId}`,
      session.openedAt,
    );
  } catch (_error) {
    // A blocked browser store only disables cross-visit tracking.
  }
  session.committed = true;
}

function invalidateChanges({ clearLatest = true } = {}) {
  model.changes = null;
  model.changesLoading = false;
  model.changesError = "";
  model.changesRequestKey = null;
  if (clearLatest) {
    model.watchedLatestChanges.clear();
    model.watchedPriceStats.clear();
  }
}

function resetCatalogView(nextState) {
  setActiveTab(preferredTabForState(nextState));
  model.search = "";
  model.category = "";
  model.openDescriptions.clear();
  model.openHistory.clear();
  model.busyHistory.clear();
  model.historyCache.clear();
  model.busyItems.clear();
  model.changeScope = "all";
  model.changePeriod = "since";
  initializeChangeSession(nextState?.active_cruise_id || null);
  invalidateChanges();
  elements.searchInput.value = "";
  elements.categorySelect.value = "";
}

function acceptState(nextState) {
  const priorCruiseId = activeCruiseId();
  const priorGeneratedAt = model.state?.catalog?.generated_at;
  const priorWatches = Object.keys(model.state?.preferences?.watching || {}).sort().join("\n");
  const nextCruiseId = nextState?.active_cruise_id || null;
  const nextWatches = Object.keys(nextState?.preferences?.watching || {}).sort().join("\n");
  if (!model.state || priorCruiseId !== nextCruiseId) {
    resetCatalogView(nextState);
  } else if (priorGeneratedAt && priorGeneratedAt !== nextState?.catalog?.generated_at) {
    model.historyCache.clear();
    invalidateChanges();
  } else if (priorWatches !== nextWatches) {
    invalidateChanges();
  }
  model.state = nextState;
  if (nextState?.setup_required) model.onboarding.open = true;
}

async function request(path, options = {}) {
  const response = await fetch(`./api/${path}`, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    throw new Error(payload?.error || payload || `Request failed (${response.status})`);
  }
  return payload;
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.remove("hidden");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => elements.toast.classList.add("hidden"), 3200);
}

function formatCooldown(seconds) {
  const minutes = Math.max(1, Math.ceil(Number(seconds || 0) / 60));
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
}

async function loadState({ quiet = false } = {}) {
  try {
    const nextState = await request("state");
    acceptState(nextState);
    render();
    await loadChanges();
  } catch (error) {
    if (!quiet) showToast(error.message);
    elements.errorBanner.textContent = error.message;
    elements.errorBanner.classList.remove("hidden");
  }
}

function currentData() {
  const catalog = model.state?.catalog || { items: [] };
  const preferences = model.state?.preferences || { pinned: [], watching: {} };
  return {
    catalog,
    preferences,
    items: catalog.items || [],
    pinned: new Set(preferences.pinned || []),
    watching: preferences.watching || {},
  };
}

function updateCategoryOptions(items) {
  const prior = elements.categorySelect.value;
  const categories = [...new Set(items.map((item) => item.category).filter(Boolean))]
    .sort((left, right) => left.localeCompare(right));
  elements.categorySelect.replaceChildren();
  const all = node("option", "", "All categories");
  all.value = "";
  elements.categorySelect.append(all);
  for (const category of categories) {
    const option = node("option", "", category);
    option.value = category;
    elements.categorySelect.append(option);
  }
  elements.categorySelect.value = categories.includes(prior) ? prior : "";
  model.category = elements.categorySelect.value;
}

function filteredItems(data) {
  const search = model.search.trim().toLocaleLowerCase();
  const result = data.items.filter((item) => {
    const isPinned = data.pinned.has(item.id);
    const isWatching = Boolean(data.watching[item.id]);
    if (model.activeTab === "watching" && !isWatching) return false;
    if (model.activeTab === "pinned" && !isPinned) return false;
    if (model.category && item.category !== model.category) return false;
    if (search) {
      const haystack = [
        item.name,
        item.category,
        item.subcategory,
        item.description,
        item.prefix,
        item.product,
      ].filter(Boolean).join(" ").toLocaleLowerCase();
      if (!haystack.includes(search)) return false;
    }
    return true;
  });

  const byName = (left, right) => left.name.localeCompare(right.name);
  if (model.sort === "name") result.sort(byName);
  if (model.sort === "price-asc") {
    result.sort((left, right) => {
      const leftPrice = left.price ?? Number.POSITIVE_INFINITY;
      const rightPrice = right.price ?? Number.POSITIVE_INFINITY;
      return leftPrice - rightPrice || byName(left, right);
    });
  }
  if (model.sort === "price-desc") {
    result.sort((left, right) => {
      const leftPrice = left.price ?? Number.NEGATIVE_INFINITY;
      const rightPrice = right.price ?? Number.NEGATIVE_INFINITY;
      return rightPrice - leftPrice || byName(left, right);
    });
  }
  if (model.sort === "category") {
    result.sort((left, right) =>
      left.category.localeCompare(right.category)
      || (left.subcategory || "").localeCompare(right.subcategory || "")
      || byName(left, right));
  }
  return result;
}

async function mutateItem(itemId, action, body, successMessage) {
  if (model.busyItems.has(itemId)) return;
  const cruiseId = activeCruiseId();
  if (!cruiseId) return;
  model.busyItems.add(itemId);
  renderCatalog();
  try {
    const response = await request(
      `cruises/${encodeURIComponent(cruiseId)}/items/${encodeURIComponent(itemId)}/${action}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    );
    acceptState(response.state);
    if (
      action === "pin"
      && model.activeTab === "pinned"
      && !response.state?.preferences?.pinned?.length
    ) {
      setActiveTab("all");
    }
    showToast(successMessage);
  } catch (error) {
    showToast(error.message);
  } finally {
    model.busyItems.delete(itemId);
    render();
    await loadChanges();
  }
}

function createBadge(text, className) {
  return node("span", `badge ${className}`, text);
}

function createPriceBadge(item) {
  const stats = model.watchedPriceStats.get(item.id);
  if (!stats?.record_low && !stats?.below_average) return null;
  const count = Number(stats.recorded_price_count);
  const average = formatCurrency(stats.average_price, item.currency);
  const current = formatCurrency(stats.current_price, item.currency);
  const priceCount = `${count} recorded price${count === 1 ? "" : "s"}`;
  const recordLow = Boolean(stats.record_low);
  const badge = createBadge(
    recordLow ? "Record low" : "Below average",
    recordLow ? "record-low" : "below-average",
  );
  const explanation = recordLow
    ? `Record low. ${current} is the lowest of ${priceCount}; the average is ${average}.`
    : `Below average. ${current} is below the ${average} average across ${priceCount}.`;
  badge.title = explanation;
  badge.setAttribute("aria-label", explanation);
  return badge;
}

function historyPanelId(itemId) {
  return `history-${String(itemId).replace(/[^A-Za-z0-9_-]/g, "_")}`;
}

function svgNode(tag, attributes = {}, text) {
  const result = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) {
    result.setAttribute(name, String(value));
  }
  if (text !== undefined) result.textContent = text;
  return result;
}

function shortDate(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(parsed);
}

function changeRequestKey() {
  const cruiseId = activeCruiseId();
  if (!cruiseId) return null;
  const generatedAt = model.state?.catalog?.generated_at || "no-catalog";
  const watches = Object.keys(model.state?.preferences?.watching || {}).sort().join(",");
  return [
    cruiseId,
    generatedAt,
    model.changeScope,
    model.changePeriod,
    model.changePeriod === "since" ? model.changesSince || "recent" : "all",
    watches,
  ].join("|");
}

async function loadChanges({ force = false } = {}) {
  const cruiseId = activeCruiseId();
  const requestKey = changeRequestKey();
  if (!cruiseId || !requestKey) return;
  if (
    !force
    && model.changesRequestKey === requestKey
    && (model.changes || model.changesLoading)
  ) return;

  model.changesLoading = true;
  model.changesError = "";
  model.changesRequestKey = requestKey;
  renderCatalog();
  const query = new URLSearchParams({
    scope: model.changeScope,
    limit: model.changePeriod === "all" ? "500" : "100",
  });
  if (model.changePeriod === "all") {
    query.set("latest_only", "true");
  } else if (model.changesSince) {
    query.set("since", model.changesSince);
  }
  try {
    const response = await request(
      `cruises/${encodeURIComponent(cruiseId)}/changes?${query.toString()}`,
    );
    if (changeRequestKey() !== requestKey) return;
    model.changes = response;
    model.watchedLatestChanges = new Map(
      Object.entries(response.watched_latest || {}),
    );
    model.watchedPriceStats = new Map(
      Object.entries(response.watched_price_stats || {}),
    );
    commitChangeSession(cruiseId);
  } catch (error) {
    if (changeRequestKey() !== requestKey) return;
    model.changesError = error.message;
    model.changes = null;
  } finally {
    if (changeRequestKey() === requestKey) {
      model.changesLoading = false;
      renderCatalog();
    }
  }
}

function createHistoryChart(history, { expanded = false } = {}) {
  const points = history.points || [];
  const pricedPoints = points.filter(
    (point) => point.available && point.price !== null && point.price !== undefined,
  );
  if (!pricedPoints.length) {
    return node("p", "history-empty", "No available price has been recorded yet.");
  }

  const width = expanded ? 1100 : 760;
  const height = expanded ? 500 : 210;
  const padding = expanded
    ? { top: 34, right: 36, bottom: 68, left: 122 }
    : { top: 18, right: 18, bottom: 38, left: 64 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const prices = pricedPoints.map((point) => Number(point.price));
  const rawMin = Math.min(...prices);
  const rawMax = Math.max(...prices);
  const pricePadding = rawMin === rawMax
    ? Math.max(1, rawMin * 0.05)
    : Math.max(0.5, (rawMax - rawMin) * 0.12);
  const minimum = Math.max(0, rawMin - pricePadding);
  const maximum = rawMax + pricePadding;

  const times = points.map((point, index) => {
    const parsed = new Date(point.observed_at).valueOf();
    return Number.isFinite(parsed) ? parsed : index;
  });
  const firstTime = Math.min(...times);
  const lastTime = Math.max(...times);
  const timeSpan = Math.max(1, lastTime - firstTime);
  const xFor = (index) => points.length === 1
    ? padding.left + plotWidth / 2
    : padding.left + ((times[index] - firstTime) / timeSpan) * plotWidth;
  const yFor = (price) => padding.top
    + ((maximum - Number(price)) / (maximum - minimum)) * plotHeight;

  const svg = svgNode("svg", {
    class: expanded ? "history-chart expanded-history-chart" : "history-chart",
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `${history.item.name} price history`,
  });
  svg.append(svgNode("title", {}, `${history.item.name} price history`));

  const gridRatios = expanded ? [0, 0.25, 0.5, 0.75, 1] : [0, 0.5, 1];
  for (const ratio of gridRatios) {
    const y = padding.top + ratio * plotHeight;
    const value = maximum - ratio * (maximum - minimum);
    svg.append(svgNode("line", {
      class: "history-grid-line",
      x1: padding.left,
      x2: width - padding.right,
      y1: y,
      y2: y,
    }));
    svg.append(svgNode("text", {
      class: "history-axis-label",
      x: padding.left - 9,
      y: y + 4,
      "text-anchor": "end",
    }, formatCurrency(value, history.sailing.currency)));
  }

  let pathData = "";
  let previousWasAvailable = false;
  points.forEach((point, index) => {
    if (!point.available || point.price === null || point.price === undefined) {
      previousWasAvailable = false;
      svg.append(svgNode("circle", {
        class: "history-unavailable-point",
        cx: xFor(index),
        cy: padding.top + plotHeight,
        r: expanded ? 6 : 4,
      }));
      return;
    }
    const x = xFor(index);
    const y = yFor(point.price);
    pathData += previousWasAvailable ? ` H ${x} V ${y}` : ` M ${x} ${y}`;
    previousWasAvailable = true;
  });
  if (pricedPoints.length === 1 && points.length === 1) {
    const y = yFor(pricedPoints[0].price);
    pathData = `M ${padding.left} ${y} H ${width - padding.right}`;
  }
  svg.append(svgNode("path", { class: "history-line", d: pathData.trim() }));

  points.forEach((point, index) => {
    if (!point.available || point.price === null || point.price === undefined) return;
    const marker = svgNode("circle", {
      class: "history-price-point",
      cx: xFor(index),
      cy: yFor(point.price),
      r: expanded ? 7 : 4.5,
    });
    marker.append(svgNode(
      "title",
      {},
      `${formatCurrency(point.price, history.sailing.currency)} on ${shortDate(point.observed_at)}`,
    ));
    svg.append(marker);
  });

  svg.append(svgNode("text", {
    class: "history-axis-label",
    x: padding.left,
    y: height - 10,
    "text-anchor": "start",
  }, shortDate(points[0].observed_at)));
  if (points.length > 1) {
    svg.append(svgNode("text", {
      class: "history-axis-label",
      x: width - padding.right,
      y: height - 10,
      "text-anchor": "end",
    }, shortDate(points.at(-1).observed_at)));
  }
  return svg;
}

async function loadHistory(itemId, { force = false } = {}) {
  if (model.busyHistory.has(itemId)) return;
  if (!force && model.historyCache.has(itemId)) return;
  model.busyHistory.add(itemId);
  renderCatalog();
  try {
    const cruiseId = activeCruiseId();
    if (!cruiseId) throw new Error("Choose a cruise first.");
    const history = await request(
      `cruises/${encodeURIComponent(cruiseId)}/items/${encodeURIComponent(itemId)}/history`,
    );
    model.historyCache.set(itemId, history);
  } catch (error) {
    model.historyCache.set(itemId, { error: error.message });
  } finally {
    model.busyHistory.delete(itemId);
    renderCatalog();
  }
}

function toggleHistory(itemId) {
  if (model.openHistory.has(itemId)) {
    model.openHistory.delete(itemId);
  } else {
    model.openHistory.add(itemId);
  }
  renderCatalog();
}

function closeExpandedHistory() {
  if (elements.historyChartDialog.open) {
    elements.historyChartDialog.close();
  } else {
    elements.historyChartDialog.removeAttribute("open");
  }
  elements.historyChartDialogBody.replaceChildren();
}

function openExpandedHistory(history) {
  elements.historyChartDialogTitle.textContent = history.item.name;
  elements.historyChartDialogBody.replaceChildren(
    createHistoryChart(history, { expanded: true }),
  );
  if (typeof elements.historyChartDialog.showModal === "function") {
    elements.historyChartDialog.showModal();
  } else {
    elements.historyChartDialog.setAttribute("open", "");
  }
  elements.historyChartDialogClose.focus();
}

function createHistoryPanel(item) {
  const panel = node("section", "history-panel");
  panel.id = historyPanelId(item.id);
  panel.setAttribute("role", "region");
  panel.setAttribute("aria-label", `Price history for ${item.name}`);

  if (model.busyHistory.has(item.id)) {
    panel.append(node("p", "history-loading", "Loading price history…"));
    return panel;
  }
  const history = model.historyCache.get(item.id);
  if (!history) {
    panel.append(node("p", "history-loading", "Loading price history…"));
    window.queueMicrotask(() => loadHistory(item.id));
    return panel;
  }
  if (history.error) {
    panel.append(node("p", "history-error", history.error));
    const retry = node("button", "row-action", "Retry history");
    retry.type = "button";
    retry.addEventListener("click", () => loadHistory(item.id, { force: true }));
    panel.append(retry);
    return panel;
  }

  const summary = node("div", "history-summary");
  const stats = [
    ["Current", history.summary.current_price],
    ["Lowest", history.summary.lowest_price],
    ["Highest", history.summary.highest_price],
  ];
  for (const [label, value] of stats) {
    const stat = node("div", "history-stat");
    stat.append(
      node("span", "history-stat-label", label),
      node("strong", "history-stat-value", formatCurrency(value, history.sailing.currency)),
    );
    summary.append(stat);
  }
  const eventStat = node("div", "history-stat");
  eventStat.append(
    node("span", "history-stat-label", "Saved events"),
    node("strong", "history-stat-value", String(history.summary.events)),
  );
  summary.append(eventStat);
  panel.append(summary);
  const chart = createHistoryChart(history);
  if (chart.nodeName.toLowerCase() === "svg") {
    const chartTrigger = node("button", "history-chart-trigger");
    chartTrigger.type = "button";
    chartTrigger.setAttribute(
      "aria-label",
      `Expand price history chart for ${history.item.name}`,
    );
    chartTrigger.title = "Open a larger price history chart";
    chartTrigger.append(chart);
    chartTrigger.addEventListener("click", () => openExpandedHistory(history));
    panel.append(chartTrigger);
  } else {
    panel.append(chart);
  }

  const points = history.points || [];
  const note = points.length <= 1
    ? "Baseline saved. Future price or availability changes will appear here."
    : "Only price and availability changes are saved; unchanged daily checks do not add duplicate points.";
  panel.append(node("p", "history-note", note));
  return panel;
}

function createDescriptionDisclosure(item) {
  const details = node("details", "item-description");
  const summary = node("summary", "item-description-toggle");
  const copy = node("p", "item-description-copy", item.description);

  details.open = model.openDescriptions.has(item.id);
  summary.textContent = details.open ? "Hide description" : "Show description";
  details.addEventListener("toggle", () => {
    if (details.open) {
      model.openDescriptions.add(item.id);
    } else {
      model.openDescriptions.delete(item.id);
    }
    summary.textContent = details.open ? "Hide description" : "Show description";
  });
  details.append(summary, copy);
  return details;
}

function lastPriceChangeCopy(change, currency) {
  const delta = Number(change?.price_delta);
  const price = Number(change?.price);
  if (!Number.isFinite(delta) || !Number.isFinite(price) || delta === 0) return null;
  const direction = delta < 0 ? "down" : "up";
  return (
    `Last price change ${shortDate(change.observed_at)}: ${direction} `
    + `${formatCurrency(Math.abs(delta), currency)} to ${formatCurrency(price, currency)}`
  );
}

function createRow(item, data) {
  const isPinned = data.pinned.has(item.id);
  const watch = data.watching[item.id];
  const isBusy = model.busyItems.has(item.id);
  const row = node("article", "price-row");

  const identity = node("div", "item-identity");
  const titleLine = node("div", "item-title-line");
  titleLine.append(node("h2", "item-name", item.name));
  if (watch) titleLine.append(createBadge("Watching", "watching"));
  if (watch) {
    const priceBadge = createPriceBadge(item);
    if (priceBadge) titleLine.append(priceBadge);
  }
  if (isPinned) titleLine.append(createBadge("Pinned", "pinned"));
  identity.append(titleLine);

  const location = [item.category, item.subcategory].filter(Boolean).join(" · ");
  identity.append(node("p", "item-meta", location));
  const codeLine = node("p", "item-code");
  codeLine.append("Watch code ");
  codeLine.append(node("code", "", `${item.prefix} / ${item.product}`));
  identity.append(codeLine);
  if (watch && model.changes && !model.changesError) {
    const latestChange = model.watchedLatestChanges.get(item.id);
    const changeCopy = lastPriceChangeCopy(latestChange, item.currency);
    identity.append(node(
      "p",
      `last-price-change${Number(latestChange?.price_delta) < 0 ? " decrease" : ""}`,
      changeCopy || "No price changes recorded yet.",
    ));
  }
  if (item.description) identity.append(createDescriptionDisclosure(item));
  row.append(identity);

  const priceBlock = node("div", "price-block");
  if (item.price_available) {
    priceBlock.append(node("span", "price", formatPrice(item)));
    if (item.unit) priceBlock.append(node("span", "price-unit", `per ${item.unit}`));
  } else {
    priceBlock.append(node("span", "unavailable", "Price unavailable"));
  }
  row.append(priceBlock);

  const controls = node("div", "item-controls");
  if (watch) {
    const editor = node("div", "target-editor");
    const label = node("label", "", "Alert below");
    const target = node("input", "target-input");
    target.type = "number";
    target.min = "0";
    target.step = "0.01";
    target.value = Number(watch.target_price).toFixed(2);
    target.setAttribute("aria-label", `Alert target for ${item.name}`);
    target.disabled = isBusy;
    const save = node("button", "row-action", "Save target");
    save.type = "button";
    save.disabled = isBusy;
    save.addEventListener("click", () => {
      const next = Number(target.value);
      if (!Number.isFinite(next) || next < 0) {
        showToast("Enter a valid target price.");
        return;
      }
      mutateItem(item.id, "target", { target_price: next }, "Target price updated.");
    });
    editor.append(label, target, save);
    controls.append(editor);

    const unwatch = node("button", "row-action unwatch", "Unwatch");
    unwatch.type = "button";
    unwatch.disabled = isBusy;
    unwatch.addEventListener("click", () =>
      mutateItem(item.id, "watch", { watching: false }, "Watch removed."));
    controls.append(unwatch);
  } else {
    const watchButton = node("button", "row-action watch", "＋ Add watch");
    watchButton.type = "button";
    watchButton.disabled = isBusy || !item.price_available;
    watchButton.title = item.price_available
      ? "Alert only if a later price drops below today's price"
      : "A current price is required before this item can be watched";
    watchButton.addEventListener("click", () =>
      mutateItem(
        item.id,
        "watch",
        { watching: true },
        `Watching ${item.name} below ${formatPrice(item)}.`,
      ));
    controls.append(watchButton);
  }

  const isHistoryOpen = model.openHistory.has(item.id);
  const historyButton = node(
    "button",
    "row-action history-action",
    isHistoryOpen ? "▾ History" : "▸ History",
  );
  historyButton.type = "button";
  historyButton.disabled = isBusy;
  historyButton.setAttribute("aria-expanded", String(isHistoryOpen));
  historyButton.setAttribute("aria-controls", historyPanelId(item.id));
  historyButton.title = "Show saved price and availability changes";
  historyButton.addEventListener("click", () => toggleHistory(item.id));
  controls.append(historyButton);

  const pinButton = node(
    "button",
    "row-action pin-action",
    isPinned ? "★ Unpin" : "☆ Pin",
  );
  pinButton.type = "button";
  pinButton.disabled = isBusy;
  pinButton.title = isPinned
    ? "Remove this item from your pinned shortlist"
    : "Add this item to your pinned shortlist";
  pinButton.addEventListener("click", () =>
    mutateItem(
      item.id,
      "pin",
      { pinned: !isPinned },
      isPinned ? "Item unpinned." : "Item pinned. It still appears in All.",
    ));
  controls.append(pinButton);
  row.append(controls);
  if (isHistoryOpen) row.append(createHistoryPanel(item));
  return row;
}

function changeMovement(change) {
  const currency = change.item.currency;
  const priorPrice = change.previous_price;
  const price = change.price;
  const delta = change.price_delta;
  if (change.previous_available && !change.available) {
    return {
      title: "Price became unavailable",
      detail: priorPrice === null
        ? "No current public price"
        : `Previously ${formatCurrency(priorPrice, currency)}`,
      className: "unavailable-change",
    };
  }
  if (!change.previous_available && change.available) {
    return {
      title: "Price is available again",
      detail: `Now ${formatCurrency(price, currency)}`,
      className: "available-change",
    };
  }
  if (delta !== null && Number(delta) !== 0) {
    const movedDown = Number(delta) < 0;
    return {
      title: `${movedDown ? "Down" : "Up"} ${formatCurrency(Math.abs(delta), currency)}`,
      detail: `${formatCurrency(priorPrice, currency)} → ${formatCurrency(price, currency)}`,
      className: movedDown ? "decrease" : "increase",
    };
  }
  return {
    title: "Availability changed",
    detail: price === null ? "No current public price" : formatCurrency(price, currency),
    className: "availability-change",
  };
}

async function openHistoryFromChange(itemId) {
  await loadHistory(itemId);
  const history = model.historyCache.get(itemId);
  if (!history) return;
  if (history.error) {
    showToast(history.error);
    return;
  }
  openExpandedHistory(history);
}

function createChangeRow(change, data) {
  const item = change.item;
  const catalogItem = data.items.find((candidate) => candidate.id === item.id) || item;
  const watch = data.watching[item.id];
  const isPinned = data.pinned.has(item.id);
  const isBusy = model.busyItems.has(item.id);
  const isHistoryBusy = model.busyHistory.has(item.id);
  const movement = changeMovement(change);
  const row = node("article", "change-row");
  const identity = node("div", "change-identity");
  const titleLine = node("div", "item-title-line");
  titleLine.append(node("h2", "item-name", item.name));
  if (watch) titleLine.append(createBadge("Watching", "watching"));
  if (isPinned) titleLine.append(createBadge("Pinned", "pinned"));
  identity.append(titleLine);
  identity.append(node(
    "p",
    "item-meta",
    [item.category, item.subcategory].filter(Boolean).join(" · "),
  ));

  const movementBlock = node("div", `change-movement ${movement.className}`);
  movementBlock.append(
    node("strong", "change-title", movement.title),
    node("span", "change-detail", movement.detail),
  );

  const when = node("div", "change-when");
  when.append(
    node("time", "change-time", `Changed ${formatTimestamp(change.observed_at)}`),
  );

  const actions = node("div", "change-actions");
  const watchButton = node(
    "button",
    `row-action ${watch ? "unwatch" : "watch"}`,
    watch ? "Unwatch" : "＋ Watch",
  );
  watchButton.type = "button";
  watchButton.disabled = isBusy || (!watch && !catalogItem.price_available);
  watchButton.title = watch
    ? "Stop price-drop alerts for this item"
    : catalogItem.price_available
      ? "Alert only if a later price drops below today's price"
      : "A current price is required before this item can be watched";
  watchButton.addEventListener("click", () => {
    if (watch) {
      mutateItem(item.id, "watch", { watching: false }, "Watch removed.");
      return;
    }
    mutateItem(
      item.id,
      "watch",
      { watching: true },
      `Watching ${item.name} below ${formatPrice(catalogItem)}.`,
    );
  });

  const pinButton = node(
    "button",
    "row-action pin-action",
    isPinned ? "★ Unpin" : "☆ Pin",
  );
  pinButton.type = "button";
  pinButton.disabled = isBusy;
  pinButton.title = isPinned
    ? "Remove this item from your pinned shortlist"
    : "Add this item to your pinned shortlist";
  pinButton.addEventListener("click", () =>
    mutateItem(
      item.id,
      "pin",
      { pinned: !isPinned },
      isPinned ? "Item unpinned." : "Item pinned. It still appears in All.",
    ));

  const history = node(
    "button",
    "row-action history-action",
    isHistoryBusy ? "Loading…" : "View history",
  );
  history.type = "button";
  history.disabled = isBusy || isHistoryBusy;
  history.addEventListener("click", () => openHistoryFromChange(item.id));
  actions.append(watchButton, pinButton, history);
  when.append(actions);
  row.append(identity, movementBlock, when);
  return row;
}

function renderChanges() {
  const data = currentData();
  const response = model.changes;
  const changes = response?.changes || [];
  const showingAllChanges = model.changePeriod === "all";
  elements.changesHeading.textContent = showingAllChanges
    ? "All changed items"
    : "Changes since last visit";
  if (showingAllChanges) {
    elements.changesPeriod.textContent = (
      "Each product with a recorded price or availability change appears once, newest first."
    );
  } else if (model.changesVisitedAt && model.changesSince) {
    const visitWasBounded = (
      new Date(model.changesSince).valueOf()
      < new Date(model.changesVisitedAt).valueOf()
    );
    elements.changesPeriod.textContent = visitWasBounded
      ? `Showing changes since ${formatTimestamp(model.changesSince)}. A minimum one-day lookback keeps same-day visits useful.`
      : `This device last opened the cruise on ${formatTimestamp(model.changesVisitedAt)}.`;
  } else {
    elements.changesPeriod.textContent = (
      "First visit on this device, so the latest recorded changes are shown."
    );
  }
  elements.changePeriodButtons.forEach((button) => {
    const active = button.dataset.changePeriod === model.changePeriod;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  elements.changeScopeButtons.forEach((button) => {
    const active = button.dataset.changeScope === model.changeScope;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });

  if (model.changesLoading && !response) {
    elements.catalog.replaceChildren(node("p", "changes-message", "Loading price changes…"));
  } else if (model.changesError) {
    const message = node("div", "changes-message error");
    message.append(node("p", "", model.changesError));
    const retry = node("button", "row-action", "Retry changes");
    retry.type = "button";
    retry.addEventListener("click", () => loadChanges({ force: true }));
    message.append(retry);
    elements.catalog.replaceChildren(message);
  } else {
    elements.catalog.replaceChildren(
      ...changes.map((change) => createChangeRow(change, data)),
    );
  }

  const empty = !model.changesLoading && !model.changesError && changes.length === 0;
  elements.emptyState.classList.toggle("hidden", !empty);
  if (empty) {
    const hasWatches = Object.keys(data.watching).length > 0;
    if (model.changeScope === "watched" && !hasWatches) {
      elements.emptyTitle.textContent = "Nothing watched yet";
      elements.emptyCopy.textContent = "Choose All items here, or add a watch from the catalog.";
    } else if (model.changeScope === "watched") {
      elements.emptyTitle.textContent = "No watched changes here";
      elements.emptyCopy.textContent = "Choose All items to include changes elsewhere in the catalog.";
    } else {
      elements.emptyTitle.textContent = showingAllChanges ? "No changed items yet" : "All caught up";
      elements.emptyCopy.textContent = showingAllChanges
        ? "No changes have been recorded beyond the initial price baseline."
        : "No recorded price or availability changes in this visit window.";
    }
  }

  const shown = changes.length;
  const scopeLabel = model.changeScope === "watched" ? "watched " : "";
  const resultLabel = showingAllChanges ? "changed item" : "change";
  const more = response?.truncated ? ` First ${response.limit} shown.` : "";
  elements.resultsSummary.textContent = (
    `${shown} ${scopeLabel}${resultLabel}${shown === 1 ? "" : "s"} shown.${more}`
  );
}

function resetOnboarding() {
  model.onboarding = {
    open: true,
    step: "line",
    cruiseLine: null,
    ships: [],
    shipSearch: "",
    selectedShip: null,
    sailings: [],
    selectedSailing: null,
    currency: "USD",
    notificationsEnabled: true,
    busy: false,
    error: "",
  };
}

function setupNavigation(backAction, primaryButton = null) {
  const actions = node("div", "setup-actions");
  const back = node("button", "button quiet", "← Back");
  back.type = "button";
  back.disabled = model.onboarding.busy;
  back.addEventListener("click", backAction);
  actions.append(back);
  if (primaryButton) actions.append(primaryButton);
  return actions;
}

async function loadShips(cruiseLine) {
  const onboarding = model.onboarding;
  onboarding.busy = true;
  onboarding.error = "";
  renderOnboarding();
  try {
    const response = await request(
      `discovery/ships?line=${encodeURIComponent(cruiseLine)}`,
    );
    if (model.onboarding.cruiseLine !== cruiseLine) return;
    model.onboarding.ships = response.ships || [];
  } catch (error) {
    if (model.onboarding.cruiseLine === cruiseLine) {
      model.onboarding.error = error.message;
    }
  } finally {
    if (model.onboarding.cruiseLine === cruiseLine) {
      model.onboarding.busy = false;
      renderOnboarding();
    }
  }
}

async function loadSailings(ship) {
  const onboarding = model.onboarding;
  onboarding.busy = true;
  onboarding.error = "";
  renderOnboarding();
  try {
    const response = await request(
      `discovery/sailings?ship=${encodeURIComponent(ship.name)}`,
    );
    if (model.onboarding.selectedShip?.name !== ship.name) return;
    model.onboarding.sailings = response.sailings || [];
  } catch (error) {
    if (model.onboarding.selectedShip?.name === ship.name) {
      model.onboarding.error = error.message;
    }
  } finally {
    if (model.onboarding.selectedShip?.name === ship.name) {
      model.onboarding.busy = false;
      renderOnboarding();
    }
  }
}

function renderLineStep() {
  const fragment = document.createDocumentFragment();
  fragment.append(node(
    "p",
    "setup-question",
    "Which fleet is your sailing part of? No cruise-line login is needed.",
  ));
  if (model.state?.setup_required) {
    const sidebarTip = node("aside", "setup-sidebar-tip");
    sidebarTip.setAttribute("aria-label", "Home Assistant sidebar tip");
    sidebarTip.append(
      node("strong", "", "Keep Royal Prices handy"),
      node(
        "span",
        "",
        "Home Assistant leaves Show in sidebar off after installation. "
          + "From Settings > Apps > Royal Price Dashboard, enable it for a "
          + "Royal Prices shortcut. You can always return through Open Web UI.",
      ),
    );
    fragment.append(sidebarTip);
  }
  const choices = node("div", "brand-choices");
  const brands = [
    {
      id: "royal-caribbean",
      name: "Royal Caribbean",
      mark: "⚓",
      description: "Ships ending in “of the Seas”",
    },
    {
      id: "celebrity",
      name: "Celebrity Cruises",
      mark: "✦",
      description: "Celebrity ships and public Cruise Planner prices",
    },
  ];
  for (const brand of brands) {
    const button = node("button", "choice-card brand-choice");
    button.type = "button";
    button.append(
      node("span", "choice-mark", brand.mark),
      node("strong", "choice-title", brand.name),
      node("span", "choice-copy", brand.description),
    );
    button.addEventListener("click", () => {
      model.onboarding.cruiseLine = brand.id;
      model.onboarding.selectedShip = null;
      model.onboarding.ships = [];
      model.onboarding.step = "ship";
      loadShips(brand.id);
    });
    choices.append(button);
  }
  fragment.append(choices);
  return fragment;
}

function renderShipStep() {
  const fragment = document.createDocumentFragment();
  const lineName = model.onboarding.cruiseLine === "celebrity"
    ? "Celebrity Cruises"
    : "Royal Caribbean";
  fragment.append(node("p", "setup-question", `Choose a ${lineName} ship.`));

  if (model.onboarding.busy) {
    fragment.append(node("div", "setup-loading", "Finding ships from public discovery data…"));
  } else if (model.onboarding.ships.length) {
    const searchField = node("label", "field setup-search");
    searchField.append(node("span", "", "Search ships"));
    const search = node("input");
    search.type = "search";
    search.placeholder = "Start typing a ship name…";
    search.value = model.onboarding.shipSearch;
    searchField.append(search);
    fragment.append(searchField);

    const list = node("div", "choice-list ship-list");
    const noMatches = node("p", "setup-empty hidden", "No ships match that search.");
    for (const ship of model.onboarding.ships) {
      const button = node("button", "choice-card list-choice");
      button.type = "button";
      button.dataset.searchValue = ship.name.toLocaleLowerCase();
      button.append(
        node("strong", "choice-title", ship.name),
        node("span", "choice-copy", "View actual future sailings"),
      );
      button.addEventListener("click", () => {
        model.onboarding.selectedShip = ship;
        model.onboarding.selectedSailing = null;
        model.onboarding.sailings = [];
        model.onboarding.step = "sailing";
        loadSailings(ship);
      });
      list.append(button);
    }
    search.addEventListener("input", () => {
      model.onboarding.shipSearch = search.value;
      const needle = search.value.trim().toLocaleLowerCase();
      list.querySelectorAll(".list-choice").forEach((button) => {
        button.classList.toggle(
          "hidden",
          Boolean(needle) && !button.dataset.searchValue.includes(needle),
        );
      });
      noMatches.classList.toggle(
        "hidden",
        [...list.querySelectorAll(".list-choice")].some(
          (button) => !button.classList.contains("hidden"),
        ),
      );
    });
    if (model.onboarding.shipSearch) {
      search.dispatchEvent(new Event("input"));
    }
    fragment.append(list, noMatches);
  } else {
    fragment.append(node("p", "setup-empty", "No ships were returned."));
    const retry = node("button", "button primary", "Try ship discovery again");
    retry.type = "button";
    retry.addEventListener("click", () => loadShips(model.onboarding.cruiseLine));
    fragment.append(retry);
  }

  fragment.append(setupNavigation(() => {
    model.onboarding.step = "line";
    model.onboarding.error = "";
    renderOnboarding();
  }));
  return fragment;
}

function renderSailingStep() {
  const fragment = document.createDocumentFragment();
  const ship = model.onboarding.selectedShip;
  fragment.append(node(
    "p",
    "setup-question",
    `Choose an upcoming ${ship?.name || "ship"} sailing.`,
  ));

  if (model.onboarding.busy) {
    fragment.append(node("div", "setup-loading", "Checking the public sailing schedule…"));
  } else if (model.onboarding.sailings.length) {
    const list = node("div", "choice-list sailing-list");
    for (const sailing of model.onboarding.sailings) {
      const button = node("button", "choice-card sailing-choice");
      button.type = "button";
      const duration = sailing.duration
        ? `${sailing.duration} night${sailing.duration === 1 ? "" : "s"}`
        : "Duration unavailable";
      button.append(
        node("strong", "choice-title", formatSailingDate(sailing.sail_date)),
        node("span", "sailing-duration", duration),
        node("span", "choice-copy", sailing.description),
      );
      button.addEventListener("click", () => {
        model.onboarding.selectedSailing = sailing;
        model.onboarding.step = "settings";
        model.onboarding.error = "";
        renderOnboarding();
      });
      list.append(button);
    }
    fragment.append(list);
  } else {
    fragment.append(node("p", "setup-empty", "No future sailings were returned."));
    const retry = node("button", "button primary", "Try sailing discovery again");
    retry.type = "button";
    retry.addEventListener("click", () => loadSailings(ship));
    fragment.append(retry);
  }

  fragment.append(setupNavigation(() => {
    model.onboarding.step = "ship";
    model.onboarding.error = "";
    renderOnboarding();
  }));
  return fragment;
}

async function createCruise() {
  const onboarding = model.onboarding;
  const selectedShip = onboarding.selectedShip;
  const selectedSailing = onboarding.selectedSailing;
  const selectedCruiseLine = selectedShip?.cruise_line || onboarding.cruiseLine;
  const currency = onboarding.currency.trim().toUpperCase();
  if (!/^[A-Z]{3}$/.test(currency)) {
    onboarding.error = "Enter a three-letter currency code such as USD or CAD.";
    renderOnboarding();
    return;
  }
  if (!selectedShip?.id || !selectedShip?.name) {
    onboarding.error = "Choose a ship from the discovery list again.";
    onboarding.step = "ship";
    renderOnboarding();
    return;
  }
  if (!["royal-caribbean", "celebrity"].includes(selectedCruiseLine)) {
    onboarding.error = "Choose a ship from the discovery list again.";
    onboarding.step = "ship";
    renderOnboarding();
    return;
  }
  if (!selectedSailing?.sail_date) {
    onboarding.error = "Choose a sailing from the discovery list again.";
    onboarding.step = "sailing";
    renderOnboarding();
    return;
  }
  onboarding.busy = true;
  onboarding.error = "";
  renderOnboarding();
  try {
    const response = await request("cruises", {
      method: "POST",
      body: JSON.stringify({
        client_version: APP_VERSION,
        cruise_line: selectedCruiseLine,
        ship_id: selectedShip.id,
        ship: selectedShip.name,
        sail_date: selectedSailing.sail_date,
        duration: selectedSailing.duration,
        description: selectedSailing.description,
        currency,
        notifications_enabled: onboarding.notificationsEnabled,
      }),
    });
    acceptState(response.state);
    model.onboarding.open = false;
    model.onboarding.busy = false;
    showToast("Cruise added. Its first catalog can take a few minutes to build.");
    render();
    await loadChanges();
  } catch (error) {
    onboarding.busy = false;
    onboarding.error = error.message;
    renderOnboarding();
  }
}

function renderSettingsStep() {
  const fragment = document.createDocumentFragment();
  const ship = model.onboarding.selectedShip;
  const sailing = model.onboarding.selectedSailing;

  const summary = node("div", "setup-summary");
  summary.append(
    node("span", "setup-summary-label", "Selected cruise"),
    node("strong", "setup-summary-title", ship?.name || "Ship unavailable"),
    node(
      "span",
      "setup-summary-copy",
      `${formatSailingDate(sailing?.sail_date)} · ${sailing?.duration ? `${sailing.duration} nights` : "Duration unavailable"}`,
    ),
    node("span", "setup-summary-copy", sailing?.description || ""),
  );
  fragment.append(summary);

  const storage = model.state?.status?.storage || {};
  const storageFacts = [];
  if (storage.app_data_bytes !== null && storage.app_data_bytes !== undefined) {
    storageFacts.push(`${formatBytes(storage.app_data_bytes)} App data`);
  }
  if (
    storage.filesystem_free_bytes !== null
    && storage.filesystem_free_bytes !== undefined
  ) {
    storageFacts.push(`${formatBytes(storage.filesystem_free_bytes)} free`);
  }
  const storageCopy = storageFacts.length
    ? ` Current storage: ${storageFacts.join(" · ")}.`
    : "";
  fragment.append(node(
    "p",
    "setup-storage-note",
    "Each cruise stores one current catalog and change-only history in the App's "
      + `private data. Removing a cruise deletes its saved data.${storageCopy}`,
  ));
  if (storage.message) {
    fragment.append(node("p", "banner warning", storage.message));
  }

  const settings = node("div", "setup-settings");
  const currencyField = node("label", "field");
  currencyField.append(node("span", "", "Price currency"));
  const currency = node("input");
  currency.type = "text";
  currency.inputMode = "text";
  currency.maxLength = 3;
  currency.pattern = "[A-Za-z]{3}";
  currency.autocomplete = "off";
  currency.value = model.onboarding.currency;
  currency.setAttribute("aria-describedby", "currency-help");
  currency.addEventListener("input", () => {
    currency.value = currency.value.toUpperCase().replace(/[^A-Z]/g, "");
    model.onboarding.currency = currency.value;
  });
  currencyField.append(currency);
  const currencyHelp = node(
    "small",
    "field-help",
    "Three-letter code, for example USD, CAD, GBP, AUD, or EUR.",
  );
  currencyHelp.id = "currency-help";
  currencyField.append(currencyHelp);
  settings.append(currencyField);

  const notificationField = node("label", "check-field");
  const notifications = node("input");
  notifications.type = "checkbox";
  notifications.checked = model.onboarding.notificationsEnabled;
  notifications.addEventListener("change", () => {
    model.onboarding.notificationsEnabled = notifications.checked;
  });
  const notificationCopy = node("span");
  notificationCopy.append(
    node("strong", "", "Allow Home Assistant notifications"),
    node(
      "small",
      "field-help",
      "Still silent until you explicitly Watch a product and it drops below your target.",
    ),
  );
  notificationField.append(notifications, notificationCopy);
  settings.append(notificationField);
  fragment.append(settings);

  const create = node(
    "button",
    "button primary setup-create",
    model.onboarding.busy ? "Adding cruise…" : "Add cruise & build baseline",
  );
  create.type = "button";
  create.disabled = model.onboarding.busy || storage.growth_allowed === false;
  if (storage.growth_allowed === false) {
    create.textContent = "Free storage to add this cruise";
  }
  create.addEventListener("click", createCruise);
  fragment.append(setupNavigation(() => {
    model.onboarding.step = "sailing";
    model.onboarding.error = "";
    renderOnboarding();
  }, create));
  return fragment;
}

function renderOnboarding() {
  const onboarding = model.onboarding;
  const mustSetUp = Boolean(model.state?.setup_required);
  const visible = onboarding.open || mustSetUp;
  elements.setupPanel.classList.toggle("hidden", !visible);
  elements.dashboard.classList.toggle("hidden", visible || mustSetUp);
  if (!visible) return;

  const hasCruises = Boolean(model.state?.cruises?.length);
  elements.setupTitle.textContent = hasCruises ? "Add another cruise" : "Set up your first cruise";
  elements.setupIntro.textContent = hasCruises
    ? "Each cruise keeps its own catalog, pins, watches, and price history."
    : "Choose an actual sailing, then we’ll build its first public-price baseline.";
  elements.setupCancel.classList.toggle("hidden", !hasCruises);
  elements.setupCancel.disabled = onboarding.busy;

  const steps = ["line", "ship", "sailing", "settings"];
  const activeIndex = steps.indexOf(onboarding.step);
  elements.setupProgress.forEach((item) => {
    const index = steps.indexOf(item.dataset.setupProgress);
    item.classList.toggle("active", index === activeIndex);
    item.classList.toggle("complete", index < activeIndex);
  });

  elements.setupError.textContent = onboarding.error || "";
  elements.setupError.classList.toggle("hidden", !onboarding.error);
  const renderStep = {
    line: renderLineStep,
    ship: renderShipStep,
    sailing: renderSailingStep,
    settings: renderSettingsStep,
  }[onboarding.step] || renderLineStep;
  elements.setupBody.replaceChildren(renderStep());
}

function renderCruisePicker() {
  const cruises = model.state?.cruises || [];
  const onboardingVisible = model.onboarding.open || model.state?.setup_required;
  elements.cruisePicker.classList.toggle("hidden", cruises.length === 0 || onboardingVisible);
  elements.cruiseSelect.replaceChildren();
  for (const cruise of cruises) {
    const option = node(
      "option",
      "",
      `${cruise.refreshing ? "Refreshing · " : cruise.completed ? "Completed · " : ""}${cruise.ship} · ${formatSailingDate(cruise.sail_date, { short: true })}`,
    );
    option.value = cruise.id;
    option.selected = cruise.id === activeCruiseId();
    elements.cruiseSelect.append(option);
  }
  elements.cruiseSelect.disabled = (
    model.switchingCruise || model.removingCruise || cruises.length < 2
  );
}

async function switchCruise(cruiseId) {
  if (
    !cruiseId
    || cruiseId === activeCruiseId()
    || model.switchingCruise
    || model.removingCruise
  ) return;
  model.switchingCruise = true;
  renderCruisePicker();
  try {
    const response = await request(`cruises/${encodeURIComponent(cruiseId)}/activate`, {
      method: "POST",
      body: "{}",
    });
    acceptState(response.state);
    showToast(`Now viewing ${model.state.config.ship}.`);
  } catch (error) {
    showToast(error.message);
  } finally {
    model.switchingCruise = false;
    render();
    await loadChanges();
  }
}

async function removeCruise() {
  const cruiseId = activeCruiseId();
  if (!cruiseId || model.removingCruise) return;
  const cruise = (model.state?.cruises || []).find(
    (candidate) => candidate.id === cruiseId,
  );
  if (!cruise) return;
  const sailing = `${cruise.ship} · ${formatSailingDate(cruise.sail_date)}`;
  const confirmed = window.confirm(
    `Remove ${sailing}?\n\nThis permanently deletes this cruise's catalog, pins, watches, and saved price history from the App.`,
  );
  if (!confirmed) return;

  model.removingCruise = true;
  render();
  try {
    const response = await request(`cruises/${encodeURIComponent(cruiseId)}`, {
      method: "DELETE",
    });
    if (response.state?.setup_required) resetOnboarding();
    acceptState(response.state);
    const warning = (response.warnings || []).join(" ");
    showToast(warning || `${cruise.ship} was removed.`);
  } catch (error) {
    showToast(error.message);
  } finally {
    model.removingCruise = false;
    render();
    await loadChanges();
  }
}

function renderCatalog() {
  const showingChanges = model.activeTab === "changes";
  elements.filters.classList.toggle("hidden", showingChanges);
  elements.changesControls.classList.toggle("hidden", !showingChanges);
  if (showingChanges) {
    renderChanges();
    return;
  }

  const data = currentData();
  const items = filteredItems(data);
  elements.catalog.replaceChildren(...items.map((item) => createRow(item, data)));
  elements.emptyState.classList.toggle("hidden", items.length !== 0);
  if (items.length === 0) {
    const status = model.state?.status || {};
    if (data.items.length === 0 && status.refreshing) {
      elements.emptyTitle.textContent = "Building the initial catalog";
      elements.emptyCopy.textContent = "The first load can take a few minutes while public prices are gathered. You can switch cruises or leave this page while it finishes.";
    } else if (data.items.length === 0 && status.completed) {
      elements.emptyTitle.textContent = "This cruise is complete";
      elements.emptyCopy.textContent = `It returned on ${formatSailingDate(status.return_date)}. Keep it for reference or remove it above.`;
    } else if (data.items.length === 0 && status.last_error) {
      elements.emptyTitle.textContent = "The catalog isn’t ready yet";
      elements.emptyCopy.textContent = "Review the error above, then use Refresh prices to try again.";
    } else if (data.items.length === 0) {
      elements.emptyTitle.textContent = "No prices gathered yet";
      elements.emptyCopy.textContent = "Use Refresh prices to build this cruise’s public-price baseline.";
    } else if (model.activeTab === "pinned" && data.pinned.size === 0) {
      elements.emptyTitle.textContent = "Nothing pinned yet";
      elements.emptyCopy.textContent = "Open All and pin the items you want in your shortlist.";
    } else {
      elements.emptyTitle.textContent = "Nothing on this deck";
      elements.emptyCopy.textContent = "Try another search, category, or catalog tab.";
    }
  }
  elements.resultsSummary.textContent = `${countLabel(items.length)} shown`;
}

function render() {
  const data = currentData();
  const status = model.state?.status || {};
  const hasActiveCruise = Boolean(activeCruiseId());
  const onboardingVisible = model.onboarding.open || model.state?.setup_required;
  const completed = hasActiveCruise && Boolean(status.completed);
  const storage = status.storage || {};
  const storageCritical = storage.growth_allowed === false;
  const refreshCooldown = Math.max(0, Number(status.refresh_cooldown_seconds || 0));
  const buildingInitialCatalog = (
    hasActiveCruise && Boolean(status.refreshing) && data.items.length === 0
  );
  renderCruisePicker();
  renderOnboarding();
  renderAlertTips();

  const sailing = data.catalog.sailing || model.state?.config || {};
  const duration = sailing.duration || model.state?.config?.duration;
  elements.sailingSummary.textContent = hasActiveCruise
    ? `${sailing.ship || "Ship unavailable"} · ${formatSailingDate(sailing.sail_date)}${duration ? ` · ${duration} nights` : ""} · ${sailing.currency || "USD"} public prices${completed ? " · Completed" : ""}`
    : "Choose your first cruise to start browsing public prices.";
  elements.completedCruiseNotice.classList.toggle(
    "hidden",
    !completed || Boolean(onboardingVisible),
  );
  if (completed) {
    elements.completedCruiseCopy.textContent = (
      `It returned on ${formatSailingDate(status.return_date)}. Automatic price `
      + "refreshes have stopped. Keep the cruise for reference, or remove its saved data now."
    );
  }
  elements.lastRefresh.textContent = status.refreshing
    ? (buildingInitialCatalog ? "Building catalog…" : "Refreshing…")
    : formatTimestamp(data.catalog.generated_at);
  elements.catalogCount.textContent = countLabel(data.items.length);
  elements.watchCount.textContent = countLabel(Object.keys(data.watching).length);
  elements.pinnedCount.textContent = countLabel(data.pinned.size);
  elements.storageUsage.textContent = storage.app_data_bytes === null
    || storage.app_data_bytes === undefined
    ? "Unavailable"
    : `${formatBytes(storage.app_data_bytes)} used`;
  const storageDetails = [];
  if (storage.history_bytes !== null && storage.history_bytes !== undefined) {
    storageDetails.push(`${formatBytes(storage.history_bytes)} history`);
  }
  if (
    storage.filesystem_free_bytes !== null
    && storage.filesystem_free_bytes !== undefined
  ) {
    storageDetails.push(`${formatBytes(storage.filesystem_free_bytes)} free`);
  }
  elements.storageFree.textContent = storageDetails.join(" · ") || "Details unavailable";
  elements.storageStatus.classList.toggle("warning", storage.level === "warning");
  elements.storageStatus.classList.toggle("critical", storage.level === "critical");
  elements.addCruiseButton.classList.toggle("hidden", Boolean(onboardingVisible));
  elements.removeCruiseButton.classList.toggle(
    "hidden",
    !hasActiveCruise || Boolean(onboardingVisible) || completed,
  );
  elements.refreshButton.classList.toggle(
    "hidden",
    !hasActiveCruise || onboardingVisible || completed,
  );
  elements.exportButton.classList.toggle("hidden", !hasActiveCruise || onboardingVisible);
  elements.refreshButton.disabled = (
    Boolean(status.refreshing)
    || refreshCooldown > 0
    || !hasActiveCruise
    || storageCritical
  );
  elements.addCruiseButton.disabled = model.removingCruise || storageCritical;
  elements.removeCruiseButton.disabled = (
    model.removingCruise || Boolean(status.refreshing)
  );
  elements.removeCruiseButton.textContent = model.removingCruise
    ? "Removing…"
    : "Remove cruise";
  elements.completedRemoveButton.disabled = (
    model.removingCruise || Boolean(status.refreshing)
  );
  elements.completedRemoveButton.textContent = model.removingCruise
    ? "Removing…"
    : "Remove completed cruise";
  elements.refreshButton.textContent = status.refreshing
    ? (buildingInitialCatalog ? "Building…" : "Refreshing…")
    : refreshCooldown > 0
      ? `Refresh in ${formatCooldown(refreshCooldown)}`
      : "Refresh prices";
  elements.exportButton.disabled = !hasActiveCruise || Object.keys(data.watching).length === 0;

  elements.errorBanner.textContent = status.last_error || "";
  elements.errorBanner.classList.toggle("hidden", !status.last_error);
  const warnings = [
    status.last_warning,
    status.history?.last_error ? `Price history: ${status.history.last_error}` : null,
    storage.message,
  ].filter(Boolean);
  elements.warningBanner.textContent = warnings.join(" ");
  elements.warningBanner.classList.toggle("hidden", warnings.length === 0);
  updateCategoryOptions(data.items);
  renderCatalog();
}

async function refreshPrices() {
  const cruiseId = activeCruiseId();
  if (!cruiseId || model.state?.status?.completed) return;
  const cooldown = Number(model.state?.status?.refresh_cooldown_seconds || 0);
  if (cooldown > 0) {
    showToast(`Refresh is available in ${formatCooldown(cooldown)}.`);
    return;
  }
  elements.refreshButton.disabled = true;
  try {
    await request(`cruises/${encodeURIComponent(cruiseId)}/refresh`, {
      method: "POST",
      body: "{}",
    });
    showToast("Price refresh started. This usually takes about a minute.");
    await loadState({ quiet: true });
  } catch (error) {
    showToast(error.message);
  }
}

async function exportWatches() {
  const cruiseId = activeCruiseId();
  if (!cruiseId) return;
  try {
    const response = await fetch(
      `./api/cruises/${encodeURIComponent(cruiseId)}/export`,
      { cache: "no-store" },
    );
    if (!response.ok) throw new Error(`Export failed (${response.status})`);
    const yaml = await response.text();
    try {
      await navigator.clipboard.writeText(yaml);
      showToast("Watch-list YAML copied to the clipboard.");
    } catch (_clipboardError) {
      const blob = new Blob([yaml], { type: "text/yaml" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      const ship = model.state?.config?.ship || "cruise";
      const safeShip = ship.toLocaleLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
      anchor.download = `${safeShip}-${model.state?.config?.sail_date || "watchlist"}-watchlist.yaml`;
      anchor.click();
      URL.revokeObjectURL(url);
      showToast("Watch-list YAML downloaded.");
    }
  } catch (error) {
    showToast(error.message);
  }
}

elements.tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    setActiveTab(tab.dataset.tab);
    renderCatalog();
    if (tab.dataset.tab === "changes") loadChanges();
  });
});

elements.changeScopeButtons.forEach((button) => {
  button.addEventListener("click", () => {
    const scope = button.dataset.changeScope;
    if (!scope || scope === model.changeScope) return;
    model.changeScope = scope;
    model.changes = null;
    model.changesError = "";
    model.changesRequestKey = null;
    renderCatalog();
    loadChanges();
  });
});

elements.changePeriodButtons.forEach((button) => {
  button.addEventListener("click", () => {
    const period = button.dataset.changePeriod;
    if (!period || period === model.changePeriod) return;
    model.changePeriod = period;
    model.changes = null;
    model.changesError = "";
    model.changesRequestKey = null;
    renderCatalog();
    loadChanges();
  });
});

elements.searchInput.addEventListener("input", () => {
  model.search = elements.searchInput.value;
  renderCatalog();
});

elements.categorySelect.addEventListener("change", () => {
  model.category = elements.categorySelect.value;
  renderCatalog();
});

elements.sortSelect.addEventListener("change", () => {
  model.sort = elements.sortSelect.value;
  renderCatalog();
});

elements.refreshButton.addEventListener("click", refreshPrices);
elements.exportButton.addEventListener("click", exportWatches);
elements.removeCruiseButton.addEventListener("click", removeCruise);
elements.completedRemoveButton.addEventListener("click", removeCruise);
elements.historyChartDialogClose.addEventListener("click", closeExpandedHistory);
elements.historyChartDialog.addEventListener("click", (event) => {
  if (event.target === elements.historyChartDialog) closeExpandedHistory();
});
elements.historyChartDialog.addEventListener("close", () => {
  elements.historyChartDialogBody.replaceChildren();
});
elements.dismissAlertTips.addEventListener("click", () => {
  setAlertTipsDismissed(true);
});
elements.showAlertTips.addEventListener("click", () => {
  setAlertTipsDismissed(false);
});
elements.addCruiseButton.addEventListener("click", () => {
  resetOnboarding();
  render();
});
elements.setupCancel.addEventListener("click", () => {
  if (!model.state?.cruises?.length || model.onboarding.busy) return;
  model.onboarding.open = false;
  model.onboarding.error = "";
  render();
});
elements.cruiseSelect.addEventListener("change", () => {
  switchCruise(elements.cruiseSelect.value);
});

model.alertTipsDismissed = readAlertTipsPreference();
renderAlertTips();
loadState();
window.setInterval(() => loadState({ quiet: true }), 30_000);
