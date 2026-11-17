/**
 * BAYMAX OBD — Módulo 4: Agente Mecánico IA, Búsqueda Web y Bitácora de Soluciones
 */

let chatHistory = [];
let currentDtcForFix = null;
let currentHypothesisForFix = "";

const agentMessages = document.getElementById("agentMessages");
const evidenceContainer = document.getElementById("evidenceContainer");
const chatInput = document.getElementById("chatInput");
const btnSendChat = document.getElementById("btnSendChat");
const btnClearChat = document.getElementById("btnClearChat");
const btnToggleContext = document.getElementById("btnToggleContext");
const contextInspector = document.getElementById("contextInspector");
const inputSymptoms = document.getElementById("inputSymptoms");
const btnDiagnoseSymptoms = document.getElementById("btnDiagnoseSymptoms");

// Modal de Solución Verificada
const modalFix = document.getElementById("modalFix");
const fixDtcCode = document.getElementById("fixDtcCode");
const fixText = document.getElementById("fixText");
const fixVerified = document.getElementById("fixVerified");
const btnSubmitFix = document.getElementById("btnSubmitFix");

document.addEventListener("DOMContentLoaded", () => {
  if (btnSendChat) btnSendChat.addEventListener("click", handleSendChat);
  if (btnClearChat) {
    btnClearChat.addEventListener("click", () => {
      agentMessages.innerHTML = "";
      chatHistory = [];
    });
  }

  if (btnToggleContext) {
    btnToggleContext.addEventListener("click", toggleContextPacket);
  }

  if (btnDiagnoseSymptoms) {
    btnDiagnoseSymptoms.addEventListener("click", () => {
      const sym = inputSymptoms.value.trim();
      if (sym) {
        diagnoseSymptoms(sym);
        inputSymptoms.value = "";
      }
    });
  }

  if (inputSymptoms) {
    inputSymptoms.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        btnDiagnoseSymptoms.click();
      }
    });
  }

  if (chatInput) {
    chatInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSendChat();
      }
    });
  }

  if (btnSubmitFix) {
    btnSubmitFix.addEventListener("click", submitFix);
  }
});

function nowStamp() {
  const d = new Date();
  return d.toTimeString().split(" ")[0];
}

async function toggleContextPacket() {
  if (!contextInspector) return;
  const isHidden = contextInspector.style.display === "none";
  contextInspector.style.display = isHidden ? "block" : "none";
  btnToggleContext.classList.toggle("btn-primary", isHidden);
  if (isHidden) {
    try {
      const res = await fetch("/api/agent/context");
      const data = await res.json();
      contextInspector.textContent = data.system_prompt || JSON.stringify(data.context, null, 2);
    } catch (e) {
      contextInspector.textContent = "Error al cargar el contexto activo.";
    }
  }
}

// ====================================================================
// CONSTRUCTORES DE TARJETAS DE CONVERSACIÓN
// ====================================================================
function appendOperatorEntry(label, text) {
  const entry = document.createElement("div");
  entry.className = "msg-card user";
  entry.innerHTML = `
    <div class="msg-header">
      <span>TÚ · ${escapeHtml(label)}</span>
      <span class="mono">${nowStamp()}</span>
    </div>
    <div class="msg-content"><p>${escapeHtml(text)}</p></div>
  `;
  agentMessages.appendChild(entry);
  agentMessages.scrollTop = agentMessages.scrollHeight;
  chatHistory.push({ role: "user", content: text });
}

function createTerminalEntry(roleTitle = "BAYMAX MECÁNICO · DIAGNÓSTICO") {
  const entry = document.createElement("div");
  entry.className = "msg-card";
  entry.innerHTML = `
    <div class="msg-header">
      <span>${escapeHtml(roleTitle)}</span>
      <span class="mono">${nowStamp()}</span>
    </div>
    <div class="msg-content"></div>
  `;
  agentMessages.appendChild(entry);
  agentMessages.scrollTop = agentMessages.scrollHeight;
  return entry.querySelector(".msg-content");
}
window.createTerminalEntry = createTerminalEntry;

function appendSystemEntry(title, htmlContent) {
  const bodyEl = createTerminalEntry(`SISTEMA · ${title}`);
  bodyEl.innerHTML = htmlContent;
  agentMessages.scrollTop = agentMessages.scrollHeight;
  return bodyEl;
}
window.appendSystemEntry = appendSystemEntry;

// ====================================================================
// CONSULTAS Y DIAGNÓSTICOS CON EL AGENTE
// ====================================================================
async function handleSendChat() {
  const text = chatInput.value.trim();
  if (!text) return;
  chatInput.value = "";

  appendOperatorEntry("CONSULTA", text);
  const bodyEl = createTerminalEntry("BAYMAX MECÁNICO · RESPUESTA TÉCNICA");

  try {
    const res = await fetch("/api/agent/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, history: chatHistory.slice(-8) })
    });
    await consumeSse(res, bodyEl);
  } catch (err) {
    bodyEl.textContent = "Error de comunicación con el motor de diagnóstico.";
  }
}

async function diagnoseDtc(code) {
  currentDtcForFix = code;
  appendOperatorEntry("AUDITORÍA DTC", `Diagnosticar código ${code} cruzando Freeze Frame, telemetría en vivo y foros técnicos.`);
  const bodyEl = createTerminalEntry(`BAYMAX MECÁNICO · DICTAMEN DTC ${code}`);

  try {
    const res = await fetch("/api/agent/diagnose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dtc_code: code })
    });
    await consumeSse(res, bodyEl, code);
  } catch (err) {
    bodyEl.textContent = "Error al ejecutar el diagnóstico del código DTC.";
  }
}
window.diagnoseDtc = diagnoseDtc;

async function diagnoseSymptoms(symptoms) {
  appendOperatorEntry("SÍNTOMA REPORTADO", symptoms);
  const bodyEl = createTerminalEntry("BAYMAX MECÁNICO · CORRELACIÓN DE SÍNTOMAS");

  try {
    const res = await fetch("/api/agent/diagnose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symptoms })
    });
    await consumeSse(res, bodyEl);
  } catch (err) {
    bodyEl.textContent = "Error al analizar los síntomas.";
  }
}

// ====================================================================
// DESCARGA 1-CLICK DE MODELO OLLAMA
// ====================================================================
async function triggerModelPull() {
  const bodyEl = createTerminalEntry("OLLAMA · DESCARGA DE MODELO LOCAL (QWEN 2.5)");
  bodyEl.innerHTML = `<p class="mono">Conectando con Ollama para descargar qwen2.5:7b...</p>`;
  try {
    const res = await fetch("/api/ollama/pull", { method: "POST" });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop();
      for (const p of parts) {
        if (!p.startsWith("data: ")) continue;
        const ev = JSON.parse(p.slice(6));
        if (ev.type === "progress") {
          const pctStr = ev.pct != null ? ` (${ev.pct}%)` : "";
          bodyEl.innerHTML = `<p class="mono">Descargando qwen2.5:7b: ${escapeHtml(ev.status || "en progreso")}${pctStr}</p>`;
        } else if (ev.type === "done") {
          bodyEl.innerHTML = `<p style="color:var(--status-ok); font-weight:700;">✓ Modelo listo: ${escapeHtml(ev.model)}</p>`;
        } else if (ev.type === "error") {
          bodyEl.innerHTML = `<p style="color:var(--status-danger);">Error Ollama: ${escapeHtml(ev.message)}</p>`;
        }
      }
    }
  } catch (e) {
    bodyEl.innerHTML = `<p style="color:var(--status-danger);">No se pudo conectar con el servicio local de Ollama.</p>`;
  }
}
window.triggerModelPull = triggerModelPull;

// ====================================================================
// LECTOR DE STREAMING SSE Y PANEL LATERAL DE EVIDENCIA
// ====================================================================
async function consumeSse(response, bodyEl, dtcCode = null) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let fullText = "";

  bodyEl.innerHTML = `<p style="color:var(--text-secondary);">Analizando telemetría, historial y consultando fuentes técnicas...</p>`;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop();

    for (const chunk of chunks) {
      if (!chunk.startsWith("data: ")) continue;
      try {
        const item = JSON.parse(chunk.slice(6));

        if (item.type === "status") {
          const stLine = `<div style="font-size:11.5px; color:var(--accent); margin-bottom:8px; font-weight:600;">⟳ ${escapeHtml(item.message)}</div>`;
          bodyEl.innerHTML = stLine + renderMarkdown(fullText);
        } else if (item.type === "sources") {
          renderEvidenceSidebar(item.sources, item.manuals, item.known);
        } else if (item.type === "token") {
          fullText += item.text;
          bodyEl.innerHTML = renderMarkdown(fullText);
          agentMessages.scrollTop = agentMessages.scrollHeight;
        } else if (item.type === "final") {
          fullText = item.text || fullText;
          currentHypothesisForFix = fullText;
          let footer = "";
          if (dtcCode) {
            footer = `
              <div style="margin-top:12px; padding-top:10px; border-top:1px solid var(--border-subtle); display:flex; justify-content:flex-end;">
                <button class="btn btn-sm btn-success" onclick="openFixModal('${dtcCode}')">
                  ✓ Registrar Solución Verificada para ${dtcCode}
                </button>
              </div>
            `;
          }
          bodyEl.innerHTML = renderMarkdown(fullText) + footer;
          chatHistory.push({ role: "assistant", content: fullText });
          agentMessages.scrollTop = agentMessages.scrollHeight;
        }
      } catch (e) {
        console.error("Error procesando SSE:", e);
      }
    }
  }
}
window.consumeSse = consumeSse;

function renderEvidenceSidebar(sources = [], manuals = [], known = []) {
  if (!evidenceContainer) return;
  if (!sources.length && !manuals.length && !known.length) {
    evidenceContainer.innerHTML = `<div style="color:var(--text-muted); font-size:12.5px;">No se requirieron fuentes externas adicionales para esta consulta.</div>`;
    return;
  }

  let html = "";

  if (known && known.length > 0) {
    known.forEach(k => {
      html += `
        <div class="source-item" style="border-left:3px solid var(--status-ok);">
          <div style="font-weight:700; color:var(--status-ok); font-size:11.5px;">✓ SOLUCIÓN PREVIA EN TU GARAJE</div>
          <div class="source-quote">${escapeHtml(k.fix || k.text || "")}</div>
        </div>
      `;
    });
  }

  if (sources && sources.length > 0) {
    sources.forEach((s, idx) => {
      const passage = (s.passages && s.passages[0]) || s.snippet || "";
      html += `
        <div class="source-item">
          <div>
            <a href="${s.url}" target="_blank" rel="noopener">[${idx + 1}] ${escapeHtml(s.title || s.domain)}</a>
            <div style="font-size:11px; color:var(--text-muted);">${escapeHtml(s.domain || "")}</div>
          </div>
          <div class="source-quote">${escapeHtml(passage)}</div>
        </div>
      `;
    });
  }

  if (manuals && manuals.length > 0) {
    manuals.forEach(m => {
      html += `
        <div class="source-item">
          <div class="mono" style="font-size:11px; font-weight:700; color:var(--accent);">
            📄 ${escapeHtml(m.file)} (Pág. ${m.page})
          </div>
          <div class="source-quote">${escapeHtml(m.text)}</div>
        </div>
      `;
    });
  }

  evidenceContainer.innerHTML = html;
}

// ====================================================================
// MODAL PARA GUARDAR SOLUCIÓN REAL APLICADA EN GARAGE.DB
// ====================================================================
function openFixModal(code) {
  currentDtcForFix = code;
  if (fixDtcCode) fixDtcCode.value = code || "FALLA GENERAL";
  if (fixText) fixText.value = "";
  if (modalFix) modalFix.classList.add("active");
}
window.openFixModal = openFixModal;

function closeFixModal() {
  if (modalFix) modalFix.classList.remove("active");
}
window.closeFixModal = closeFixModal;

async function submitFix() {
  const actualFix = fixText.value.trim();
  if (!actualFix) {
    alert("Por favor escribe qué reparación realizaste.");
    return;
  }
  btnSubmitFix.disabled = true;
  btnSubmitFix.textContent = "Guardando...";

  try {
    const res = await fetch("/api/agent/fix", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dtc_code: currentDtcForFix,
        actual_fix: actualFix,
        ai_hypothesis: currentHypothesisForFix || "Reparación registrada por el usuario",
        verified_working: fixVerified.checked
      })
    });
    const data = await res.json();
    if (data.ok) {
      closeFixModal();
      appendSystemEntry(
        `SOLUCIÓN GUARDADA EN GARAGE.DB (#${data.log_id})`,
        `<p>La reparación para <code>${escapeHtml(currentDtcForFix || "FALLA")}</code> fue guardada en SQLite y vectorizada en ChromaDB: <code>${escapeHtml(actualFix)}</code></p>`
      );
    } else {
      alert("No se pudo guardar la solución.");
    }
  } catch (e) {
    alert("Error de conexión al guardar la solución.");
  } finally {
    btnSubmitFix.disabled = false;
    btnSubmitFix.textContent = "Guardar en garage.db";
  }
}

// ====================================================================
// FORMATEADOR MARKDOWN LIGERO
// ====================================================================
function renderMarkdown(md) {
  if (!md) return "";
  const lines = md.split("\n");
  const out = [];
  let inList = false;

  for (let raw of lines) {
    let line = escapeHtml(raw);
    line = line.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    line = line.replace(/\*(.+?)\*/g, "<em>$1</em>");
    line = line.replace(/`([^`]+)`/g, "<code>$1</code>");

    if (/^###\s+(.*)/.test(line)) {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push(`<h3>${line.replace(/^###\s+/, "")}</h3>`);
    } else if (/^##\s+(.*)/.test(line)) {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push(`<h2>${line.replace(/^##\s+/, "")}</h2>`);
    } else if (/^#\s+(.*)/.test(line)) {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push(`<h1>${line.replace(/^#\s+/, "")}</h1>`);
    } else if (/^(\-|\d+\.)\s+(.*)/.test(line)) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${line.replace(/^(\-|\d+\.)\s+/, "")}</li>`);
    } else if (line.trim() === "") {
      if (inList) { out.push("</ul>"); inList = false; }
    } else {
      if (inList) { out.push("</ul>"); inList = false; }
      out.push(`<p>${line}</p>`);
    }
  }
  if (inList) out.push("</ul>");
  return out.join("\n");
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text || "";
  return div.innerHTML;
}
