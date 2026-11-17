/**
 * BAYMAX OBD — Módulo 5: Laboratorio Performance
 * Dinamómetro Virtual (WHP & Torque) y Validador Pre-Stage de 7 puntos.
 */

let dynoChartInstance = null;
let lastDynoResult = null;

// Referencias DOM Virtual Dyno
const btnStartDyno = document.getElementById("btnStartDyno");
const btnStopDyno = document.getElementById("btnStopDyno");
const dynoGearSelect = document.getElementById("dynoGearSelect");
const dynoRunStatus = document.getElementById("dynoRunStatus");
const dynoPeakHp = document.getElementById("dynoPeakHp");
const dynoPeakHpRpm = document.getElementById("dynoPeakHpRpm");
const dynoPeakTq = document.getElementById("dynoPeakTq");
const dynoPeakTqRpm = document.getElementById("dynoPeakTqRpm");
const dynoCrankHp = document.getElementById("dynoCrankHp");
const dynoPullTime = document.getElementById("dynoPullTime");
const dynoSpeedSpan = document.getElementById("dynoSpeedSpan");

// Referencias DOM Pre-Stage Check
const btnStartPrestage = document.getElementById("btnStartPrestage");
const prestageScore = document.getElementById("prestageScore");
const prestageVerdict = document.getElementById("prestageVerdict");
const prestageSummary = document.getElementById("prestageSummary");
const prestageCheckList = document.getElementById("prestageCheckList");

document.addEventListener("DOMContentLoaded", () => {
  if (btnStartDyno) btnStartDyno.addEventListener("click", startDynoRun);
  if (btnStopDyno) btnStopDyno.addEventListener("click", stopDynoRun);
  if (btnStartPrestage) btnStartPrestage.addEventListener("click", startPrestageRun);
});

// Progreso en vivo desde telemetry_ws.js
window.onRunProgress = function (rec) {
  if (!rec) return;
  if (rec.run_type === "dyno" && dynoRunStatus) {
    dynoRunStatus.textContent = `Grabando Pull: ${rec.samples} muestras (${rec.elapsed_s}s) — Fase: ${(rec.phase || "WOT").toUpperCase()}`;
    dynoRunStatus.style.color = "var(--accent)";
  } else if (rec.run_type === "prestage" && btnStartPrestage) {
    btnStartPrestage.textContent = `Evaluando [${(rec.phase || "RUN").toUpperCase()}] ${rec.elapsed_s}s (${rec.samples} muestras)`;
  }
};

// Fin automático de corrida desde telemetry_ws.js
window.onRunCompleted = function (msg) {
  if (msg.run_type === "dyno") {
    resetDynoButtons();
    renderDynoResult(msg.result, msg.id);
  } else if (msg.run_type === "prestage") {
    resetPrestageButton();
    renderPrestageResult(msg.result, msg.id);
  }
};

// ====================================================================
// DINAMÓMETRO VIRTUAL (WHP & TORQUE)
// ====================================================================
async function startDynoRun() {
  const gear = parseInt(dynoGearSelect.value, 10) || 3;
  btnStartDyno.disabled = true;
  try {
    const res = await fetch("/api/runs/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_type: "dyno", gear })
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      alert("No se pudo iniciar el Dyno: " + (data.detail || "Verifica que la ECU esté conectada"));
      btnStartDyno.disabled = false;
      return;
    }
    btnStartDyno.style.display = "none";
    btnStopDyno.style.display = "inline-flex";
    btnStopDyno.disabled = false;
    btnStopDyno.textContent = "Detener y Calcular";
  } catch (e) {
    alert("Error de red al iniciar grabación de Dyno.");
    btnStartDyno.disabled = false;
  }
}

async function stopDynoRun() {
  btnStopDyno.disabled = true;
  btnStopDyno.textContent = "Calculando...";
  try {
    const res = await fetch("/api/runs/stop", { method: "POST" });
    const data = await res.json();
    if (res.ok && data.ok && data.run) {
      renderDynoResult(data.run.result, data.run.id);
    }
  } catch (e) {
    console.error("Error deteniendo dyno:", e);
  } finally {
    resetDynoButtons();
  }
}

function resetDynoButtons() {
  if (!btnStartDyno || !btnStopDyno) return;
  btnStartDyno.style.display = "inline-flex";
  btnStartDyno.disabled = false;
  btnStopDyno.style.display = "none";
  btnStopDyno.disabled = false;
  if (dynoRunStatus) {
    dynoRunStatus.textContent = "Acelera a fondo de 2,000 RPM al corte en calle plana.";
    dynoRunStatus.style.color = "var(--text-secondary)";
  }
}

function renderDynoResult(res, runId) {
  if (!res || !res.ok) {
    if (dynoRunStatus) {
      dynoRunStatus.textContent = "Error de Dyno: " + ((res && res.error) || "Muestras insuficientes a plena carga");
      dynoRunStatus.style.color = "var(--status-danger)";
    }
    return;
  }

  lastDynoResult = res;

  if (dynoPeakHp) dynoPeakHp.textContent = `${res.peak_whp} WHP`;
  if (dynoPeakHpRpm) dynoPeakHpRpm.textContent = `@ ${res.peak_whp_rpm} RPM (M${res.gear})`;
  if (dynoPeakTq) dynoPeakTq.textContent = `${res.peak_tq_lbft} lb-ft`;
  if (dynoPeakTqRpm) dynoPeakTqRpm.textContent = `@ ${res.peak_tq_rpm} RPM`;
  if (dynoCrankHp) dynoCrankHp.textContent = `${res.est_crank_hp} BHP`;
  if (dynoPullTime) dynoPullTime.textContent = `${res.duration_s} s`;

  const sp = res.speed_range_kph || [0, 0];
  if (dynoSpeedSpan) dynoSpeedSpan.textContent = `${sp[0]}–${sp[1]} km/h`;

  drawDynoChart(res);

  if (window.appendSystemEntry) {
    window.appendSystemEntry(
      `CORRIDA VIRTUAL DYNO #${runId} COMPLETADA`,
      `<p><strong>Potencia a la rueda:</strong> <code>${res.peak_whp} WHP @ ${res.peak_whp_rpm} RPM</code> · <strong>Torque:</strong> <code>${res.peak_tq_lbft} lb-ft @ ${res.peak_tq_rpm} RPM</code> · <strong>Motor Est.:</strong> <code>${res.est_crank_hp} BHP</code></p>
       <div style="margin-top:8px;"><button class="btn btn-sm btn-primary" onclick="analyzeRun(${runId})">Analizar Curva con Agente IA</button></div>`
    );
  }
}

function drawDynoChart(res) {
  const canvas = document.getElementById("dynoChart");
  if (!canvas || !res || !res.curve) return;
  const ctx = canvas.getContext("2d");

  const styles = getComputedStyle(document.documentElement);
  const cTrace1 = styles.getPropertyValue("--trace-1").trim() || "#38bdf8";
  const cTrace2 = styles.getPropertyValue("--trace-2").trim() || "#f59e0b";
  const cGrid = styles.getPropertyValue("--grid-line").trim() || "#202736";
  const cText = styles.getPropertyValue("--text-secondary").trim() || "#94a3b8";

  const labels = res.curve.map(c => c.rpm);
  const hpData = res.curve.map(c => c.whp);
  const tqData = res.curve.map(c => c.tq_lbft);

  if (dynoChartInstance) {
    dynoChartInstance.destroy();
  }

  dynoChartInstance = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Potencia a la Rueda (WHP)",
          data: hpData,
          borderColor: cTrace1,
          backgroundColor: "rgba(56, 189, 248, 0.1)",
          borderWidth: 2.5,
          pointRadius: 3,
          yAxisID: "yHp",
          tension: 0.3,
          fill: true
        },
        {
          label: "Torque (lb-ft)",
          data: tqData,
          borderColor: cTrace2,
          backgroundColor: "rgba(245, 158, 11, 0.06)",
          borderWidth: 2.5,
          pointRadius: 3,
          yAxisID: "yTq",
          tension: 0.3,
          fill: true
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          labels: { color: cText, font: { family: "JetBrains Mono", size: 11 } }
        }
      },
      scales: {
        x: {
          grid: { color: cGrid },
          ticks: { color: cText, font: { family: "JetBrains Mono", size: 10.5 } },
          title: { display: true, text: "RÉGIMEN DE MOTOR (RPM)", color: cText, font: { family: "JetBrains Mono", size: 10.5 } }
        },
        yHp: {
          type: "linear",
          position: "left",
          grid: { color: cGrid },
          ticks: { color: cTrace1, font: { family: "JetBrains Mono", size: 10.5 } },
          title: { display: true, text: "WHP", color: cTrace1, font: { family: "JetBrains Mono", size: 10.5 } }
        },
        yTq: {
          type: "linear",
          position: "right",
          grid: { drawOnChartArea: false },
          ticks: { color: cTrace2, font: { family: "JetBrains Mono", size: 10.5 } },
          title: { display: true, text: "LB-FT", color: cTrace2, font: { family: "JetBrains Mono", size: 10.5 } }
        }
      }
    }
  });
}

window.refreshDynoChartTheme = function () {
  if (lastDynoResult) {
    drawDynoChart(lastDynoResult);
  }
};

// ====================================================================
// VALIDADOR PRE-STAGE (SALUD DE 7 PUNTOS ANTES DE REPROGRAMAR)
// ====================================================================
async function startPrestageRun() {
  btnStartPrestage.disabled = true;
  btnStartPrestage.textContent = "Iniciando Secuencia...";
  if (prestageSummary) {
    prestageSummary.textContent = "Ejecutando secuencia de estrés: grabando ralentí, crucero y plena carga (WOT) para auditar Fuel Trims, avance de encendido y temperaturas...";
  }

  try {
    const res = await fetch("/api/runs/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_type: "prestage" })
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      alert("Error al iniciar Pre-Stage: " + (data.detail || "Verifica conexión ECU"));
      resetPrestageButton();
    }
  } catch (e) {
    alert("Error de red al iniciar prueba Pre-Stage.");
    resetPrestageButton();
  }
}

function resetPrestageButton() {
  if (!btnStartPrestage) return;
  btnStartPrestage.disabled = false;
  btnStartPrestage.textContent = "Iniciar Prueba de Estrés";
}

function renderPrestageResult(res, runId) {
  if (!res) return;
  if (prestageScore) prestageScore.textContent = `${res.score} / 100`;
  if (prestageVerdict) prestageVerdict.textContent = res.verdict;

  const col = res.verdict === "GO"
    ? "var(--status-ok)"
    : res.verdict === "PRECAUCIÓN"
      ? "var(--status-warn)"
      : "var(--status-danger)";

  if (prestageVerdict) prestageVerdict.style.color = col;
  if (prestageScore) prestageScore.style.color = col;
  if (prestageSummary) prestageSummary.textContent = `${res.summary}`;

  if (prestageCheckList) {
    prestageCheckList.innerHTML = "";
    (res.checks || []).forEach(c => {
      const card = document.createElement("div");
      card.className = `check-item-card ${c.status}`;
      const icon = c.status === "PASS" ? "✓" : c.status === "WARN" ? "⚠️" : "✕";
      card.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; font-weight:700;">
          <span>${icon} ${c.name}</span>
          <span class="mono" style="font-size:13px;">${c.value}</span>
        </div>
        <div style="font-size:12px; color:var(--text-secondary); margin-top:4px;">${c.detail}</div>
        ${c.fix ? `<div style="font-size:11.5px; color:var(--accent); margin-top:6px; font-weight:600;">Acción: ${c.fix}</div>` : ""}
      `;
      prestageCheckList.appendChild(card);
    });
  }

  if (window.appendSystemEntry) {
    window.appendSystemEntry(
      `AUDITORÍA PRE-STAGE #${runId} — VEREDICTO: ${res.verdict} (${res.score}/100)`,
      `<p>${res.summary}</p>
       <div style="margin-top:8px;"><button class="btn btn-sm btn-primary" onclick="analyzeRun(${runId})">Generar Dictamen Stage 1/2 con IA</button></div>`
    );
  }
}

async function analyzeRun(runId) {
  if (window.switchModule) window.switchModule("modAssistant");
  if (window.createTerminalEntry) {
    const bodyEl = window.createTerminalEntry(`BAYMAX IA — AUDITORÍA DE CORRIDA #${runId}`);
    try {
      const res = await fetch(`/api/runs/analyze?run_id=${runId}`, { method: "POST" });
      await window.consumeSse(res, bodyEl);
    } catch (e) {
      bodyEl.textContent = "Error al analizar la corrida con el agente.";
    }
  }
}
window.analyzeRun = analyzeRun;
