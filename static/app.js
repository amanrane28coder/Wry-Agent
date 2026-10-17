(() => {
  const $ = (selector) => document.querySelector(selector);
  const target = $("#target");
  const traceButton = $("#trace-button");
  const saveButton = $("#save-plan");
  const indexButton = $("#index-repo");
  const evalButton = $("#evaluate-button");
  const runAgentButton = $("#run-agent-button");
  const otherTarget = $("#other-target");
  const moduleFilter = $("#module-filter");
  let latestTarget = null;
  let planProposalToken = null;
  let repositoryStatus = null;
  let allTargets = [];
  let targetsReachable = false;

  async function initialize() {
    const [statusResponse, targetsResponse] = await Promise.all([
      fetch("/api/status"), fetch("/api/targets")
    ]);
    repositoryStatus = await statusResponse.json();
    targetsReachable = targetsResponse.ok;
    const targets = targetsReachable ? await targetsResponse.json() : [];
    updateConnection(repositoryStatus);
    updateRepositoryLabel(repositoryStatus);
    renderTargets(targets);
    $("#module-list").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-path]");
      if (!button) return;
      target.value = button.dataset.path;
      document.querySelectorAll(".module-item").forEach(item => item.classList.toggle("selected", item === button));
    });
    moduleFilter.addEventListener("input", renderModuleList);
    target.addEventListener("change", renderModuleList);
    if (!repositoryStatus.configured) {
      $("#query-message").textContent = "Set FalkorDB connection values in wry_hackathon/.env to run graph tools.";
      indexButton.disabled = true;
      evalButton.disabled = true;
      runAgentButton.disabled = true;
    } else if (!targetsReachable) {
      $("#query-message").textContent = "FalkorDB is configured but not reachable. Check the instance is running and the .env values.";
      indexButton.disabled = true;
      evalButton.disabled = true;
      runAgentButton.disabled = true;
    }
  }

  function updateConnection(status) {
    $("#connection-text").textContent = !status.configured
      ? "FALKORDB NOT CONFIGURED"
      : (targetsReachable ? `FALKORDB · ${status.graph.toUpperCase()}` : "FALKORDB UNREACHABLE");
    $("#connection-dot").classList.toggle("online", status.configured && targetsReachable);
    $("#agent-mode-label").textContent = `${status.agent_mode}. Analysis is read-only; model selection is opt-in.`;
  }

  function updateRepositoryLabel(status) {
    $("#repo-name").textContent = status.name;
    $("#repo-icon").textContent = status.name.slice(0, 1).toUpperCase();
    $("#repo-kind").textContent = status.dataset === "local" ? "LOCAL PYTHON METADATA" : "SYNTHETIC DEMO GRAPH";
    $("#repo-scope").textContent = status.dataset === "local" ? "LOCAL PYTHON REPOSITORY · METADATA ONLY" : "SYNTHETIC DEMO REPOSITORY";
    indexButton.textContent = status.dataset === "local" ? "RE-INDEX LOCAL REPOSITORY" : "INDEX LOCAL PYTHON REPOSITORY";
    $("#index-details").textContent = status.repository_index_enabled
      ? `Target: ${status.index_repository}. Sends relative Python paths, module/test and criticality labels, and import/test edges to FalkorDB. Source text stays local. Use only public or otherwise non-restricted repository metadata.`
      : "Repository indexing is off. The Graph Hacks rules prohibit private or restricted data in FalkorDB; the synthetic demo remains active.";
    $("#repo-index-status").textContent = status.repository_index_enabled ? "ENABLED" : "OFF";
    $("#repo-index-status").style.color = status.repository_index_enabled ? "var(--green)" : "var(--amber)";
    indexButton.disabled = !status.configured || !status.repository_index_enabled;
  }

  function renderTargets(targets) {
    allTargets = targets;
    const options = targets.map(item => `<option value="${escapeHtml(item.path)}">${escapeHtml(item.path)}</option>`).join("");
    target.innerHTML = options;
    otherTarget.innerHTML = `<option value="">PATH TO · OPTIONAL</option>` + options;
    renderModuleList();
    target.disabled = !targets.length;
    traceButton.disabled = !targets.length || !repositoryStatus?.configured;
    runAgentButton.disabled = !targets.length || !repositoryStatus?.configured;
  }

  function renderModuleList() {
    const query = moduleFilter.value.trim().toLowerCase();
    const visibleTargets = allTargets.filter(item => item.path.toLowerCase().includes(query));
    const selectedPath = target.value;
    $("#module-list").innerHTML = visibleTargets.map(item => `
      <button class="module-item ${item.path === selectedPath ? "selected" : ""}" data-path="${escapeHtml(item.path)}" title="${escapeHtml(item.path)}">
        <span class="file-dot ${escapeHtml(item.criticality)}"></span><span>${escapeHtml(item.path.split("/").pop())}</span>
      </button>`).join("");
    $("#module-count").textContent = query ? `${visibleTargets.length}/${allTargets.length}` : String(allTargets.length);
    $("#module-empty").hidden = visibleTargets.length > 0;
  }

  indexButton.addEventListener("click", async () => {
    if (!repositoryStatus?.configured || !repositoryStatus.repository_index_enabled) return;
    const approved = window.confirm(`Confirm that ${repositoryStatus.index_repository} is public or otherwise non-restricted and that its metadata is permitted in FalkorDB. Send relative Python file paths, module/test and criticality labels, and import/test edges? Source file contents stay on this machine.`);
    if (!approved) return;
    indexButton.disabled = true;
    $("#index-message").textContent = "Scanning Python metadata locally, then indexing in FalkorDB...";
    try {
      const response = await fetch("/api/repository/index", {
        method: "POST", headers: {"X-Wry-Action": "index-local-repository"}
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Repository indexing failed");
      const [statusResponse, targetsResponse] = await Promise.all([fetch("/api/status"), fetch("/api/targets")]);
      repositoryStatus = await statusResponse.json();
      renderTargets(await targetsResponse.json());
      updateRepositoryLabel(repositoryStatus);
      latestTarget = null;
      saveButton.disabled = true;
      $("#results").hidden = true;
      $("#empty-state").hidden = false;
      $("#index-message").textContent = `${result.source_files} modules · ${result.test_files} tests · ${result.import_edges} imports indexed.`;
    } catch (error) {
      $("#index-message").textContent = error.message;
    } finally {
      indexButton.disabled = !repositoryStatus?.configured || !repositoryStatus.repository_index_enabled;
    }
  });

  traceButton.addEventListener("click", async () => {
    traceButton.disabled = true;
    runAgentButton.disabled = true;
    saveButton.disabled = true;
    $("#query-message").textContent = "Selecting MCP tool and querying FalkorDB...";
    try {
      const response = await fetch("/api/agent/investigate", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({prompt: $("#prompt").value, target_path: target.value, other_path: otherTarget.value || null})
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Investigation failed");
      latestTarget = Array.isArray(payload.result) ? null : target.value;
      planProposalToken = payload.plan_proposal_token || null;
      renderResult(payload);
      saveButton.disabled = !latestTarget || !!payload.needs_input || !planProposalToken;
      $("#query-message").textContent = "Graph traversal complete";
    } catch (error) {
      $("#query-message").textContent = error.message;
    } finally {
      traceButton.disabled = !target.value;
      runAgentButton.disabled = !target.value;
    }
  });

  runAgentButton.addEventListener("click", async () => {
    runAgentButton.disabled = true;
    traceButton.disabled = true;
    saveButton.disabled = true;
    $("#query-message").textContent = "Agent running: choosing tools and querying FalkorDB...";
    try {
      const response = await fetch("/api/agent/run", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({prompt: $("#prompt").value, target_path: target.value, other_path: otherTarget.value || null})
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Agent run failed");
      latestTarget = target.value;
      planProposalToken = (payload.proposed_action && payload.proposed_action.proposal_token) || null;
      renderResult(payload);
      saveButton.disabled = !latestTarget || !!payload.needs_input || !planProposalToken;
      $("#query-message").textContent = `Agent finished in ${payload.steps.length} step${payload.steps.length === 1 ? "" : "s"}`;
    } catch (error) {
      $("#query-message").textContent = error.message;
    } finally {
      runAgentButton.disabled = !target.value;
      traceButton.disabled = !target.value;
    }
  });

  async function savePlan() {
    if (!latestTarget) return;
    if (!planProposalToken) {
      $("#plan-message").textContent = "No proposed plan for this target — run the agent or trace impact first.";
      return;
    }
    saveButton.disabled = true;
    const approveButton = $("#approve-plan");
    if (approveButton) approveButton.disabled = true;
    $("#plan-message").textContent = "Saving proposed plan to FalkorDB...";
    try {
      const response = await fetch("/api/agent/test-plan", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({target_path: latestTarget, rationale: $("#prompt").value, proposal_token: planProposalToken})
      });
      const plan = await response.json();
      if (!response.ok) throw new Error(plan.detail || "Could not save plan");
      planProposalToken = null; // single-use: a saved plan cannot be saved twice
      $("#plan-message").textContent = `${plan.action} Plan ${plan.plan_id.slice(0, 8)}.`;
      $("#proposed-action").hidden = true;
    } catch (error) {
      $("#plan-message").textContent = error.message;
    } finally {
      saveButton.disabled = false;
      if (approveButton) approveButton.disabled = false;
    }
  }

  saveButton.addEventListener("click", savePlan);
  $("#approve-plan").addEventListener("click", savePlan);

  function renderResult(payload) {
    $("#empty-state").hidden = true;
    $("#results").hidden = false;
    // A previous needs_input render may have hidden sections; restore them
    // before the normal render path re-applies its own visibility rules.
    document.querySelectorAll("#results .analysis-section").forEach(el => {
      el.hidden = false;
      el.style.display = "";
    });
    if (payload.needs_input) {
      // The agent is waiting on the user (e.g. a second module for a path
      // query). Show the trace and the question; hide every analysis section.
      $("#result-target").textContent = "Input needed";
      renderAgentTrace(payload.steps);
      renderFinalAnswer(payload.final_answer);
      renderProposedAction(null);
      document.querySelectorAll("#results .analysis-section").forEach(el => { el.hidden = true; });
      $("#plan-message").textContent = "";
      return;
    }
    const listing = Array.isArray(payload.result);
    const tool = payload.selected_tool;
    const isLoop = !!(payload.steps && payload.steps.length);
    const result = listing ? {
      target: "Available code modules",
      impact_count: null,
      affected_files: payload.result.map(item => ({
        path: item.path, distance: 1, criticality: item.criticality, path_nodes: [item.path]
      })),
      recommended_tests: [],
      change_surface_score: null,
      reasoning: `FalkorDB MCP returned ${payload.result.length} indexed modules.`,
      provenance: repositoryStatus.dataset === "local" ? "Local Python paths and import metadata; source text stayed local." : "Synthetic repository fixture."
    } : payload.result;
    $("#empty-state").hidden = true;
    $("#results").hidden = false;
    const isChain = !listing && (tool === "shortest_dependency_path_tool" || result.tool === "shortest_dependency_path");
    const isBridges = !listing && (tool === "find_bridge_modules_tool" || result.tool === "find_bridge_modules");
    // Bridge results are a global overview with no target module; the generic
    // fallback heading would mislead, so they get their own title.
    $("#result-target").textContent = result.target || result.from || (isBridges ? "Architectural choke points" : "Available code modules");
    if (isLoop) {
      $("#selected-tool").textContent = `AGENT LOOP · ${payload.steps.length} STEPS · MCP TOOLS`;
      $("#selection-reason").textContent = "The agent chained tools itself. Each step's thought, action, and observation is shown below.";
    } else {
      $("#selected-tool").textContent = `${payload.selection_mode.toUpperCase()} · MCP TOOL · ${tool}`;
      $("#selection-reason").textContent = payload.selection_reason;
    }
    renderAgentTrace(payload.steps);
    renderFinalAnswer(payload.final_answer);
    renderProposedAction(payload.proposed_action);

    const isCommunities = !listing && (tool === "find_impact_communities_tool" || result.tool === "find_impact_communities");
    const isRecall = !listing && result.tool === "recall_investigations";
    const showStandard = !listing && !isChain && !isBridges && !isCommunities && !isRecall;

    $("#metrics").style.display = showStandard || listing ? "" : "none";
    if (showStandard || listing) {
      $("#metrics").innerHTML = `
        <div class="metric"><span>${listing ? "INDEXED MODULES" : "TRANSITIVE IMPORTERS"}</span><strong>${listing ? result.affected_files.length : result.impact_count ?? "—"}</strong></div>
        <div class="metric"><span>TEST FILES TO RETEST</span><strong>${listing ? "—" : result.recommended_tests?.length ?? "—"}</strong></div>
        <div class="metric"><span>REPOSITORY SURFACE</span><strong>${listing ? "—" : result.change_surface_score ?? "—"}${listing ? "" : "<small>%</small>"}</strong></div>`;
    }

    const impactHeading = $("#impact-list").previousElementSibling;
    impactHeading.style.display = showStandard ? "" : "none";
    $("#impact-map-heading").style.display = showStandard ? "" : "none";
    $("#impact-map").hidden = !showStandard;
    $("#impact-list").style.display = showStandard ? "" : "none";
    if (showStandard) {
      $("#path-count").textContent = `${result.affected_files.length} ${listing ? "MODULES" : "FILES IN GRAPH PATHS"}`;
      if (!listing) renderImpactMap(result.affected_files);
      $("#impact-list").innerHTML = result.affected_files.map(item => `
        <article class="impact-row">
          <span class="distance">${item.distance === 0 ? "TARGET" : `${item.distance} HOP${item.distance === 1 ? "" : "S"}`}</span>
          <div class="path-body"><div class="path-chain">${item.path_nodes.map(path => `<span>${escapeHtml(path)}</span>`).join('<b>←</b>')}</div><small>${escapeHtml(item.criticality)} criticality · importer depends on the next module in the path</small></div>
        </article>`).join("");
    }

    renderChain(isChain ? result : null);
    renderBridges(isBridges ? result : null);
    renderCommunities(isCommunities ? result : (result.communities || null));

    const testsHeading = document.querySelector("#test-list").previousElementSibling;
    const showTests = showStandard && !listing;
    testsHeading.style.display = showTests ? "" : "none";
    $("#test-list").style.display = showTests ? "" : "none";
    if (showTests) {
      $("#test-list").innerHTML = result.recommended_tests.length ? result.recommended_tests.map(item => `
        <div class="test-row"><span class="check-glyph">✓</span><code>${escapeHtml(item.path)}</code><span class="covers">COVERS ${item.covers.map(path => escapeHtml(path.split("/").pop())).join(" · ")}</span></div>`).join("") : '<div class="no-tests">No coverage edges found for this change surface.</div>';
    }
    const pagerankHeading = $("#pagerank-heading"), pagerankList = $("#pagerank-list");
    if (showTests && result.ranked_tests && result.ranked_tests.length) {
      pagerankHeading.hidden = false;
      pagerankList.hidden = false;
      pagerankList.innerHTML = result.ranked_tests.map((item, index) => `
        <div class="test-row"><span class="check-glyph">${index + 1}</span><code>${escapeHtml(item.path)}</code><span class="covers">PAGERANK ${item.pagerank_score} · TOP COVERS ${escapeHtml((item.top_covered_module || "").split("/").pop())}</span></div>`).join("");
    } else {
      pagerankHeading.hidden = true;
      pagerankList.hidden = true;
    }
    renderMemory(isRecall ? result : payload.past_investigations);
    $("#provenance").textContent = `${result.reasoning} ${result.provenance}`;
    renderCypher(result, listing);
    $("#plan-message").textContent = "";
  }

  function renderAgentTrace(steps) {
    const wrap = $("#agent-trace-wrap");
    if (!steps || !steps.length) {
      wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    $("#agent-trace").innerHTML = steps.map(step => `
      <div class="trace-step${String(step.action).startsWith("proposed:") ? " is-proposed" : ""}" data-step="${step.step}"><div>
        <div class="trace-thought">${escapeHtml(step.thought)}</div>
        <div class="trace-action">${escapeHtml(step.action)}</div>
        <div class="trace-observation">${escapeHtml(step.observation)}</div>
      </div></div>`).join("");
  }

  function renderFinalAnswer(finalAnswer) {
    const wrap = $("#final-answer-wrap");
    if (!finalAnswer) {
      wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    $("#final-answer").textContent = finalAnswer;
  }

  function renderProposedAction(proposed) {
    const el = $("#proposed-action");
    if (!proposed) {
      el.hidden = true;
      return;
    }
    el.hidden = false;
    $("#proposed-message").textContent = proposed.message;
    $("#proposed-tests").innerHTML = (proposed.tests || []).map(path => `<code>${escapeHtml(path)}</code>`).join("");
  }

  function renderChain(result) {
    const wrap = $("#chain-wrap");
    if (!result) {
      wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    if (!result.connected) {
      $("#chain-hops").textContent = "NO PATH";
      $("#chain-view").innerHTML = `<div class="chain-note">${escapeHtml(result.reasoning)}</div>`;
      return;
    }
    $("#chain-hops").textContent = `${result.hops} HOP${result.hops === 1 ? "" : "S"}`;
    const nodes = result.chain.map((path, index, arr) => {
      const cls = (index === 0 || index === arr.length - 1) ? "chain-node endpoint" : "chain-node";
      return `<span class="${cls}" title="${escapeHtml(path)}">${escapeHtml(path.split("/").pop())}</span>`;
    }).join('<span class="chain-arrow">→</span>');
    $("#chain-view").innerHTML = `<div class="chain-links">${nodes}</div><div class="chain-note">${escapeHtml(result.reasoning)}</div>`;
  }

  function renderBridges(result) {
    const heading = $("#bridges-heading"), list = $("#bridges-list");
    const bridges = result ? result.bridges || [] : [];
    if (!bridges.length) {
      heading.hidden = true;
      list.hidden = true;
      return;
    }
    heading.hidden = false;
    list.hidden = false;
    list.innerHTML = bridges.map((item, index) => `
      <div class="test-row"><span class="check-glyph">${index + 1}</span><code>${escapeHtml(item.path)}</code><span class="covers">BETWEENNESS ${item.betweenness_score}</span></div>`).join("");
  }

  function renderCommunities(result) {
    const heading = $("#communities-heading"), list = $("#communities-list");
    if (!result || !Array.isArray(result.communities) || !result.communities.length) {
      heading.hidden = true;
      list.hidden = true;
      return;
    }
    heading.hidden = false;
    list.hidden = false;
    const rows = result.communities.map(c => {
      const cls = ["community-row"];
      if (c.touched) cls.push("touched");
      if (c.contains_target) cls.push("target-community");
      const note = c.contains_target ? "TARGET'S COMMUNITY"
        : c.touched ? "TOUCHED BY IMPACT" : "UNTOUCHED";
      return `<div class="${cls.join(" ")}"><span class="community-id">C${c.community}</span>`
        + `<div class="community-body"><div class="community-members">`
        + c.members.map(m => `<code>${escapeHtml(m.split("/").pop())}</code>`).join("")
        + `</div><small>${c.size} MODULE${c.size === 1 ? "" : "S"} · ${note}</small></div></div>`;
    }).join("");
    list.innerHTML = `<div class="community-summary">Blast radius touches `
      + `<strong>${result.impacted_communities}</strong> of <strong>${result.community_count}</strong> `
      + `architectural communities · target sits in <strong>C${result.target_community}</strong></div>` + rows;
  }

  function renderMemory(memory) {
    const heading = $("#memory-heading"), list = $("#memory-list");
    if (!memory || !Array.isArray(memory.investigations)) {
      heading.hidden = true;
      list.hidden = true;
      return;
    }
    heading.hidden = false;
    list.hidden = false;
    list.innerHTML = memory.investigations.length ? memory.investigations.map(inv => `
      <div class="memory-row"><span class="memory-time">${escapeHtml(relativeTime(inv.created_at))}</span><span>${escapeHtml(inv.summary || `${inv.importer_count} importers · ${inv.test_count} tests`)}</span></div>`).join("")
      : '<div class="memory-empty">No past investigations of this module yet — this one will be remembered.</div>';
  }

  function relativeTime(timestampMs) {
    const seconds = Math.max(0, Math.floor((Date.now() - Number(timestampMs)) / 1000));
    if (seconds < 60) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
  }

  function renderCypher(result, listing) {
    const cypherDetails = $("#cypher-details");
    if (!result.cypher || listing) {
      cypherDetails.hidden = true;
      return;
    }
    cypherDetails.hidden = false;
    const timings = result.timings || {};
    const blocks = [
      ["impact_traversal", "transitive importers, up to 5 hops"],
      ["test_coverage", "covering tests for the change surface"],
      ["pagerank", "PageRank over the import graph"],
      ["betweenness", "betweenness centrality over the import graph"],
      ["communities", "CDLP community detection over the import graph"],
      ["shortest_path", "shortest dependency path"],
      ["recall", "past investigations from agent memory"],
    ];
    let totalMs = 0;
    const text = blocks
      .filter(([key]) => result.cypher[key])
      .map(([key, comment]) => {
        const ms = timings[`${key}_ms`];
        if (typeof ms === "number") totalMs += ms;
        const tag = typeof ms === "number" ? ` · ${ms}ms` : "";
        return `// ${comment}${tag}\n${result.cypher[key]}`;
      })
      .join("\n\n");
    $("#cypher-query").textContent = text;
    $("#cypher-summary").textContent =
      `GRAPH QUERY · CYPHER EXECUTED IN FALKORDB${totalMs ? ` · ${Math.round(totalMs * 10) / 10}MS TOTAL` : ""}`;
  }

  function renderImpactMap(affectedFiles) {
    const mapEl = $("#impact-map");
    const svg = $("#impact-svg");
    if (!affectedFiles.length) {
      mapEl.hidden = true;
      return;
    }
    mapEl.hidden = false;
    const nodes = new Map();
    for (const file of affectedFiles) {
      if (!nodes.has(file.path)) {
        nodes.set(file.path, {
          path: file.path,
          distance: file.distance,
          criticality: file.criticality,
          chain: file.path_nodes && file.path_nodes.length ? file.path_nodes : [file.path]
        });
      }
    }
    // Each chain runs target-first, node-last (path_nodes from the trace), so
    // the module a node directly depends on is the previous entry: edges run
    // left (dependency) to right (importer), the direction a change propagates.
    const parentOf = (node) => {
      const index = node.chain.indexOf(node.path);
      return index > 0 ? node.chain[index - 1] || null : null;
    };
    const maxDistance = Math.max(...[...nodes.values()].map(node => node.distance));
    const columns = [];
    for (let d = 0; d <= maxDistance; d++) {
      columns.push([...nodes.values()].filter(node => node.distance === d));
    }
    const NODE_W = 176, NODE_H = 36, COL_W = 198, ROW_H = 50;
    const PAD_X = 18, PAD_TOP = 34, PAD_BOTTOM = 16;
    const pos = new Map();
    columns.forEach((column, d) => {
      const ordered = [...column].sort((a, b) => {
        if (d === 0) return a.path.localeCompare(b.path);
        const parentA = parentOf(a), parentB = parentOf(b);
        const yA = parentA && pos.get(parentA) ? pos.get(parentA).y : -1;
        const yB = parentB && pos.get(parentB) ? pos.get(parentB).y : -1;
        return (yA - yB) || a.path.localeCompare(b.path);
      });
      ordered.forEach((node, i) => pos.set(node.path, {x: PAD_X + d * COL_W, y: PAD_TOP + i * ROW_H}));
    });
    const width = PAD_X * 2 + maxDistance * COL_W + NODE_W;
    const height = PAD_TOP + Math.max(...columns.map(column => column.length)) * ROW_H + PAD_BOTTOM;
    const dotFill = (node) => node.distance === 0 ? "#d9480f"
      : node.criticality === "critical" ? "#c03527"
      : node.criticality === "high" ? "#a86e08" : "#9aa0a8";
    const parts = [];
    columns.forEach((column, d) => {
      const label = d === 0 ? "TARGET" : d === 1 ? "1 HOP" : `${d} HOPS`;
      parts.push(`<text class="map-col-label" x="${PAD_X + d * COL_W}" y="16">${label}</text>`);
    });
    let edgeCount = 0;
    for (const node of nodes.values()) {
      const parent = parentOf(node);
      if (!parent || !pos.has(parent)) continue;
      const from = pos.get(parent), to = pos.get(node.path);
      const x1 = from.x + NODE_W, y1 = from.y + NODE_H / 2;
      const x2 = to.x, y2 = to.y + NODE_H / 2;
      const midX = (x1 + x2) / 2;
      parts.push(`<path class="map-edge" d="M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}"/>`);
      edgeCount++;
    }
    for (const node of nodes.values()) {
      const p = pos.get(node.path);
      const base = node.path.split("/").pop();
      const label = base.length > 24 ? `${base.slice(0, 23)}…` : base;
      parts.push(`<g class="map-node${node.distance === 0 ? " target" : ""}">`
        + `<title>${escapeHtml(node.path)}</title>`
        + `<rect x="${p.x}" y="${p.y}" width="${NODE_W}" height="${NODE_H}"/>`
        + `<circle cx="${p.x + 16}" cy="${p.y + NODE_H / 2}" r="3.5" fill="${dotFill(node)}"/>`
        + `<text x="${p.x + 28}" y="${p.y + NODE_H / 2 + 3.5}">${escapeHtml(label)}</text></g>`);
    }
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.innerHTML = parts.join("");
    $("#map-count").textContent = `${nodes.size} NODES · ${edgeCount} EDGES`;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]));
  }

  evalButton.addEventListener("click", async () => {
    evalButton.disabled = true;
    $("#eval-message").textContent = "Running evaluation against FalkorDB...";
    try {
      const response = await fetch("/api/agent/evaluate");
      const report = await response.json();
      if (!response.ok) throw new Error(report.detail || "Evaluation failed");
      renderEvaluation(report);
      const duration = report.duration_ms != null ? ` in ${(report.duration_ms / 1000).toFixed(1)}s` : "";
      $("#eval-message").textContent = report.evaluated === false
        ? report.reason
        : `${report.passed}/${report.total} cases passed against the live graph${duration}.`;
    } catch (error) {
      $("#eval-message").textContent = error.message;
    } finally {
      evalButton.disabled = false;
    }
  });

  function renderEvaluation(report) {
    if (report.evaluated === false) {
      $("#eval-results").innerHTML = `<article class="eval-case"><header><span class="eval-status skipped">SKIPPED</span><code>evaluation</code></header><div class="eval-detail">${escapeHtml(report.reason || "")}</div></article>`;
      return;
    }
    $("#eval-results").innerHTML = report.cases.map(item => {
      const status = item.passed ? "PASS" : "FAIL";
      let detail;
      if (item.kind === "pagerank") {
        detail = `<div class="eval-row"><span class="eval-label">TOP MODULE</span>
            <div>expected · ${escapeHtml(item.expected_top_module || "—")}</div>
            <div>actual · ${escapeHtml(item.actual_top_module || "—")}</div></div>
          <div class="eval-row"><span class="eval-label">RANKING</span>
            ${(item.actual_ranking || []).map(entry => `<div>${escapeHtml(entry.path)} · ${Number(entry.score).toFixed(4)}</div>`).join("") || "<div>—</div>"}
            ${item.scores_descending === false ? '<div class="eval-unexp">scores not in descending order</div>' : ""}</div>`
          + (item.error ? `<div class="eval-detail">${escapeHtml(item.error)}</div>` : "");
      } else if (item.kind === "communities") {
        detail = `<div class="eval-row"><span class="eval-label">BLAST RADIUS</span>
            <div>target community · C${item.target_community ?? "—"}</div>
            <div>communities · ${item.community_count ?? "—"} total, ${item.impacted_communities ?? "—"} touched by impact</div></div>`
          + (item.error ? `<div class="eval-detail">${escapeHtml(item.error)}</div>` : "");
      } else if (item.expected_error) {
        detail = `<div class="eval-detail">Expected rejection · ${escapeHtml(item.error || "")}</div>`;
      } else {
        detail = [["IMPORTERS", item.expected_importers, item.actual_importers, item.missing_importers, item.unexpected_importers],
                  ["TESTS", item.expected_tests, item.actual_tests, item.missing_tests, item.unexpected_tests]]
          .map(([label, expected, actual, missing, unexpected]) => `
            <div class="eval-row"><span class="eval-label">${label}</span>
              <div>expected · ${expected.map(escapeHtml).join(", ") || "—"}</div>
              <div>actual · ${actual.map(escapeHtml).join(", ") || "—"}</div>
              ${missing.length ? `<div class="eval-miss">missing · ${missing.map(escapeHtml).join(", ")}</div>` : ""}
              ${unexpected.length ? `<div class="eval-unexp">unexpected · ${unexpected.map(escapeHtml).join(", ")}</div>` : ""}
            </div>`).join("")
          + `<div class="eval-surface">change surface ${item.change_surface_score}%</div>`;
      }
      return `<article class="eval-case ${item.passed ? "pass" : "fail"}">
        <header><span class="eval-status">${status}</span><code>${escapeHtml(item.target)}</code></header>
        ${detail}</article>`;
    }).join("");
  }

  initialize().catch(error => { $("#query-message").textContent = error.message; });
})();
