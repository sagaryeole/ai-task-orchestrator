    const board = document.getElementById("task-board");
    const modal = document.getElementById("task-modal");
    const closeBtn = document.getElementById("modal-close");
    let taskMap = new Map();
    let latestData = { tasks: [] };

    function esc(s) { return (s ?? "").toString(); }

    function providerLabel(name) {
      const n = esc(name) || "none";
      const first = (n.trim()[0] || "?").toUpperCase();
      return `[${first}] ${n}`;
    }

    function fmtDur(v) {
      if (v === null || v === undefined || v === "") return "--";
      const n = Math.max(0, Math.floor(Number(v)));
      const m = Math.floor(n / 60);
      const s = n % 60;
      return `${m}:${String(s).padStart(2, "0")}`;
    }

    function fmtEta(v) {
      if (v === null || v === undefined) return "--";
      const n = Math.max(0, Math.floor(Number(v)));
      if (n < 60) return `${n}s`;
      const m = Math.floor(n / 60);
      const s = n % 60;
      if (m < 60) return `${m}m ${s}s`;
      const h = Math.floor(m / 60);
      const rm = m % 60;
      return `${h}h ${rm}m`;
    }

    function openModal(task) {
      document.getElementById("modal-title").textContent = esc(task.title);
      document.getElementById("modal-status").textContent = esc(task.status);
      document.getElementById("modal-provider").textContent = esc(task.provider) || "none";
      document.getElementById("modal-attempt").textContent = String(task.attempt || 0);
      document.getElementById("modal-started").textContent = esc(task.started_at) || "--";
      document.getElementById("modal-finished").textContent = esc(task.finished_at) || "--";
      document.getElementById("modal-duration").textContent = fmtDur(task.duration_seconds);
      document.getElementById("modal-exit").textContent = (task.exit_code === null || task.exit_code === undefined) ? "--" : String(task.exit_code);
      document.getElementById("modal-verify").textContent = (task.verification_passed === null || task.verification_passed === undefined) ? "--" : String(task.verification_passed);
      document.getElementById("modal-error").textContent = esc(task.error_summary) || "--";
      modal.classList.add("open");
    }

    closeBtn.addEventListener("click", () => modal.classList.remove("open"));
    modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.remove("open"); });
    window.addEventListener("keydown", (e) => { if (e.key === "Escape") modal.classList.remove("open"); });

    function taskHash(t) {
      return JSON.stringify([t.status, t.provider, t.started_at, t.finished_at, t.duration_seconds, t.attempt, t.exit_code, t.verification_passed, t.error_summary]);
    }

    function renderTaskCard(task) {
      const el = document.createElement("article");
      el.className = `task ${task.status || "pending"}`;
      el.dataset.id = task.id;
      el.dataset.startedAt = task.started_at || "";
      el.dataset.status = task.status || "pending";
      const attempts = (task.attempt && task.attempt > 1) ? `<span class="attempt-badge">attempt ${task.attempt}</span>` : "";
      el.innerHTML = `
        <div class="t" title="${esc(task.title)}">${esc(task.title)}</div>
        <div class="status">${esc(task.status || "pending")}</div>
        <div class="sub">
          <span><span class="provider-badge">${providerLabel(task.provider)}</span>${attempts}</span>
          <span class="dur">${fmtDur(task.duration_seconds)}</span>
        </div>`;
      el.addEventListener("click", () => openModal(task));
      return el;
    }

    function syncTaskCards(tasks) {
      const nextIds = new Set();
      for (const t of tasks) {
        const id = t.id || t.title;
        nextIds.add(id);
        const hash = taskHash(t);
        const existing = taskMap.get(id);
        if (!existing) {
          const card = renderTaskCard(t);
          card.dataset.hash = hash;
          board.appendChild(card);
          taskMap.set(id, card);
          continue;
        }
        if (existing.dataset.hash !== hash) {
          const card = renderTaskCard(t);
          card.dataset.hash = hash;
          existing.replaceWith(card);
          taskMap.set(id, card);
          if (t.status === "complete" || t.status === "failed") {
            const glow = t.status === "failed" ? "rgba(239,68,68,.35)" : "rgba(34,197,94,.35)";
            card.style.boxShadow = `0 0 0 3px ${glow}`;
            setTimeout(() => { card.style.boxShadow = ""; }, 900);
          }
        }
      }
      for (const [id, el] of taskMap.entries()) {
        if (!nextIds.has(id)) {
          el.remove();
          taskMap.delete(id);
        }
      }
    }

    function tickRunningDurations() {
      const now = Date.now();
      board.querySelectorAll(".task.running").forEach((card) => {
        const started = card.dataset.startedAt;
        if (!started) return;
        const ms = Date.parse(started);
        if (Number.isNaN(ms)) return;
        const dur = Math.max(0, Math.floor((now - ms) / 1000));
        const durEl = card.querySelector(".dur");
        if (durEl) durEl.textContent = fmtDur(dur);
      });
    }

    function renderProviders(list) {
      const root = document.getElementById("providers-chips");
      root.innerHTML = "";
      const now = Date.now() / 1000;
      for (const p of list || []) {
        const el = document.createElement("span");
        if (p.available) {
          el.className = "chip available";
          el.textContent = `available \u2022 ${p.name}`;
        } else if (p.cooldown_until) {
          el.className = "chip cooldown";
          el.textContent = `cooldown \u2022 ${p.name} (${Math.max(0, Math.floor(p.cooldown_until - now))}s)`;
        } else {
          el.className = "chip cooldown";
          el.textContent = `cooldown \u2022 ${p.name}`;
        }
        root.appendChild(el);
      }
    }

    function renderHistory(hist) {
      const root = document.getElementById("events-list");
      root.innerHTML = "";
      if (!hist || hist.length === 0) {
        const li = document.createElement("li");
        li.textContent = "No history yet";
        root.appendChild(li);
        return;
      }
      for (const h of hist.slice(-12).reverse()) {
        const li = document.createElement("li");
        li.textContent = `${h.status || ""} \u2022 ${h.provider || "none"} \u2022 ${h.task || ""}`;
        root.appendChild(li);
      }
    }

    function renderSummary(sum) {
      const total = Number(sum?.total || 0);
      const completed = Number(sum?.completed || 0);
      const failed = Number(sum?.failed || 0);
      const running = Number(sum?.running || 0);
      const pending = Number(sum?.pending || 0);
      document.getElementById("m-total").textContent = String(total);
      document.getElementById("m-completed").textContent = String(completed);
      document.getElementById("m-failed").textContent = String(failed);
      document.getElementById("m-running").textContent = String(running);
      document.getElementById("m-pending").textContent = String(pending);
      document.getElementById("m-eta").textContent = fmtEta(sum?.estimated_remaining_seconds);
      const pct = (total > 0) ? Math.floor((completed / total) * 100) : 0;
      document.getElementById("progress-fill").style.width = `${pct}%`;
    }

    async function refreshState() {
      try {
        const r = await fetch("/api/state", { cache: "no-store" });
        if (!r.ok) return;
        const d = await r.json();
        latestData = d;
        document.getElementById("uptime").textContent = `Uptime: ${d.uptime_seconds}s`;
        document.getElementById("current-task").textContent = `Task: ${d.current_task || "idle"}`;
        document.getElementById("current-provider").textContent = `Provider: ${d.current_provider || "none"}`;
        renderSummary(d.run_summary || {});
        renderProviders(d.providers || []);
        syncTaskCards(d.tasks || []);
        renderHistory(d.history || []);
      } catch (_) { /* silent */ }
    }

    setInterval(refreshState, 3000);
    setInterval(tickRunningDurations, 1000);
    refreshState();