(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let S = null;
  let first = true;
  let canvasSkipped = false, lsSkipped = false, canvasReplacing = false;
  let finishing = false;
  let pollTimer = null, autoChecked = false;
  const rows = new Map();
  let rowSeq = 0;

  const NET = "Could not reach the planner. Check that it is still running, then try again.";

  async function api(path, body) {
    const opt = body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const r = await fetch(path, opt);
    let data = null;
    try { data = await r.json(); } catch (_) { }
    if (!data) throw new Error("bad response");
    return data;
  }

  function busy(btn, on) {
    btn.disabled = on;
    btn.classList.toggle("busy", on);
    if (on) btn.setAttribute("aria-busy", "true"); else btn.removeAttribute("aria-busy");
  }

  function setErr(el, text) { el.textContent = text || ""; }

  async function load() {
    try {
      S = await api("/api/setup/state");
      $("offline").hidden = true;
    } catch (_) {
      $("offline").textContent = NET;
      $("offline").hidden = false;
      schedulePoll();
      return;
    }
    render();
    schedulePoll();
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    const aiBusy = !!S && (aiWaiting !== "" || S.ai.tools.some((t) => t.install.status === "running"));
    const need = !S || S.learningsuite.status === "running" || S.first_run.status === "running" || $("offline").hidden === false;
    if (need) pollTimer = setTimeout(load, 1000);
    else if (aiBusy) pollTimer = setTimeout(load, 2000);
  }

  const RENEW = location.hash === "#token";
  const AI_ONLY = location.hash === "#ai";
  const SETTINGS = location.pathname === "/settings";

  function render() {
    if (first) {
      first = false;
      if (SETTINGS && !S.current) { location.replace("/"); return; }
      if (SETTINGS) {
        document.body.classList.add("settings");
        $("page-title").textContent = "Settings";
        document.title = "Settings";
        document.querySelector(".hd .term").textContent = "Settings";
        $("finish-title").textContent = "Save";
      }
      if (AI_ONLY) {
        document.body.classList.add("ai-only");
        $("page-title").textContent = "Choose your AI";
        document.title = "Choose your AI";
      }
      if (RENEW) {
        document.body.classList.add("renew");
        $("page-title").textContent = "Renew your Canvas token";
        document.title = "Renew Canvas token";
        canvasReplacing = true;
      }
      $("canvas-link").href = S.canvas.base.replace(/\/+$/, "") + "/profile/settings";
      $("canvas-link").textContent = S.canvas.base.replace(/^https:\/\//, "").replace(/\/+$/, "") + "/profile/settings (opens in a new tab)";
      const d = SETTINGS ? S.current.week : S.defaults;
      $("f-timezone").value = d.timezone || "";
      $("f-wake").value = d.wake || "07:00";
      $("f-sleep").value = d.sleep || "23:00";
      $("f-term_start").value = d.term_start || "";
      $("f-term_end").value = d.term_end || "";
      $("replace-note").hidden = RENEW || AI_ONLY || SETTINGS || !(S.needed === false && S.first_run.status === "idle");
      if (S.canvas.saved && !S.canvas.verified && !autoChecked && !RENEW && !AI_ONLY) { autoChecked = true; verifySaved(); }
    }
    renderCanvas();
    renderLS();
    renderAI();
    renderCourses();
    renderProgress();
    renderSchedule();
  }

  let scheduleBusy = false;
  function renderSchedule() {
    if (!SETTINGS || !S.schedule) return;
    const sc = S.schedule;
    $("root-path").textContent = S.root || "";
    $("auto-status").textContent = !sc.supported
      ? "Automatic runs need a Mac. The Sync, Grades and Replan buttons in the planner still work."
      : sc.on ? "Automatic runs are on." : "Automatic runs are off.";
    $("auto-on").hidden = !sc.supported || sc.on;
    $("auto-off").hidden = !sc.supported || !sc.on;
    $("auto-on").disabled = $("auto-off").disabled = scheduleBusy;
  }

  async function setSchedule(on) {
    scheduleBusy = true;
    $("auto-err").textContent = "";
    renderSchedule();
    try {
      const d = await api("/api/setup/schedule", { on });
      if (typeof d.on === "boolean") S.schedule.on = d.on;
      if (!d.ok) $("auto-err").textContent = "That did not work: " + (d.error || "no reason given");
    } catch (_) {
      $("auto-err").textContent = NET;
    }
    scheduleBusy = false;
    renderSchedule();
  }
  $("auto-on").addEventListener("click", () => setSchedule(true));
  $("auto-off").addEventListener("click", () => setSchedule(false));

  let verifying = false;
  async function verifySaved() {
    verifying = true; renderCanvas();
    try {
      const r = await api("/api/setup/canvas", { base: S.canvas.base, token: "" });
      if (!r.ok) setErr($("canvas-err"), r.error || "Canvas did not accept the saved token.");
    } catch (_) { setErr($("canvas-err"), NET); }
    verifying = false;
    load();
  }

  function renderCanvas() {
    const c = S.canvas;
    const connected = (c.verified || (c.saved && verifying)) && !canvasReplacing;
    $("canvas-connected").hidden = !connected || canvasSkipped;
    $("canvas-form").hidden = connected || canvasSkipped;
    if (connected) {
      $("canvas-ok").textContent = c.verified
        ? `Connected as ${c.name} · ${c.count} ${c.count === 1 ? "course" : "courses"}`
        : "Checking your saved Canvas token…";
      $("canvas-ok").classList.toggle("ok-line", c.verified);
    }
    $("canvas-skip").setAttribute("aria-pressed", canvasSkipped);
    $("canvas-skip").textContent = canvasSkipped ? "Skipped. Set up Canvas after all" : "My classes don't use Canvas";
  }

  function renderLS() {
    const l = S.learningsuite, st = l.status;
    $("ls-idle").hidden = st !== "idle";
    $("ls-running").hidden = st !== "running" || lsSkipped;
    $("ls-ok").hidden = st !== "ok";
    $("ls-failed").hidden = st !== "failed";
    if (st === "ok") {
      $("ls-ok-text").textContent = `Found ${l.count} ${l.count === 1 ? "course" : "courses"}` + (l.term ? ` for ${l.term}` : "");
    }
    if (st === "failed") $("ls-fail-text").textContent = l.detail || "Sign-in did not finish.";
    $("ls-go").hidden = lsSkipped;
    $("ls-skip").setAttribute("aria-pressed", lsSkipped);
    $("ls-skip").textContent = lsSkipped ? "Skipped. Sign in after all" : "My classes don't use LearningSuite";
  }

  function sourceText(c) {
    const s = [];
    if (c.canvas_id) s.push("Canvas");
    if (c.ls_course_id) s.push("LearningSuite");
    return s.join(" + ");
  }

  const DAYS = [["mon", "M", "Monday"], ["tue", "T", "Tuesday"], ["wed", "W", "Wednesday"], ["thu", "Th", "Thursday"],
                ["fri", "F", "Friday"], ["sat", "Sa", "Saturday"], ["sun", "Su", "Sunday"]];

  function makeRow(c) {
    const n = ++rowSeq;
    const li = document.createElement("li");
    li.innerHTML = `
      <label class="pick" for="pk${n}"><input type="checkbox" id="pk${n}" checked>
        <span><span class="cname"></span><span class="src"></span></span></label>
      <div class="field f-use" hidden><label for="us${n}">Work is posted on</label>
        <select id="us${n}"><option value="both">Both</option><option value="canvas">Canvas only</option><option value="learningsuite">LearningSuite only</option></select></div>
      <div class="field f-credits"><label for="cr${n}">Credits</label>
        <input type="number" id="cr${n}" min="0.5" max="12" step="0.5" inputmode="decimal"><p class="err" id="cre${n}" role="alert"></p></div>
      <fieldset class="field f-meets"${SETTINGS ? " hidden" : ""}><legend>When it meets</legend>
        <div class="meet">
          <span class="days">${DAYS.map(([d, short, long]) => `<label class="day" title="${long}"><input type="checkbox" value="${d}" aria-label="${long}"><span>${short}</span></label>`).join("")}</span>
          <span class="times"><input type="time" id="ms${n}" aria-label="Starts at"> <span class="to">to</span> <input type="time" id="me${n}" aria-label="Ends at"></span>
        </div>
        <p class="hint locked" hidden>This course meets at more than one time, so it can't be changed here. Those times are kept in config/commitments.yaml in the planner's folder.</p>
        <p class="err" id="mte${n}" role="alert"></p></fieldset>`;
    const r = { li, n, use: li.querySelector(`#us${n}`), pick: li.querySelector(`#pk${n}`), credits: li.querySelector(`#cr${n}`), meets: li.querySelector(`#ms${n}`),
                end: li.querySelector(`#me${n}`), days: [...li.querySelectorAll(".day input")], locked: false,
                cErr: li.querySelector(`#cre${n}`), mErr: li.querySelector(`#mte${n}`) };
    r.credits.value = c.credits || 3;
    const clearMeet = () => { setErr(r.mErr, ""); r.meets.removeAttribute("aria-invalid"); r.meets.removeAttribute("aria-describedby"); };
    r.credits.addEventListener("input", () => { setErr(r.cErr, ""); r.credits.removeAttribute("aria-invalid"); r.credits.removeAttribute("aria-describedby"); });
    for (const el of [r.meets, r.end, ...r.days]) el.addEventListener("input", clearMeet);

    r.meets.addEventListener("change", () => {
      if (r.end.value || !r.meets.value) return;
      const [h, m] = r.meets.value.split(":").map(Number), t = Math.min(h * 60 + m + 50, 23 * 60 + 59);
      r.end.value = String(Math.floor(t / 60)).padStart(2, "0") + ":" + String(t % 60).padStart(2, "0");
    });

    r.getMeets = () => {
      if (r.locked || SETTINGS) return undefined;
      const days = r.days.filter((d) => d.checked).map((d) => d.value);
      if (!days.length && !r.meets.value && !r.end.value) return null;
      return { days, start: r.meets.value, end: r.end.value };
    };
    r.setMeets = (m, locked) => {
      r.locked = !!locked;
      for (const d of r.days) { d.checked = !!m && m.days.includes(d.value); d.disabled = r.locked; }
      r.meets.value = m ? m.start : ""; r.end.value = m ? m.end : "";
      r.meets.disabled = r.end.disabled = r.locked;
      li.querySelector(".locked").hidden = !r.locked;
      li.querySelector(".meet").hidden = r.locked;
    };
    if (c.meets || c.meets_locked) r.setMeets(c.meets, c.meets_locked);
    return r;
  }

  function renderCourses() {
    const list = $("course-list"), cs = SETTINGS ? S.current.courses : S.courses;
    const keys = new Set(cs.map((c) => c.key));
    for (const [k, r] of rows) if (!keys.has(k)) { r.li.remove(); rows.delete(k); }
    cs.forEach((c, i) => {
      let r = rows.get(c.key);
      if (!r) { r = makeRow(c); rows.set(c.key, r); }
      const nm = r.li.querySelector(".cname");
      nm.textContent = c.key;
      if (c.name && c.name !== c.key) {
        const f = document.createElement("span"); f.className = "cfull"; f.textContent = " " + c.name.replace(/^[^:]{1,24}?(?::|\s[-\u2013]\s)\s*/, ""); nm.appendChild(f);
      }
      r.li.querySelector(".src").textContent = sourceText(c);
      r.li.querySelector(".f-use").hidden = SETTINGS || !(c.canvas_id && c.ls_course_id);
      if (list.children[i] !== r.li) list.insertBefore(r.li, list.children[i] || null);
    });
    list.hidden = !cs.length;
    $("courses-empty").hidden = !!cs.length;
    $("sec-courses").classList.toggle("dim", !cs.length);
  }

  let aiChoice = null;
  let aiWaiting = "";
  let aiSent = "";
  const aiRows = new Map();

  function aiTool(cli) { return (S.ai.tools || []).find((t) => t.cli === cli) || null; }
  function aiReady(t) { return !!t && t.installed && t.signed_in === true; }

  function aiStateText(t) {
    if (t.install.status === "running") return "Installing…";
    if (!t.installed) return "Not installed";
    if (t.signed_in === true) return "Ready";
    if (t.signed_in === false) return "Installed, not signed in";
    return "Installed";
  }

  function renderAI() {
    const ai = S.ai;
    if (aiChoice === null) aiChoice = ai.choice || "";
    const list = $("ai-list");
    for (const t of ai.tools) {
      let r = aiRows.get(t.cli);
      if (!r) {
        const label = document.createElement("label");
        const input = document.createElement("input");
        input.type = "radio"; input.name = "ai"; input.value = t.cli;
        const logo = document.createElement("img");
        logo.className = "ai-logo"; logo.src = t.logo; logo.alt = ""; logo.width = 36; logo.height = 36;
        const name = document.createElement("span"); name.className = "ai-name"; name.textContent = t.name;
        const tiers = document.createElement("span"); tiers.className = "ai-tiers";
        for (const [kind, text] of t.tiers) {
          const b = document.createElement("span"); b.className = "tier " + kind; b.textContent = text;
          tiers.append(b);
        }
        const needs = document.createElement("span"); needs.className = "ai-needs"; needs.textContent = t.needs;
        const state = document.createElement("span"); state.className = "ai-state";
        const head = document.createElement("span"); head.className = "ai-head";
        head.append(input, logo, name);
        label.append(head, tiers, needs, state);
        list.append(label);
        input.addEventListener("change", () => chooseAI(t.cli));
        r = { input, state };
        aiRows.set(t.cli, r);
      }
      r.input.checked = aiChoice === t.cli;
      r.state.textContent = aiStateText(t);
      r.state.classList.toggle("ok", aiReady(t));
    }
    const skipped = aiChoice === "none";
    $("ai-skip").setAttribute("aria-pressed", skipped);
    $("ai-skip").textContent = skipped ? "Skipped. Choose one after all" : "Skip this for now";
    list.hidden = skipped;
    $("ai-intro").hidden = skipped;

    const t = skipped ? null : aiTool(aiChoice);
    const ready = aiReady(t);
    if (ready && aiWaiting === t.cli) aiWaiting = "";
    renderAccount(t && !ready ? t : null);
    $("ai-switch").hidden = !t || !t.can_switch;
    $("ai-ready").hidden = !ready;
    if (ready) $("ai-ready-text").textContent = t.name + " is ready. It will write your plans and time estimates.";
    $("ai-todo").hidden = !t || ready;
    $("ai-note").hidden = RENEW || AI_ONLY || SETTINGS || ready;
    if (!t || ready) return;

    const job = t.install.status;
    const inst = $("ai-install"), sign = $("ai-signin");
    inst.hidden = t.installed;
    sign.hidden = !t.installed;
    busy(inst, job === "running");
    inst.textContent = job === "running" ? "Installing " + t.name + "…" : (job === "failed" ? "Try again" : "Install " + t.name);
    sign.textContent = aiWaiting === t.cli ? "Open the sign-in window again" : "Sign in to " + t.name;
    let text;
    if (job === "running") text = "Installing " + t.name + ". This can take a few minutes; you can carry on below.";
    else if (!t.installed) text = t.name + " is not on this computer yet. The planner can install it for you.";
    else if (aiWaiting === t.cli) text = "A Terminal window opened. " + t.signin_help + " This page notices when you are done.";
    else if (t.signed_in === false) text = t.name + " is installed. Sign in to finish.";
    else text = t.name + " is installed. If you have not signed in to it on this computer yet, do that now.";
    $("ai-todo-text").textContent = text;
    if (job === "failed" && !t.installed) setErr($("ai-err"), "Installing did not work: " + (t.install.detail || "no details") + " You can try again, or do it yourself in Terminal.");
    else if (aiSent !== t.cli) setErr($("ai-err"), "");
    const box = $("ai-manual-cmds");
    if (box.dataset.cli !== t.cli) {
      box.dataset.cli = t.cli;
      box.textContent = "";
      for (const c of t.manual) {
        const pre = document.createElement("pre");
        pre.className = "cmd";
        pre.textContent = c;
        box.append(pre);
      }
    }
  }

  function renderAccount(t) {
    const a = t && t.account;
    const box = $("ai-account");
    box.hidden = !a;
    if (!a || box.dataset.cli === t.cli) return;
    box.dataset.cli = t.cli;
    $("ai-account-title").textContent = a.title;
    const steps = $("ai-account-steps"), notes = $("ai-account-notes");
    steps.textContent = ""; notes.textContent = "";
    for (const [text, url] of a.steps) {
      const li = document.createElement("li");
      li.textContent = text + " ";
      if (url) {
        const link = document.createElement("a");
        link.href = url; link.target = "_blank"; link.rel = "noopener";
        link.textContent = url.replace(/^https:\/\//, "") + " (opens in a new tab)";
        li.append(link);
      }
      steps.append(li);
    }
    for (const text of a.notes) {
      const li = document.createElement("li");
      li.textContent = text;
      notes.append(li);
    }
    $("ai-account-help").href = a.help;
  }

  async function chooseAI(cli) {
    aiChoice = cli;
    aiWaiting = "";
    aiSent = "";
    setErr($("ai-err"), "");
    render();
    try { await api("/api/setup/ai", { cli }); } catch (_) { setErr($("ai-err"), NET); }
  }

  $("ai-skip").addEventListener("click", async () => {
    const undo = aiChoice === "none";
    await chooseAI(undo ? "auto" : "none");
    if (undo) aiChoice = null;
    load();
  });

  $("ai-install").addEventListener("click", async () => {
    const cli = aiChoice;
    setErr($("ai-err"), "");
    try {
      const r = await api("/api/setup/ai-install", { cli });
      if (!r.ok) setErr($("ai-err"), r.error || "Could not start the install.");
    } catch (_) { setErr($("ai-err"), NET); }
    load();
  });

  async function signIn(btn, switchAccount) {
    const cli = aiChoice;
    setErr($("ai-err"), "");
    busy(btn, true);
    let problem = "";
    try {
      const r = await api("/api/setup/ai-signin", { cli, switch: switchAccount });
      if (!r.ok) problem = r.error || "Could not open the sign-in.";
      else {
        aiWaiting = cli;
        if (r.command) problem = "Could not open a Terminal window. Open Terminal yourself and run: " + r.command;
      }
    } catch (_) { problem = NET; }
    busy(btn, false);
    await load();
    if (problem) { aiSent = cli; $("ai-todo").hidden = false; setErr($("ai-err"), problem); }
  }
  $("ai-signin").addEventListener("click", () => signIn($("ai-signin"), false));
  $("ai-switch").addEventListener("click", () => signIn($("ai-switch"), true));

  function renderProgress() {
    const f = S.first_run;
    if (f.status === "idle") { if (!finishing) $("progress").hidden = true; return; }
    $("progress").hidden = false;
    const log = $("progress-log");
    const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
    const text = f.log.join("\n");
    if (log.textContent !== text) { log.textContent = text; if (atEnd) log.scrollTop = log.scrollHeight; }
    log.hidden = !f.log.length;
    const st = $("progress-status");
    if (f.status === "running") {
      st.innerHTML = '<span class="spin" aria-hidden="true"></span> Setting things up. This can take a few minutes. Keep this page open.';
      $("progress-open").hidden = true;
    } else if (f.status === "done") {
      st.textContent = "All set.";
      $("progress-open").hidden = false;
    } else {
      st.textContent = "Something went wrong along the way. Your courses are saved, and you can still open the planner.";
      $("progress-open").hidden = false;
    }
    const running = f.status === "running";
    finishing = running;
    busy($("finish-go"), running);
    $("finish-go").textContent = f.status === "error" ? "Try again" : "Finish setup";
  }

  $("canvas-go").addEventListener("click", async () => {
    const btn = $("canvas-go"), tok = $("canvas-token");
    const token = tok.value.trim();
    setErr($("canvas-err"), "");
    if (!token && !S.canvas.saved) { setErr($("canvas-err"), "Paste the token from Canvas first."); tok.focus(); return; }
    busy(btn, true);
    try {
      const r = await api("/api/setup/canvas", { base: S.canvas.base, token });
      if (r.ok) {
        tok.value = ""; canvasReplacing = false; await load();
        if (RENEW) {
          $("renew-done").hidden = false;
          api("/api/job", { name: "sync" }).catch(() => {});
        }
      }
      else setErr($("canvas-err"), r.error || "Canvas did not accept that token.");
    } catch (_) { setErr($("canvas-err"), NET); }
    busy(btn, false);
  });
  $("canvas-token").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("canvas-go").click(); } });
  $("canvas-replace").addEventListener("click", () => { canvasReplacing = true; renderCanvas(); $("canvas-token").focus(); });
  $("canvas-skip").addEventListener("click", () => { canvasSkipped = !canvasSkipped; renderCanvas(); });

  async function lsLogin(btn) {
    setErr($("ls-net"), "");
    busy(btn, true);
    try {
      const r = await api("/api/setup/ls-login", {});
      if (!r.ok) throw new Error("no");
      lsSkipped = false;
      await load();
    } catch (_) { setErr($("ls-net"), NET); }
    busy(btn, false);
  }
  for (const id of ["ls-go", "ls-retry", "ls-again"]) $(id).addEventListener("click", (e) => lsLogin(e.currentTarget));
  $("ls-skip").addEventListener("click", () => { lsSkipped = !lsSkipped; renderLS(); });

  const FIELDS = ["timezone", "wake", "sleep", "term_start", "term_end"];

  function fieldErr(input, errEl, msg) {
    setErr(errEl, msg);
    if (msg) { input.setAttribute("aria-invalid", "true"); input.setAttribute("aria-describedby", errEl.id); }
    else { input.removeAttribute("aria-invalid"); input.removeAttribute("aria-describedby"); }
  }

  function clearErrors() {
    setErr($("finish-err"), ""); setErr($("courses-err"), "");
    for (const f of FIELDS) fieldErr($("f-" + f), $("e-" + f), "");
    for (const r of rows.values()) { fieldErr(r.credits, r.cErr, ""); fieldErr(r.meets, r.mErr, ""); }
  }
  for (const f of FIELDS) $("f-" + f).addEventListener("input", () => fieldErr($("f-" + f), $("e-" + f), ""));

  function showErrors(errors) {
    const targets = [];
    if (errors.courses) { setErr($("courses-err"), errors.courses); targets.push($("courses-err")); }
    for (const [k, r] of rows) {
      if (errors["credits:" + k]) { fieldErr(r.credits, r.cErr, errors["credits:" + k]); targets.push(r.credits); }
      if (errors["meets:" + k]) { fieldErr(r.meets, r.mErr, errors["meets:" + k]); targets.push(r.meets); }
    }
    for (const f of FIELDS) if (errors[f]) { fieldErr($("f-" + f), $("e-" + f), errors[f]); targets.push($("f-" + f)); }
    const known = new Set(["courses", ...FIELDS, ...[...rows.keys()].flatMap((k) => ["credits:" + k, "meets:" + k])]);
    const other = Object.entries(errors).filter(([k]) => !known.has(k)).map(([, v]) => v);
    if (other.length) { setErr($("finish-err"), other.join(" ")); targets.push($("finish-err")); }

    targets.sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);
    const t = targets[0];
    if (t) { t.scrollIntoView({ block: "center", behavior: "smooth" }); t.focus({ preventScroll: true }); }
  }

  $("finish-go").addEventListener("click", async () => {
    const btn = $("finish-go");
    clearErrors();
    const courses = [];
    for (const [key, r] of rows) if (r.pick.checked) courses.push({ key, credits: r.credits.value.trim(), meets: r.getMeets(), use: r.use.value });
    const body = { courses, catch_up: $("f-catch_up").checked };
    for (const f of FIELDS) body[f] = $("f-" + f).value.trim();
    busy(btn, true); finishing = true;
    try {
      const r = await api("/api/setup/finish", body);
      if (r.ok) {
        $("progress").hidden = false;
        $("progress-status").textContent = "Starting…";
        $("progress").scrollIntoView({ block: "nearest", behavior: "smooth" });
        await load();
        return;
      }
      finishing = false; busy(btn, false);
      showErrors(r.errors || { x: r.error || "Something went wrong. Try again." });
    } catch (_) {
      finishing = false; busy(btn, false);
      setErr($("finish-err"), NET);
    }
  });

  $("save-go").addEventListener("click", async () => {
    const btn = $("save-go");
    clearErrors();
    $("save-done").hidden = true;
    const courses = [];
    for (const [key, r] of rows) {
      const c = { key, credits: r.credits.value.trim() }, m = r.getMeets();
      if (m !== undefined) c.meets = m;
      courses.push(c);
    }
    const body = { courses };
    for (const f of FIELDS) body[f] = $("f-" + f).value.trim();
    busy(btn, true);
    try {
      const r = await api("/api/setup/save", body);
      if (r.ok) {
        const list = $("save-list");
        list.textContent = "";
        for (const line of r.changed) { const li = document.createElement("li"); li.textContent = line; list.append(li); }
        $("save-text").textContent = r.changed.length
          ? "Saved. Press Replan in the planner to rebuild today's plan with these."
          : "Nothing was different, so nothing was changed.";
        $("save-backup").hidden = !r.backups.length;
        $("save-done").hidden = false;
        $("save-done").scrollIntoView({ block: "nearest", behavior: "smooth" });
      } else showErrors(r.errors || { x: r.error || "Something went wrong. Try again." });
    } catch (_) { setErr($("finish-err"), NET); }
    busy(btn, false);
  });

  load();
})();
