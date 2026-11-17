/**
 * BAYMAX OBD — Gestor Modular, Selector de Modos de Vista, Gauges SVG y Osciloscopio
 */

let ws = null;
let reconnectTimer = null;
let allVehicles = [];
let currentVehicle = null;
let isConnected = false;
let peakRpmValue = 0;
let scopeHold = false;
let hoverX = null;
let activeDtcsList = [];
let selectedDtcIndex = 0;

// Registro de mínimos y máximos para la matriz de 12 sensores
const sensorStats = {};

const SENSOR_DEFINITIONS = [
  { key: "rpm", label: "Régimen Motor", pid: "PID 0C", unit: "RPM", dec: 0 },
  { key: "speed_kph", label: "Velocidad Vehículo", pid: "PID 0D", unit: "km/h", dec: 0 },
  { key: "coolant_temp_c", label: "Temp. Refrigerante", pid: "PID 05", unit: "°C", dec: 1 },
  { key: "intake_temp_c", label: "Temp. Admisión (IAT)", pid: "PID 0F", unit: "°C", dec: 1 },
  { key: "stft_b1", label: "Fuel Trim Corto (STFT)", pid: "PID 06", unit: "%", dec: 1, signed: true },
  { key: "ltft_b1", label: "Fuel Trim Largo (LTFT)", pid: "PID 07", unit: "%", dec: 1, signed: true },
  { key: "map_kpa", label: "Presión Múltiple (MAP)", pid: "PID 0B", unit: "kPa", dec: 1 },
  { key: "maf_gs", label: "Flujo de Aire (MAF)", pid: "PID 10", unit: "g/s", dec: 2 },
  { key: "throttle_pos", label: "Posición Mariposa (TPS)", pid: "PID 11", unit: "%", dec: 1 },
  { key: "engine_load", label: "Carga Calculada", pid: "PID 04", unit: "%", dec: 1 },
  { key: "timing_advance", label: "Avance de Chispa", pid: "PID 0E", unit: "°", dec: 1 },
  { key: "control_module_voltage", label: "Voltaje ECU / Alt", pid: "PID 42", unit: "V", dec: 2 }
];

const SCOPE_BUFFER_SIZE = 200;
const scopeData = {
  time: [],
  stft: [],
  ltft: [],
  rpm: [],
  map: [],
  timing: [],
  iat: [],
  maf: []
};

// Referencias DOM principales
const appShell = document.getElementById("appShell");
const activeModuleTitle = document.getElementById("activeModuleTitle");
const vehicleSelect = document.getElementById("vehicleSelect");
const portSelect = document.getElementById("portSelect");
const btnRescanPorts = document.getElementById("btnRescanPorts");
const btnConnect = document.getElementById("btnConnect");
const pillObd = document.getElementById("pillObd");
const pillInternet = document.getElementById("pillInternet");
const pillLlm = document.getElementById("pillLlm");
const btnPullModel = document.getElementById("btnPullModel");
const mockBar = document.getElementById("mockBar");
const mockModeLabel = document.getElementById("mockModeLabel");
const navDtcBadge = document.getElementById("navDtcBadge");

// Gauges Cabina
const valRpm = document.getElementById("valRpm");
const arcRpm = document.getElementById("arcRpm");
const peakRpmEl = document.getElementById("peakRpm");
const redlineRpmEl = document.getElementById("redlineRpm");

const valSpeed = document.getElementById("valSpeed");
const arcSpeed = document.getElementById("arcSpeed");
const subMaf = document.getElementById("subMaf");

const cardCoolant = document.getElementById("cardCoolant");
const valCoolant = document.getElementById("valCoolant");
const arcCoolant = document.getElementById("arcCoolant");
const subIat = document.getElementById("subIat");

const valMap = document.getElementById("valMap");
const arcMap = document.getElementById("arcMap");
const subBoost = document.getElementById("subBoost");

const valLoad = document.getElementById("valLoad");
const subTps = document.getElementById("subTps");
const barLoad = document.getElementById("barLoad");

const valVoltage = document.getElementById("valVoltage");
const barVoltage = document.getElementById("barVoltage");

const valStft = document.getElementById("valStft");
const barStft = document.getElementById("barStft");
const valLtft = document.getElementById("valLtft");
const barLtft = document.getElementById("barLtft");

const cockpitHealthTitle = document.getElementById("cockpitHealthTitle");
const cockpitHealthSub = document.getElementById("cockpitHealthSub");

// Osciloscopio
const canvas = document.getElementById("scopeCanvas");
const ctx = canvas.getContext("2d");
const scopeModeSelect = document.getElementById("scopeMode");
const scopeLegend = document.getElementById("scopeLegend");
const btnScopeHold = document.getElementById("btnScopeHold");
const btnScopeClear = document.getElementById("btnScopeClear");
const sensorMatrixGrid = document.getElementById("sensorMatrixGrid");

// DTCs
const dtcList = document.getElementById("dtcList");
const ffGrid = document.getElementById("ffGrid");
const ffPanelTitle = document.getElementById("ffPanelTitle");
const ffActions = document.getElementById("ffActions");

// Contexto Asistente
const ctxVehicle = document.getElementById("ctxVehicle");
const ctxTrims = document.getElementById("ctxTrims");
const ctxDtcs = document.getElementById("ctxDtcs");

document.addEventListener("DOMContentLoaded", async () => {
  initThemeSwitcher();
  initModuleNavigation();
  initScopeControls();
  initSensorMatrixDOM();
  initGarageModule();
  resizeCanvas();
  window.addEventListener("resize", resizeCanvas);

  await loadVehicles();
  await loadPorts();
  initWebSocket();

  btnConnect.addEventListener("click", toggleConnection);
  btnRescanPorts.addEventListener("click", loadPorts);
  vehicleSelect.addEventListener("change", () => selectVehicleById(parseInt(vehicleSelect.value, 10)));
  document.getElementById("btnRefreshDtcs").addEventListener("click", () => fetch("/api/dtcs/refresh", { method: "POST" }));
  document.getElementById("btnClearDtcs").addEventListener("click", clearDtcs);

  if (btnPullModel) {
    btnPullModel.addEventListener("click", () => {
      switchModule("modAssistant");
      if (window.triggerModelPull) window.triggerModelPull();
    });
  }

  requestAnimationFrame(renderScopeLoop);
});

// ==================================================================== 1. Modos de Vista (Temas)
function initThemeSwitcher() {
  const saved = localStorage.getItem("baymax_theme") || localStorage.getItem("baymax-theme") || "dark";
  applyTheme(saved);

  document.querySelectorAll("[data-set-theme]").forEach(btn => {
    btn.addEventListener("click", () => {
      applyTheme(btn.getAttribute("data-set-theme"));
    });
  });

  const btnCollapse = document.getElementById("btnCollapseSidebar");
  if (btnCollapse) {
    btnCollapse.addEventListener("click", () => {
      appShell.classList.toggle("sidebar-collapsed");
      setTimeout(resizeCanvas, 220);
    });
  }

  const btnMobileMenu = document.getElementById("btnMobileMenu");
  const sidebarBackdrop = document.getElementById("sidebarBackdrop");
  const appSidebar = document.getElementById("appSidebar");

  if (btnMobileMenu && appSidebar && sidebarBackdrop) {
    btnMobileMenu.addEventListener("click", () => {
      appSidebar.classList.toggle("mobile-open");
      sidebarBackdrop.classList.toggle("active");
    });
    sidebarBackdrop.addEventListener("click", () => {
      appSidebar.classList.remove("mobile-open");
      sidebarBackdrop.classList.remove("active");
    });
  }

  const btnHudMode = document.getElementById("btnHudMode");
  if (btnHudMode) {
    btnHudMode.addEventListener("click", () => {
      switchModule("modCockpit");
      const active = document.body.classList.toggle("hud-mirror-mode");
      btnHudMode.classList.toggle("btn-primary", active);
      btnHudMode.textContent = active ? "Salir HUD" : "HUD Parabrisas";
    });
  }
}

function applyTheme(themeName) {
  document.documentElement.setAttribute("data-theme", themeName);
  const metaScheme = document.querySelector('meta[name="color-scheme"]');
  if (metaScheme) {
    metaScheme.content = themeName === "light" ? "light" : "dark";
  }
  try {
    localStorage.setItem("baymax_theme", themeName);
    localStorage.setItem("baymax-theme", themeName);
  } catch (e) {}
  document.querySelectorAll("[data-set-theme]").forEach(btn => {
    btn.classList.toggle("active", btn.getAttribute("data-set-theme") === themeName);
  });
  if (window.refreshDynoChartTheme) {
    window.refreshDynoChartTheme();
  }
}

// ==================================================================== 2. Navegación por Módulos
function initModuleNavigation() {
  document.querySelectorAll(".nav-btn[data-module]").forEach(btn => {
    btn.addEventListener("click", () => {
      switchModule(btn.getAttribute("data-module"));
    });
  });

  // Sub-pestañas en módulo Performance
  document.querySelectorAll("[data-perf-tab]").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-perf-tab]").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const target = btn.getAttribute("data-perf-tab");
      document.getElementById("perfDyno").style.display = target === "perfDyno" ? "flex" : "none";
      document.getElementById("perfPrestage").style.display = target === "perfPrestage" ? "flex" : "none";
    });
  });
}

function switchModule(moduleId) {
  document.querySelectorAll(".nav-btn[data-module]").forEach(btn => {
    const isTarget = btn.getAttribute("data-module") === moduleId;
    btn.classList.toggle("active", isTarget);
    if (isTarget) {
      activeModuleTitle.textContent = btn.getAttribute("data-title") || "Dashboard";
    }
  });

  document.querySelectorAll(".module-view").forEach(sec => {
    sec.classList.toggle("active", sec.id === moduleId);
  });

  // Cerrar menú lateral móvil al elegir un módulo
  const appSidebar = document.getElementById("appSidebar");
  const sidebarBackdrop = document.getElementById("sidebarBackdrop");
  if (appSidebar) appSidebar.classList.remove("mobile-open");
  if (sidebarBackdrop) sidebarBackdrop.classList.remove("active");

  if (moduleId === "modScope") {
    setTimeout(resizeCanvas, 40);
  } else if (moduleId === "modGarage" && currentVehicle) {
    loadVehicleHistory(currentVehicle.id);
  }
}
window.switchModule = switchModule;

// ==================================================================== 3. Flota y Conexión
async function loadVehicles() {
  try {
    const res = await fetch("/api/vehicles");
    allVehicles = await res.json();
    vehicleSelect.innerHTML = "";
    allVehicles.forEach(v => {
      const opt = document.createElement("option");
      opt.value = v.id;
      opt.textContent = `${v.year} ${v.make} ${v.model} (${v.alias})`;
      vehicleSelect.appendChild(opt);
    });
    if (allVehicles.length > 0 && !currentVehicle) {
      updateVehicleState(allVehicles[0]);
    }
    renderGarageFleet();
  } catch (err) {
    console.error("Error cargando vehículos:", err);
  }
}

async function selectVehicleById(vId) {
  if (!vId) return;
  peakRpmValue = 0;
  for (const k in sensorStats) delete sensorStats[k];
  try {
    const res = await fetch(`/api/vehicles/${vId}/select`, { method: "POST" });
    const data = await res.json();
    if (data.ok && data.vehicle) {
      updateVehicleState(data.vehicle);
      loadVehicleHistory(vId);
    }
  } catch (err) {
    console.error("Error cambiando vehículo:", err);
  }
}
window.selectVehicleById = selectVehicleById;

async function loadPorts() {
  try {
    const res = await fetch("/api/ports");
    const ports = await res.json();
    const prev = portSelect.value;
    portSelect.innerHTML = "";
    ports.forEach(p => {
      const opt = document.createElement("option");
      opt.value = p.device;
      opt.textContent = p.device === "MOCK"
        ? "Simulador Físico (Sin Cable)"
        : `${p.device} — ${p.description || "Puerto Serial"}`;
      portSelect.appendChild(opt);
    });
    if (prev && Array.from(portSelect.options).some(o => o.value === prev)) {
      portSelect.value = prev;
    }
  } catch (err) {
    console.error("Error listando puertos:", err);
  }
}

async function toggleConnection() {
  if (isConnected) {
    await fetch("/api/disconnect", { method: "POST" });
  } else {
    const port = portSelect.value || "MOCK";
    const vId = parseInt(vehicleSelect.value, 10);
    btnConnect.textContent = "Conectando...";
    btnConnect.disabled = true;
    try {
      const res = await fetch("/api/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ port, vehicle_id: vId })
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        alert("No se pudo conectar: " + (data.detail || "Verifica el puerto"));
      }
    } catch (err) {
      alert("Error de red al conectar con el backend");
    } finally {
      btnConnect.disabled = false;
      btnConnect.textContent = isConnected ? "Desconectar" : "Conectar";
    }
  }
}

// ==================================================================== 4. WebSocket
function initWebSocket() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

  ws.onmessage = (ev) => {
    try {
      handleWsMessage(JSON.parse(ev.data));
    } catch (e) {
      console.error("WS error:", e);
    }
  };

  ws.onclose = () => {
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(initWebSocket, 1500);
  };
}

function handleWsMessage(msg) {
  if (msg.type === "hello" || msg.type === "init") {
    if (msg.vehicle) updateVehicleState(msg.vehicle);
    if (msg.connection) updateConnectionState(msg.connection);
    if (msg.status) updateExternalStatus(msg.status);
    if (msg.dtcs) renderDtcs(msg.dtcs, msg.freeze_frame);
  } else if (msg.type === "telemetry") {
    const frame = msg.frame || msg.data;
    if (frame) updateTelemetry(frame);
    if (msg.recording && window.onRunProgress) {
      window.onRunProgress(msg.recording);
    }
  } else if (msg.type === "connection") {
    updateConnectionState(msg.connection);
    if (msg.vehicle) updateVehicleState(msg.vehicle);
  } else if (msg.type === "status") {
    if (msg.status) updateExternalStatus(msg.status);
    if (msg.connection) updateConnectionState(msg.connection);
  } else if (msg.type === "dtcs") {
    renderDtcs(msg.dtcs, msg.freeze_frame);
  } else if (msg.type === "vehicle") {
    updateVehicleState(msg.vehicle);
  } else if (msg.type === "run_result") {
    if (window.onRunCompleted) window.onRunCompleted(msg);
  }
}

function updateConnectionState(conn) {
  isConnected = Boolean(conn && conn.connected);
  btnConnect.textContent = isConnected ? "Desconectar" : "Conectar";
  btnConnect.className = isConnected ? "btn btn-danger btn-sm" : "btn btn-primary btn-sm";

  if (isConnected) {
    const info = conn.info || {};
    const baud = info.baud || "500kbps";
    const name = info.kind === "mock" ? "Simulador" : (info.port || "OBD");
    pillObd.className = "status-chip online";
    pillObd.querySelector(".text").textContent = `${name} (${baud})`;
    mockBar.style.display = info.kind === "mock" ? "flex" : "none";
  } else {
    pillObd.className = "status-chip";
    pillObd.querySelector(".text").textContent = "Desconectado";
    mockBar.style.display = "none";
  }
}

function updateVehicleState(v) {
  if (!v) return;
  currentVehicle = v;
  if (vehicleSelect.value !== String(v.id)) {
    vehicleSelect.value = v.id;
  }
  redlineRpmEl.textContent = v.redline_rpm || 6200;
  ctxVehicle.textContent = `${v.year} ${v.make} ${v.model} [${v.engine_code || ""}]`;
  renderGarageFleet();
}

function updateExternalStatus(st) {
  if (st.internet) {
    pillInternet.className = "status-chip online";
    pillInternet.querySelector(".text").textContent = "Web Search: Activo";
  } else {
    pillInternet.className = "status-chip warn";
    pillInternet.querySelector(".text").textContent = "Web Search: Offline";
  }

  const ol = st.ollama || {};
  if (ol.online && ol.model) {
    pillLlm.className = "status-chip online";
    pillLlm.querySelector(".text").textContent = `IA: ${ol.model}`;
    if (btnPullModel) btnPullModel.style.display = "none";
  } else {
    pillLlm.className = "status-chip warn";
    pillLlm.querySelector(".text").textContent = "IA: Motor Técnico Local";
    if (btnPullModel) btnPullModel.style.display = "inline-flex";
  }
}

// ==================================================================== 5. Telemetría en Vivo (Gauges + Matriz)
function setArcPct(arcEl, pct, color) {
  if (!arcEl) return;
  const clamped = Math.max(0, Math.min(100, pct));
  arcEl.style.strokeDashoffset = String(100 - clamped);
  if (color) arcEl.style.stroke = color;
}

function updateTelemetry(f) {
  const redline = (currentVehicle && currentVehicle.redline_rpm) || 6200;

  // RPM
  if (f.rpm != null) {
    const rpm = Math.round(f.rpm);
    valRpm.textContent = rpm;
    if (rpm > peakRpmValue) peakRpmValue = rpm;
    peakRpmEl.textContent = peakRpmValue;
    const pct = (rpm / (redline * 1.08)) * 100;
    const col = rpm >= redline * 0.92 ? "var(--status-danger)" : rpm >= redline * 0.78 ? "var(--status-warn)" : "var(--accent)";
    setArcPct(arcRpm, pct, col);
  }

  // Velocidad & MAF
  if (f.speed_kph != null) {
    const sp = Math.round(f.speed_kph);
    valSpeed.textContent = sp;
    setArcPct(arcSpeed, (sp / 220) * 100, "var(--accent)");
  }
  if (f.maf_gs != null) {
    subMaf.textContent = `${f.maf_gs.toFixed(1)} g/s`;
  }

  // Refrigerante & IAT (Alerta si > 102 °C)
  if (f.coolant_temp_c != null) {
    const ect = f.coolant_temp_c;
    valCoolant.textContent = ect.toFixed(0);
    const pct = ((ect - 40) / 85) * 100;
    const isOver = ect > 102.0;
    cardCoolant.classList.toggle("overheat-alert", isOver);
    setArcPct(arcCoolant, pct, isOver ? "var(--status-danger)" : ect > 96 ? "var(--status-warn)" : "var(--status-ok)");
  }
  if (f.intake_temp_c != null) {
    subIat.textContent = `${f.intake_temp_c.toFixed(0)} °C`;
  }

  // MAP / Boost
  if (f.map_kpa != null) {
    valMap.textContent = f.map_kpa.toFixed(0);
    const psi = (f.map_kpa - 101.3) * 0.145038;
    subBoost.textContent = `${psi >= 0 ? "+" : ""}${psi.toFixed(1)} PSI`;
    setArcPct(arcMap, (f.map_kpa / 220) * 100, psi > 1.5 ? "var(--status-warn)" : "var(--accent)");
  }

  // Secundarios
  if (f.engine_load != null) {
    valLoad.textContent = f.engine_load.toFixed(0);
    barLoad.style.width = `${Math.min(100, f.engine_load)}%`;
  }
  if (f.throttle_pos != null) {
    subTps.textContent = `TPS ${f.throttle_pos.toFixed(0)}%`;
  }
  if (f.control_module_voltage != null) {
    valVoltage.textContent = f.control_module_voltage.toFixed(2);
    const vPct = Math.max(0, Math.min(100, ((f.control_module_voltage - 11.5) / 3.5) * 100));
    barVoltage.style.width = `${vPct}%`;
  }

  const stft = f.stft_b1 || 0;
  const ltft = f.ltft_b1 || 0;
  valStft.textContent = `${stft >= 0 ? "+" : ""}${stft.toFixed(1)}%`;
  barStft.style.width = `${Math.max(0, Math.min(100, 50 + stft * 2))}%`;
  barStft.style.background = Math.abs(stft) > 10 ? "var(--status-warn)" : "var(--accent)";

  valLtft.textContent = `${ltft >= 0 ? "+" : ""}${ltft.toFixed(1)}%`;
  barLtft.style.width = `${Math.max(0, Math.min(100, 50 + ltft * 2))}%`;
  barLtft.style.background = Math.abs(ltft) > 12 ? "var(--status-danger)" : "var(--accent)";

  // Etiqueta del simulador
  const sim = f.sim || {};
  const modeStr = sim.mode || f._mode;
  if (modeStr) {
    const g = sim.gear || f._gear || 1;
    mockModeLabel.textContent = `SIMULADOR: ${modeStr.toUpperCase()} (${g}ª MARCHA)`;
  }

  // Contexto en Asistente
  ctxTrims.textContent = `STFT ${stft >= 0 ? "+" : ""}${stft.toFixed(1)}% | LTFT ${ltft >= 0 ? "+" : ""}${ltft.toFixed(1)}%`;

  // Actualizar matriz de 12 sensores (Módulo 2)
  updateSensorMatrixValues(f);

  // Buffer del Osciloscopio
  if (!scopeHold) {
    scopeData.time.push(f.t != null ? f.t : Date.now() / 1000);
    scopeData.stft.push(stft);
    scopeData.ltft.push(ltft);
    scopeData.rpm.push(f.rpm || 0);
    scopeData.map.push(f.map_kpa || 0);
    scopeData.timing.push(f.timing_advance || 0);
    scopeData.iat.push(f.intake_temp_c || 0);
    scopeData.maf.push(f.maf_gs || 0);

    if (scopeData.time.length > SCOPE_BUFFER_SIZE) {
      for (const k in scopeData) scopeData[k].shift();
    }
  }
}

function initSensorMatrixDOM() {
  if (!sensorMatrixGrid) return;
  sensorMatrixGrid.innerHTML = "";
  SENSOR_DEFINITIONS.forEach(def => {
    const tile = document.createElement("div");
    tile.className = "sensor-tile";
    tile.innerHTML = `
      <div class="sensor-tile-top">
        <span>${def.label}</span>
        <span class="mono">${def.pid}</span>
      </div>
      <div class="sensor-tile-val mono" id="sval_${def.key}">--</div>
      <div class="sensor-tile-minmax mono">
        <span id="smin_${def.key}">Mín: --</span>
        <span id="smax_${def.key}">Máx: --</span>
      </div>
    `;
    sensorMatrixGrid.appendChild(tile);
  });
}

function updateSensorMatrixValues(f) {
  SENSOR_DEFINITIONS.forEach(def => {
    const v = f[def.key];
    if (v == null) return;
    if (!sensorStats[def.key]) {
      sensorStats[def.key] = { min: v, max: v };
    } else {
      if (v < sensorStats[def.key].min) sensorStats[def.key].min = v;
      if (v > sensorStats[def.key].max) sensorStats[def.key].max = v;
    }
    const fmt = (num) => {
      const s = num.toFixed(def.dec);
      return def.signed && num > 0 ? `+${s}` : s;
    };
    const valEl = document.getElementById(`sval_${def.key}`);
    const minEl = document.getElementById(`smin_${def.key}`);
    const maxEl = document.getElementById(`smax_${def.key}`);
    if (valEl) valEl.textContent = `${fmt(v)} ${def.unit}`;
    if (minEl) minEl.textContent = `Mín: ${fmt(sensorStats[def.key].min)}`;
    if (maxEl) maxEl.textContent = `Máx: ${fmt(sensorStats[def.key].max)}`;
  });
}

// ==================================================================== 6. Osciloscopio (Módulo 2)
function initScopeControls() {
  btnScopeHold.addEventListener("click", () => {
    scopeHold = !scopeHold;
    btnScopeHold.textContent = scopeHold ? "Reanudar Señal" : "Pausar Señal";
    btnScopeHold.className = scopeHold ? "btn btn-sm btn-primary" : "btn btn-sm";
  });

  btnScopeClear.addEventListener("click", () => {
    for (const k in scopeData) scopeData[k] = [];
  });

  canvas.addEventListener("mousemove", (e) => {
    const rect = canvas.getBoundingClientRect();
    hoverX = (e.clientX - rect.left) * window.devicePixelRatio;
  });

  canvas.addEventListener("mouseleave", () => {
    hoverX = null;
  });

  canvas.addEventListener("touchmove", (e) => {
    if (!e.touches || !e.touches[0]) return;
    const rect = canvas.getBoundingClientRect();
    hoverX = (e.touches[0].clientX - rect.left) * window.devicePixelRatio;
  }, { passive: true });

  canvas.addEventListener("touchend", () => {
    hoverX = null;
  });

  if (window.ResizeObserver && canvas.parentElement) {
    new ResizeObserver(() => resizeCanvas()).observe(canvas.parentElement);
  }
}

function resizeCanvas() {
  if (!canvas || !canvas.parentElement) return;
  const rect = canvas.parentElement.getBoundingClientRect();
  if (rect.width > 0 && rect.height > 0) {
    canvas.width = Math.floor(rect.width * window.devicePixelRatio);
    canvas.height = Math.floor(rect.height * window.devicePixelRatio);
  }
}

function renderScopeLoop() {
  if (document.getElementById("modScope").classList.contains("active")) {
    renderScope();
  }
  requestAnimationFrame(renderScopeLoop);
}

function renderScope() {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.width;
  const h = canvas.height;
  if (w < 20 || h < 20) return;

  const styles = getComputedStyle(document.documentElement);
  const gridCol = styles.getPropertyValue("--grid-line").trim() || "#202736";
  const c1 = styles.getPropertyValue("--trace-1").trim() || "#38bdf8";
  const c2 = styles.getPropertyValue("--trace-2").trim() || "#f59e0b";
  const txtCol = styles.getPropertyValue("--text-muted").trim() || "#64748b";

  ctx.clearRect(0, 0, w, h);

  const padL = 46 * dpr;
  const padR = 46 * dpr;
  const padT = 16 * dpr;
  const padB = 22 * dpr;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  ctx.strokeStyle = gridCol;
  ctx.lineWidth = 1;
  const rows = 6;
  const cols = 10;
  for (let r = 0; r <= rows; r++) {
    const y = padT + (plotH / rows) * r;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke();
  }
  for (let c = 0; c <= cols; c++) {
    const x = padL + (plotW / cols) * c;
    ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, padT + plotH); ctx.stroke();
  }

  const mode = scopeModeSelect.value;
  let s1, s2, min1, max1, min2, max2, u1, u2, n1, n2;

  if (mode === "trims") {
    s1 = scopeData.stft; min1 = -25; max1 = 25; u1 = "%"; n1 = "STFT";
    s2 = scopeData.ltft; min2 = -25; max2 = 25; u2 = "%"; n2 = "LTFT";
  } else if (mode === "rpm_map") {
    s1 = scopeData.rpm; min1 = 0; max1 = 7000; u1 = "RPM"; n1 = "RPM";
    s2 = scopeData.map; min2 = 0; max2 = 240; u2 = "kPa"; n2 = "MAP";
  } else {
    s1 = scopeData.timing; min1 = -10; max1 = 45; u1 = "°"; n1 = "Avance";
    s2 = scopeData.iat; min2 = 0; max2 = 90; u2 = "°C"; n2 = "IAT";
  }

  ctx.font = `${10 * dpr}px "JetBrains Mono", monospace`;
  for (let r = 0; r <= rows; r += 2) {
    const y = padT + (plotH / rows) * r + 3 * dpr;
    const v1 = max1 - ((max1 - min1) / rows) * r;
    const v2 = max2 - ((max2 - min2) / rows) * r;
    ctx.fillStyle = c1; ctx.textAlign = "right";
    ctx.fillText(`${Math.round(v1)}${u1}`, padL - 6 * dpr, y);
    ctx.fillStyle = c2; ctx.textAlign = "left";
    ctx.fillText(`${Math.round(v2)}${u2}`, padL + plotW + 6 * dpr, y);
  }

  ctx.fillStyle = txtCol;
  ctx.textAlign = "center";
  for (let c = 0; c <= cols; c += 2) {
    const x = padL + (plotW / cols) * c;
    ctx.fillText(`-${Math.round(((cols - c) / cols) * 20)}s`, x, h - 5 * dpr);
  }

  const count = scopeData.time.length;
  if (count < 2) return;

  drawTrace(s1, min1, max1, c1, 2.2 * dpr, padL, padT, plotW, plotH);
  drawTrace(s2, min2, max2, c2, 2.2 * dpr, padL, padT, plotW, plotH);

  // Cursor interactivo
  if (hoverX != null && hoverX >= padL && hoverX <= padL + plotW) {
    const step = plotW / (SCOPE_BUFFER_SIZE - 1);
    const startX = padL + plotW - (count - 1) * step;
    const idx = Math.round((hoverX - startX) / step);
    if (idx >= 0 && idx < count) {
      const cx = startX + idx * step;
      ctx.strokeStyle = txtCol;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(cx, padT); ctx.lineTo(cx, padT + plotH); ctx.stroke();

      scopeLegend.textContent = `Cursor: ${n1}: ${(s1[idx] || 0).toFixed(1)} ${u1}  |  ${n2}: ${(s2[idx] || 0).toFixed(1)} ${u2}`;
    }
  }
}

function drawTrace(values, minVal, maxVal, color, lineWidth, padL, padT, plotW, plotH) {
  const count = values.length;
  const step = plotW / (SCOPE_BUFFER_SIZE - 1);
  const startX = padL + plotW - (count - 1) * step;

  ctx.strokeStyle = color;
  ctx.lineWidth = lineWidth;
  ctx.beginPath();
  for (let i = 0; i < count; i++) {
    const x = startX + i * step;
    const clamped = Math.max(minVal, Math.min(maxVal, values[i] || 0));
    const y = padT + plotH - ((clamped - minVal) / (maxVal - minVal)) * plotH;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
}

// ==================================================================== 7. Escáner de Fallas DTCs (Módulo 3)
function renderDtcs(dtcs, globalFreezeFrame) {
  activeDtcsList = (dtcs || []).map(d => ({
    ...d,
    freeze_frame: d.freeze_frame || globalFreezeFrame || {}
  }));

  navDtcBadge.textContent = String(activeDtcsList.length);
  navDtcBadge.className = activeDtcsList.length > 0 ? "nav-badge" : "nav-badge ok";

  if (activeDtcsList.length === 0) {
    ctxDtcs.textContent = "Ninguno";
    cockpitHealthTitle.textContent = "Motor sin Códigos de Falla Activos";
    cockpitHealthSub.textContent = "Todos los monitores reportan operación normal en tiempo real.";

    dtcList.innerHTML = `
      <div class="card" style="text-align:center; color:var(--text-secondary);">
        No se detectaron códigos de falla activos ni pendientes en la ECU.
      </div>
    `;
    ffPanelTitle.textContent = "Sin Freeze Frame capturado";
    ffGrid.innerHTML = `<div style="grid-column:1/-1; color:var(--text-muted); font-size:12.5px;">La memoria Freeze Frame está vacía porque no hay códigos registrados.</div>`;
    ffActions.innerHTML = "";
    return;
  }

  const codesStr = activeDtcsList.map(d => d.code).join(", ");
  ctxDtcs.textContent = codesStr;
  cockpitHealthTitle.textContent = `Atención: ${activeDtcsList.length} código(s) detectado(s) (${codesStr})`;
  cockpitHealthSub.textContent = "Abre el módulo Códigos DTC o el Agente Mecánico para analizar el Freeze Frame.";

  if (selectedDtcIndex >= activeDtcsList.length) selectedDtcIndex = 0;

  dtcList.innerHTML = "";
  activeDtcsList.forEach((d, idx) => {
    const isPending = d.status === "PENDING";
    const card = document.createElement("div");
    card.className = `dtc-card-item ${isPending ? "pending" : ""} ${idx === selectedDtcIndex ? "selected" : ""}`;
    card.innerHTML = `
      <div>
        <div style="display:flex; align-items:center; gap:8px;">
          <span class="mono" style="font-size:16px; font-weight:800;">${d.code}</span>
          <span class="spec-tag">${d.status}</span>
        </div>
        <div style="font-size:13px; color:var(--text-secondary); margin-top:4px;">${d.description || "Código de falla específico"}</div>
      </div>
      <div style="display:flex; gap:8px;">
        <button class="btn btn-sm btn-primary" onclick="event.stopPropagation(); jumpToDiagnoseDtc('${d.code}')">Diagnosticar con IA</button>
        <button class="btn btn-sm" onclick="event.stopPropagation(); openFixModal('${d.code}')">Registrar Arreglo</button>
      </div>
    `;
    card.addEventListener("click", () => {
      selectedDtcIndex = idx;
      renderDtcs(activeDtcsList, globalFreezeFrame);
    });
    dtcList.appendChild(card);
  });

  renderFreezeFrameDetail(activeDtcsList[selectedDtcIndex]);
}

function renderFreezeFrameDetail(dtc) {
  if (!dtc) return;
  ffPanelTitle.textContent = `Freeze Frame de ${dtc.code} (${dtc.status})`;
  const ff = dtc.freeze_frame || {};
  const items = [
    { lbl: "Régimen Motor", val: ff.rpm != null ? `${Math.round(ff.rpm)} RPM` : "N/D" },
    { lbl: "Velocidad", val: ff.speed_kph != null ? `${Math.round(ff.speed_kph)} km/h` : "N/D" },
    { lbl: "Temp. Refrigerante", val: ff.coolant_temp_c != null ? `${ff.coolant_temp_c} °C` : "N/D" },
    { lbl: "Temp. Admisión (IAT)", val: ff.intake_temp_c != null ? `${ff.intake_temp_c} °C` : "N/D" },
    { lbl: "Short Term Fuel Trim", val: ff.stft_b1 != null ? `${ff.stft_b1 >= 0 ? "+" : ""}${ff.stft_b1}%` : "N/D" },
    { lbl: "Long Term Fuel Trim", val: ff.ltft_b1 != null ? `${ff.ltft_b1 >= 0 ? "+" : ""}${ff.ltft_b1}%` : "N/D" },
    { lbl: "Presión MAP", val: ff.map_kpa != null ? `${ff.map_kpa} kPa` : "N/D" },
    { lbl: "Flujo MAF", val: ff.maf_gs != null ? `${ff.maf_gs} g/s` : "N/D" }
  ];

  ffGrid.innerHTML = items.map(it => `
    <div class="ff-box">
      <div class="ff-box-lbl">${it.lbl}</div>
      <div class="ff-box-val mono">${it.val}</div>
    </div>
  `).join("");

  ffActions.innerHTML = `
    <button class="btn btn-primary" onclick="jumpToDiagnoseDtc('${dtc.code}')">Analizar este Freeze Frame con Baymax IA</button>
    <button class="btn" onclick="openFixModal('${dtc.code}')">Guardar Solución Aplicada</button>
  `;
}

function jumpToDiagnoseDtc(code) {
  switchModule("modAssistant");
  if (window.diagnoseDtc) window.diagnoseDtc(code);
}
window.jumpToDiagnoseDtc = jumpToDiagnoseDtc;

async function clearDtcs() {
  if (!confirm("¿Borrar códigos de falla de la ECU (Modo 04)? Esto también reiniciará los monitores de emisiones.")) return;
  try {
    const res = await fetch("/api/dtcs/clear", { method: "POST" });
    const data = await res.json();
    if (data.ok) renderDtcs([], {});
  } catch (err) {
    alert("No se pudieron borrar los códigos.");
  }
}

// ==================================================================== 8. Mi Garaje & Bitácora (Módulo 6)
function initGarageModule() {
  const btnOpenNew = document.getElementById("btnOpenNewVehicle");
  const btnSaveNew = document.getElementById("btnSaveNewVehicle");
  const btnIndexManuals = document.getElementById("btnIndexManuals");

  if (btnOpenNew) {
    btnOpenNew.addEventListener("click", () => {
      document.getElementById("modalNewVehicle").classList.add("active");
    });
  }

  if (btnSaveNew) {
    btnSaveNew.addEventListener("click", createNewVehicle);
  }

  if (btnIndexManuals) {
    btnIndexManuals.addEventListener("click", async () => {
      btnIndexManuals.textContent = "Indexando PDFs...";
      btnIndexManuals.disabled = true;
      try {
        const res = await fetch("/api/manuals/ingest", { method: "POST" });
        const data = await res.json();
        const r = data.report || {};
        alert(`Manuales procesados.\nNuevos indexados: ${(r.indexed || []).length}\nOmitidos (sin cambios): ${(r.skipped || []).length}`);
      } catch (e) {
        alert("Error al indexar manuales PDF.");
      } finally {
        btnIndexManuals.disabled = false;
        btnIndexManuals.textContent = "Indexar Manuales PDF (./data/manuals)";
      }
    });
  }
}

function renderGarageFleet() {
  const grid = document.getElementById("garageFleetGrid");
  if (!grid) return;
  grid.innerHTML = "";

  allVehicles.forEach(v => {
    const isAct = currentVehicle && currentVehicle.id === v.id;
    const card = document.createElement("div");
    card.className = `vehicle-card ${isAct ? "active" : ""}`;
    card.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:flex-start;">
        <div>
          <div style="font-size:15px; font-weight:700;">${v.year} ${v.make} ${v.model}</div>
          <div style="font-size:12px; color:var(--text-secondary);">Alias: <strong>${v.alias}</strong></div>
        </div>
        ${isAct ? `<span class="status-chip online" style="font-size:10.5px;">Activo</span>` : ""}
      </div>
      <div class="specs-pills">
        <span class="spec-tag mono">${v.engine_code || "Motor Std"}</span>
        <span class="spec-tag">${v.aspiration || "NA"}</span>
        <span class="spec-tag mono">${v.curb_weight_kg || "--"} kg</span>
        <span class="spec-tag mono">Corte ${v.redline_rpm || "--"} RPM</span>
      </div>
    `;
    card.addEventListener("click", () => selectVehicleById(v.id));
    grid.appendChild(card);
  });
}

async function loadVehicleHistory(vId) {
  const repList = document.getElementById("garageRepairsList");
  const runList = document.getElementById("garageRunsList");
  if (!repList || !runList) return;

  try {
    const res = await fetch(`/api/vehicles/${vId}/history`);
    const data = await res.json();

    // Reparaciones
    const repairs = data.repairs || [];
    if (repairs.length === 0) {
      repList.innerHTML = `<div style="color:var(--text-muted); font-size:12.5px;">Aún no hay reparaciones registradas para este vehículo.</div>`;
    } else {
      repList.innerHTML = repairs.map(r => `
        <div class="source-item">
          <div style="display:flex; justify-content:space-between; font-size:11px; color:var(--text-muted);">
            <span>${r.created_at}</span>
            <span style="color:${r.verified ? "var(--status-ok)" : "var(--status-warn)"}; font-weight:700;">
              ${r.verified ? "VERIFICADO" : "PENDIENTE"}
            </span>
          </div>
          <div style="font-weight:700; margin-top:4px;">Arreglo: ${r.fix || "Sin especificar"}</div>
          ${r.symptoms ? `<div style="font-size:12px; color:var(--text-secondary); margin-top:2px;">Síntoma: ${r.symptoms}</div>` : ""}
        </div>
      `).join("");
    }

    // Corridas Dyno / Pre-stage
    const runs = data.runs || [];
    if (runs.length === 0) {
      runList.innerHTML = `<div style="color:var(--text-muted); font-size:12.5px;">Aún no hay corridas de Dyno o Pre-Stage guardadas para este vehículo.</div>`;
    } else {
      runList.innerHTML = runs.map(run => {
        const resObj = run.result || {};
        const summary = run.run_type === "dyno"
          ? `Virtual Dyno (${run.gear || 3}ª): <strong>${resObj.peak_whp || "--"} WHP</strong> / <strong>${resObj.peak_tq_lbft || "--"} lb-ft</strong>`
          : `Pre-Stage Check: Veredicto <strong>${resObj.verdict || "--"}</strong> (${resObj.score || "--"}/100)`;
        return `
          <div class="source-item">
            <div style="font-size:11px; color:var(--text-muted);">${run.started_at}</div>
            <div style="margin-top:4px;">${summary}</div>
          </div>
        `;
      }).join("");
    }
  } catch (e) {
    console.error("Error cargando historial:", e);
  }
}

async function createNewVehicle() {
  const payload = {
    alias: document.getElementById("nvAlias").value.trim() || "Nuevo Carro",
    year: parseInt(document.getElementById("nvYear").value, 10) || 2015,
    make: document.getElementById("nvMake").value.trim() || "Toyota",
    model: document.getElementById("nvModel").value.trim() || "Corolla",
    engine_code: document.getElementById("nvEngine").value.trim() || "1.8L",
    aspiration: document.getElementById("nvAspiration").value || "NA",
    curb_weight_kg: parseFloat(document.getElementById("nvWeight").value) || 1400,
    redline_rpm: parseInt(document.getElementById("nvRedline").value, 10) || 6300,
    tire_diameter_m: 0.65,
    final_drive: 4.1,
    gear_ratios_json: "[3.5, 2.1, 1.4, 1.0, 0.8]"
  };

  try {
    const res = await fetch("/api/vehicles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const created = await res.json();
    document.getElementById("modalNewVehicle").classList.remove("active");
    await loadVehicles();
    if (created && created.id) {
      await selectVehicleById(created.id);
    }
  } catch (e) {
    alert("Error creando vehículo.");
  }
}

// ==================================================================== Acciones Simulador
async function sendMockAction(action) {
  try {
    await fetch("/api/mock/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action })
    });
  } catch (e) {
    console.error("Error simulador:", e);
  }
}
window.sendMockAction = sendMockAction;
