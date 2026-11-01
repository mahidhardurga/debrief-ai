// Desktop edition hooks for the shared UI (shared/ui/app.js). Everything not set here uses the desktop defaults.
const P = window.PLATFORM = {
  name: "desktop",
  speakers: true,                                                                                    // the start screen offers "tell the speakers apart"
  todayExtra: () => '<br><a href="/api/day_summary" download>Download today\'s summary (CSV)</a>',
};
P.beforeBoot = async () => {
  await new Promise((res, rej) => { const s = document.createElement("script"); s.src = "aimodel.js"; s.onload = res; s.onerror = () => rej(new Error("aimodel.js")); document.head.appendChild(s); });
  installAiModel(P);                                                                                 // choose Claude or GitHub Copilot (shared with the server edition)
  await P.refreshAi();
  return true;
};
P.topExtra = () => (P.aiChip ? P.aiChip() : "");
P.topSig = () => (P.aiSig ? P.aiSig() : "");
