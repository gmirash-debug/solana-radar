export function networkFromUrl(url) {
  return new URL(url).searchParams.get("network") === "robinhood" ? "robinhood" : "solana";
}

export function networkUrl(url, network) {
  const next = new URL("index.html", url);
  if (network === "robinhood") next.searchParams.set("network", "robinhood");
  return next.href;
}

if (typeof document !== "undefined") {
  const network = networkFromUrl(location.href);
  document.body.dataset.network = network;
  document.querySelector("#network").value = network;
  document.querySelector("#network").addEventListener("change", event => {
    location.href = networkUrl(location.href, event.target.value);
  });
  try {
    if (network === "robinhood") await import("./robinhood.js?v=20260911-relay-1");
    else await import("./app.js?v=20260910-gmgn-2");
  } catch {
    document.querySelector("#subtitle").textContent = "Interface could not load";
    document.querySelector("#scannerSummary").textContent = "Reload to retry. Published data has not been changed.";
    document.querySelector("#refresh").onclick = () => location.reload();
  }
}
