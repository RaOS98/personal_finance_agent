// Personal Finance Agent — iPhone widget (medium size).
//
// Reads its endpoint URL and bearer token from the iOS Keychain (run the
// companion `setup_keychain.js` script once to populate them), fetches
// /widget/summary, and renders a compact spending overview. On network
// failure, falls back to the last-good response cached in iCloud Drive
// so the widget never goes blank.
//
// Slug-to-name mapping lives in this file because the API stays slug-keyed
// for stability; if a category is added in project_documentation/DATA_MODEL.md
// remember to mirror it here.

const KEYCHAIN_URL_KEY = "pfa_widget_url";
const KEYCHAIN_TOKEN_KEY = "pfa_widget_token";
const CACHE_FILENAME = "pfa_widget_cache.json";

const SLUG_NAMES = {
  food_dining: "Food & Dining",
  groceries: "Groceries",
  transportation: "Transport",
  housing: "Housing",
  utilities: "Utilities",
  health: "Health",
  personal_care: "Personal Care",
  entertainment: "Entertainment",
  shopping: "Shopping",
  education: "Education",
  work: "Work",
  other: "Other",
  uncategorized: "Uncategorized",
};

const ACCENT_WARN = new Color("#ff9500");
const ACCENT_ERROR = new Color("#ff3b30");

function readKeychain(key) {
  return Keychain.contains(key) ? Keychain.get(key) : null;
}

function cacheFile() {
  let fm;
  try {
    fm = FileManager.iCloud();
    fm.documentsDirectory();
  } catch (_) {
    fm = FileManager.local();
  }
  return { fm, path: fm.joinPath(fm.documentsDirectory(), CACHE_FILENAME) };
}

function readCache() {
  const { fm, path } = cacheFile();
  if (!fm.fileExists(path)) return null;
  try {
    return JSON.parse(fm.readString(path));
  } catch (_) {
    return null;
  }
}

function writeCache(summary) {
  try {
    const { fm, path } = cacheFile();
    fm.writeString(path, JSON.stringify(summary));
  } catch (_) {
    // Cache is best-effort; failure to write doesn't affect the widget.
  }
}

async function fetchSummary() {
  const url = readKeychain(KEYCHAIN_URL_KEY);
  const token = readKeychain(KEYCHAIN_TOKEN_KEY);
  if (!url || !token) {
    throw new Error("Run setup_keychain.js to configure the widget");
  }
  const req = new Request(url);
  req.headers = { Authorization: `Bearer ${token}` };
  req.timeoutInterval = 15;
  const summary = await req.loadJSON();
  if (req.response && req.response.statusCode && req.response.statusCode >= 400) {
    throw new Error(`API returned ${req.response.statusCode}`);
  }
  return summary;
}

async function loadSummary() {
  try {
    const summary = await fetchSummary();
    writeCache(summary);
    return { summary, stale: false };
  } catch (e) {
    const cached = readCache();
    if (cached) return { summary: cached, stale: true, error: e.message };
    throw e;
  }
}

function fmtPen(value) {
  const n = Number(value || 0);
  return `S/. ${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtUsdRounded(value) {
  const n = Number(value || 0);
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
}

function topCategories(byCategory, n) {
  return Object.entries(byCategory || {})
    .filter(([, amt]) => Number(amt) > 0)
    .sort(([, a], [, b]) => Number(b) - Number(a))
    .slice(0, n);
}

function monthLabel(year, month) {
  const d = new Date(year, month - 1, 1);
  return d.toLocaleString("default", { month: "long", year: "numeric" }).toUpperCase();
}

function nowHHMM() {
  return new Date().toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function buildWidget(summary, stale) {
  const widget = new ListWidget();
  widget.setPadding(14, 16, 14, 16);

  const header = widget.addStack();
  header.layoutHorizontally();
  header.centerAlignContent();
  const title = header.addText(monthLabel(summary.period.year, summary.period.month));
  title.font = Font.semiboldSystemFont(10);
  title.textOpacity = 0.55;
  header.addSpacer();
  if (Number(summary.totals.month_usd) > 0) {
    const usd = header.addText(`+ ${fmtUsdRounded(summary.totals.month_usd)}`);
    usd.font = Font.semiboldSystemFont(10);
    usd.textOpacity = 0.55;
  }

  widget.addSpacer(2);

  const total = widget.addText(fmtPen(summary.totals.month_pen));
  total.font = Font.boldSystemFont(28);
  total.minimumScaleFactor = 0.6;
  total.lineLimit = 1;

  if (Number(summary.totals.today_pen) > 0) {
    const today = widget.addText(`Today ${fmtPen(summary.totals.today_pen)}`);
    today.font = Font.systemFont(11);
    today.textOpacity = 0.55;
  }

  widget.addSpacer(6);

  const top = topCategories(summary.by_category_pen, 3);
  if (top.length === 0) {
    const empty = widget.addText("No spending yet this month");
    empty.font = Font.systemFont(11);
    empty.textOpacity = 0.5;
  } else {
    for (const [slug, amount] of top) {
      const row = widget.addStack();
      row.layoutHorizontally();
      const label = row.addText(SLUG_NAMES[slug] || slug);
      label.font = Font.systemFont(12);
      row.addSpacer();
      const amt = row.addText(fmtPen(amount));
      amt.font = Font.systemFont(12);
      amt.textOpacity = 0.7;
    }
  }

  widget.addSpacer();

  const footer = widget.addStack();
  footer.layoutHorizontally();
  footer.centerAlignContent();
  const updated = footer.addText(stale ? `Cached ${nowHHMM()}` : `Updated ${nowHHMM()}`);
  updated.font = Font.systemFont(9);
  updated.textOpacity = stale ? 0.7 : 0.4;
  if (stale) updated.textColor = ACCENT_WARN;
  footer.addSpacer();
  if (Number(summary.unreconciled_count) > 0) {
    const badge = footer.addText(`${summary.unreconciled_count} unreconciled`);
    badge.font = Font.semiboldSystemFont(9);
    badge.textColor = ACCENT_WARN;
  }

  return widget;
}

function buildErrorWidget(message) {
  const widget = new ListWidget();
  widget.setPadding(14, 16, 14, 16);
  const title = widget.addText("Widget error");
  title.font = Font.semiboldSystemFont(13);
  title.textColor = ACCENT_ERROR;
  widget.addSpacer(4);
  const msg = widget.addText(String(message || "Unknown error"));
  msg.font = Font.systemFont(11);
  msg.textOpacity = 0.7;
  widget.addSpacer();
  const hint = widget.addText("Run setup_keychain.js or check connection.");
  hint.font = Font.systemFont(9);
  hint.textOpacity = 0.5;
  return widget;
}

async function main() {
  let widget;
  try {
    const { summary, stale } = await loadSummary();
    widget = buildWidget(summary, stale);
  } catch (e) {
    widget = buildErrorWidget(e && e.message ? e.message : e);
  }

  if (config.runsInWidget) {
    Script.setWidget(widget);
  } else {
    await widget.presentMedium();
  }
  Script.complete();
}

await main();
