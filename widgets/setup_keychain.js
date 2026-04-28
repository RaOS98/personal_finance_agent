// One-shot setup helper for the Personal Finance Agent widget.
//
// Stores the API endpoint URL and bearer token in the iOS Keychain so the
// widget script never has to be edited per-environment. Re-run this any
// time the token rotates; existing values are pre-filled to make rotation
// a one-tap edit.

const KEYCHAIN_URL_KEY = "pfa_widget_url";
const KEYCHAIN_TOKEN_KEY = "pfa_widget_token";

function readKeychain(key) {
  return Keychain.contains(key) ? Keychain.get(key) : "";
}

async function promptString(title, message, secure, current) {
  const alert = new Alert();
  alert.title = title;
  alert.message = message;
  if (secure) {
    const field = alert.addSecureTextField("(value hidden)", current || "");
    field.spellCheckingType = false;
  } else {
    const field = alert.addTextField("https://abc123.execute-api.us-east-1.amazonaws.com/widget/summary", current || "");
    field.spellCheckingType = false;
  }
  alert.addAction("Save");
  alert.addCancelAction("Cancel");
  const idx = await alert.presentAlert();
  if (idx !== 0) return null;
  return alert.textFieldValue(0);
}

async function done(message) {
  const a = new Alert();
  a.title = "Personal Finance Widget";
  a.message = message;
  a.addAction("OK");
  await a.presentAlert();
}

async function main() {
  const currentUrl = readKeychain(KEYCHAIN_URL_KEY);
  const url = await promptString(
    "Widget API URL",
    "Paste the WidgetApiUrl shown by `sam deploy` outputs. Should end in /widget/summary.",
    false,
    currentUrl,
  );
  if (url === null) {
    await done("Cancelled. Nothing was changed.");
    Script.complete();
    return;
  }
  if (url.trim()) {
    Keychain.set(KEYCHAIN_URL_KEY, url.trim());
  }

  const token = await promptString(
    "Widget Bearer Token",
    "Paste the value of the SSM SecureString /pfa/widget-bearer-token.",
    true,
    "",
  );
  if (token === null) {
    await done("URL saved. Token unchanged.");
    Script.complete();
    return;
  }
  if (token.trim()) {
    Keychain.set(KEYCHAIN_TOKEN_KEY, token.trim());
  }

  await done(
    "Saved. Add a medium widget to your home screen and pick scriptable.js as the script.",
  );
  Script.complete();
}

await main();
