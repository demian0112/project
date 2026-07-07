"use strict";

const state = {
  users: [],
  devices: [],
  alerts: [],
};

const csrfToken =
  document.querySelector('meta[name="csrf-token"]')?.content || "";

const elements = {
  refreshButton: document.querySelector("#refresh-button"),
  lastUpdated: document.querySelector("#last-updated"),
  overviewDate: document.querySelector("#overview-date"),
  userCount: document.querySelector("#user-count"),
  activeUserSummary: document.querySelector("#active-user-summary"),
  onlineDeviceCount: document.querySelector("#online-device-count"),
  onlineDeviceSummary: document.querySelector("#online-device-summary"),
  runningDeviceCount: document.querySelector("#running-device-count"),
  startingDeviceSummary: document.querySelector("#starting-device-summary"),
  errorDeviceCount: document.querySelector("#error-device-count"),
  offlineDeviceSummary: document.querySelector("#offline-device-summary"),
  pendingAlertCount: document.querySelector("#pending-alert-count"),
  latestAlertSummary: document.querySelector("#latest-alert-summary"),
  navAlertCount: document.querySelector("#nav-alert-count"),
  enabledSummary: document.querySelector("#enabled-summary"),
  healthRing: document.querySelector("#health-ring"),
  healthyRate: document.querySelector("#healthy-rate"),
  healthOnlineCount: document.querySelector("#health-online-count"),
  healthOfflineCount: document.querySelector("#health-offline-count"),
  healthErrorCount: document.querySelector("#health-error-count"),
  attentionPanel: document.querySelector("#attention-panel"),
  attentionTitle: document.querySelector("#attention-title"),
  attentionCopy: document.querySelector("#attention-copy"),
  deviceSearch: document.querySelector("#device-search"),
  stateFilter: document.querySelector("#state-filter"),
  detectionFilter: document.querySelector("#detection-filter"),
  ownerFilter: document.querySelector("#owner-filter"),
  deviceTableBody: document.querySelector("#device-table-body"),
  deviceTableSummary: document.querySelector("#device-table-summary"),
  addDeviceButton: document.querySelector("#add-device-button"),
  userSearch: document.querySelector("#user-search"),
  userStatusFilter: document.querySelector("#user-status-filter"),
  userTableBody: document.querySelector("#user-table-body"),
  userTableSummary: document.querySelector("#user-table-summary"),
  alertStatusFilter: document.querySelector("#alert-status-filter"),
  alertTableBody: document.querySelector("#alert-table-body"),
  alertTableSummary: document.querySelector("#alert-table-summary"),
  userModal: document.querySelector("#user-modal"),
  userForm: document.querySelector("#user-form"),
  userModalTitle: document.querySelector("#user-modal-title"),
  userId: document.querySelector("#user-id"),
  userNickname: document.querySelector("#user-nickname"),
  userPhone: document.querySelector("#user-phone"),
  userRole: document.querySelector("#user-role"),
  userStatus: document.querySelector("#user-status"),
  userRecordSummary: document.querySelector("#user-record-summary"),
  deviceModal: document.querySelector("#device-modal"),
  deviceForm: document.querySelector("#device-form"),
  deviceModalTitle: document.querySelector("#device-modal-title"),
  deviceId: document.querySelector("#device-id"),
  deviceUid: document.querySelector("#device-uid"),
  deviceName: document.querySelector("#device-name"),
  deviceOwner: document.querySelector("#device-owner"),
  deviceStatus: document.querySelector("#device-status"),
  deviceLocation: document.querySelector("#device-location"),
  deviceRemark: document.querySelector("#device-remark"),
  deviceTopicPreview: document.querySelector("#device-topic-preview"),
  deviceLiveSummary: document.querySelector("#device-live-summary"),
  toast: document.querySelector("#toast"),
  toastTitle: document.querySelector("#toast-title"),
  toastMessage: document.querySelector("#toast-message"),
  toastClose: document.querySelector("#toast-close"),
};

const labels = {
  state: {
    online: "在线",
    offline: "离线",
    error: "故障",
  },
  runtime: {
    idle: "空闲",
    running: "运行中",
    stopped: "已停止",
  },
  detection: {
    idle: "空闲",
    starting: "启动中",
    running: "检测中",
    stopping: "停止中",
  },
  quality: {
    good: "良好",
    fair: "一般",
    poor: "较差",
    unknown: "暂无数据",
  },
  eventStatus: {
    pending: "待处理",
    confirmed: "已确认",
    ignored: "已忽略",
  },
};

let toastTimer;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatDate(value, fallback = "暂无记录") {
  if (!value) {
    return fallback;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return fallback;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatRelative(value, fallback = "暂无通信") {
  if (!value) {
    return fallback;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return fallback;
  }
  const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) {
    return `${seconds} 秒前`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return `${minutes} 分钟前`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return `${hours} 小时前`;
  }
  return `${Math.floor(hours / 24)} 天前`;
}

function getInitials(name) {
  const cleanName = String(name ?? "").trim();
  return cleanName ? [...cleanName].slice(0, 2).join("") : "微";
}

function userName(user) {
  return user?.nickname || `用户 #${user?.id || "?"}`;
}

function showToast(title, message = "", kind = "success") {
  window.clearTimeout(toastTimer);
  elements.toastTitle.textContent = title;
  elements.toastMessage.textContent = message;
  elements.toast.dataset.kind = kind;
  elements.toast.hidden = false;
  toastTimer = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, 4200);
}

async function apiRequest(url, options = {}) {
  const method = options.method || "GET";
  const headers = {
    Accept: "application/json",
    ...options.headers,
  };
  if (options.body) {
    headers["Content-Type"] = "application/json";
  }
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
    headers["X-CSRF-Token"] = csrfToken;
  }

  const response = await fetch(url, {
    ...options,
    method,
    headers,
    cache: "no-store",
  });
  if (response.status === 401) {
    window.location.assign("/admin/login");
    throw new Error("管理员登录状态已失效");
  }

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `请求失败（HTTP ${response.status}）`);
  }
  return data;
}

function renderOverview() {
  const activeUsers = state.users.filter((user) => user.status === "active");
  const onlineDevices = state.devices.filter(
    (device) => device.state === "online",
  );
  const offlineDevices = state.devices.filter(
    (device) => device.state === "offline",
  );
  const errorDevices = state.devices.filter(
    (device) => device.state === "error",
  );
  const runningDevices = state.devices.filter(
    (device) => device.detection_state === "running",
  );
  const startingDevices = state.devices.filter(
    (device) => device.detection_state === "starting",
  );
  const enabledDevices = state.devices.filter(
    (device) => device.status === "enabled",
  );
  const pendingAlerts = state.alerts.filter(
    (event) => event.status === "pending",
  );
  const rate = state.devices.length
    ? Math.round((onlineDevices.length / state.devices.length) * 100)
    : 0;

  elements.userCount.textContent = state.users.length;
  elements.activeUserSummary.textContent =
    `${activeUsers.length} 位账户正常`;
  elements.onlineDeviceCount.textContent = onlineDevices.length;
  elements.onlineDeviceSummary.textContent =
    `共 ${state.devices.length} 台设备`;
  elements.runningDeviceCount.textContent = runningDevices.length;
  elements.startingDeviceSummary.textContent =
    `启动中 ${startingDevices.length} 台`;
  elements.errorDeviceCount.textContent = errorDevices.length;
  elements.offlineDeviceSummary.textContent =
    `离线 ${offlineDevices.length} 台`;
  elements.pendingAlertCount.textContent = pendingAlerts.length;
  elements.enabledSummary.textContent =
    `${enabledDevices.length} 台已启用`;
  elements.healthyRate.textContent = `${rate}%`;
  elements.healthRing.style.setProperty("--online-rate", rate);
  elements.healthOnlineCount.textContent = onlineDevices.length;
  elements.healthOfflineCount.textContent = offlineDevices.length;
  elements.healthErrorCount.textContent = errorDevices.length;

  if (state.alerts.length) {
    elements.latestAlertSummary.textContent =
      `最近 ${formatRelative(state.alerts[0].occurred_at)}`;
  } else {
    elements.latestAlertSummary.textContent = "暂无跌倒记录";
  }

  elements.navAlertCount.textContent = pendingAlerts.length;
  elements.navAlertCount.hidden = pendingAlerts.length === 0;

  const hasAttention = pendingAlerts.length > 0 || errorDevices.length > 0;
  elements.attentionPanel.classList.toggle("is-healthy", !hasAttention);
  if (pendingAlerts.length > 0) {
    const latest = pendingAlerts[0];
    elements.attentionTitle.textContent =
      `${pendingAlerts.length} 条跌倒告警等待处理`;
    elements.attentionCopy.textContent =
      `最近事件来自 ${latest.display_name || latest.device_name}，发生于 ${formatDate(latest.occurred_at)}。`;
  } else if (errorDevices.length > 0) {
    elements.attentionTitle.textContent =
      `${errorDevices.length} 台设备处于故障状态`;
    elements.attentionCopy.textContent =
      `请检查 ${errorDevices.slice(0, 3).map((item) => item.name).join("、")} 的故障上报。`;
  } else {
    elements.attentionTitle.textContent = "当前没有需要立即处理的事项";
    elements.attentionCopy.textContent =
      "设备故障与待处理跌倒告警均为零，系统仍会持续接收 MQTT 状态。";
  }
}

function renderOwnerOptions() {
  const selectedValue = elements.ownerFilter.value;
  const deviceOwnerValue = elements.deviceOwner.value;
  const options = state.users
    .map(
      (user) =>
        `<option value="${escapeHtml(user.id)}">${escapeHtml(userName(user))}</option>`,
    )
    .join("");
  elements.ownerFilter.innerHTML = `
    <option value="">全部用户</option>
    ${options}
  `;
  elements.deviceOwner.innerHTML = options;

  if (
    state.users.some((user) => String(user.id) === String(selectedValue))
  ) {
    elements.ownerFilter.value = selectedValue;
  }
  if (
    state.users.some((user) => String(user.id) === String(deviceOwnerValue))
  ) {
    elements.deviceOwner.value = deviceOwnerValue;
  }
}

function filteredDevices() {
  const search = elements.deviceSearch.value.trim().toLowerCase();
  const stateValue = elements.stateFilter.value;
  const detection = elements.detectionFilter.value;
  const ownerId = elements.ownerFilter.value;
  return state.devices.filter((device) => {
    const searchable = [
      device.name,
      device.device_uid,
      device.owner_username,
      device.location,
      device.fault_message,
    ]
      .join(" ")
      .toLowerCase();
    return (
      (!search || searchable.includes(search)) &&
      (!stateValue || device.state === stateValue) &&
      (!detection || device.detection_state === detection) &&
      (!ownerId || String(device.owner_id) === String(ownerId))
    );
  });
}

function deviceRow(device) {
  const stateName = labels.state[device.state] || device.state;
  const detection =
    labels.detection[device.detection_state] || device.detection_state;
  const quality =
    labels.quality[device.network_quality] || device.network_quality;
  const runtime =
    labels.runtime[device.runtime_state] || device.runtime_state;
  const disabledNote =
    device.status === "disabled"
      ? '<span class="fault-copy">管理端已禁用</span>'
      : "";
  const faultNote =
    device.fault_message
      ? `<span class="fault-copy" title="${escapeHtml(device.fault_message)}">${escapeHtml(device.fault_message)}</span>`
      : disabledNote;
  const session =
    device.current_session
      ? `<code class="session-code" title="${escapeHtml(device.current_session)}">${escapeHtml(device.current_session)}</code>`
      : `<span class="session-code">硬件：${escapeHtml(runtime)}</span>`;

  return `
    <tr>
      <td>
        <span class="entity-cell">
          <span class="entity-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24">
              <rect x="5" y="2" width="14" height="20" rx="4"/>
              <path d="M9 17h6M8 6h8v7H8z"/>
            </svg>
          </span>
          <span class="entity-copy">
            <strong title="${escapeHtml(device.name)}">${escapeHtml(device.name)}</strong>
            <small title="${escapeHtml(device.device_uid)}">${escapeHtml(device.device_uid)}</small>
          </span>
        </span>
      </td>
      <td>
        <span class="status-pill state-${escapeHtml(device.state)}">
          ${escapeHtml(stateName)}
        </span>
        ${faultNote}
      </td>
      <td>
        <span class="detection-pill detection-${escapeHtml(device.detection_state)}">
          ${escapeHtml(detection)}
        </span>
        ${session}
      </td>
      <td>
        <span class="quality-pill quality-${escapeHtml(device.network_quality)}">
          ${escapeHtml(quality)}
        </span>
        <span class="session-code">
          CSI ${escapeHtml(formatRelative(device.last_csi_at, "暂无"))}
        </span>
      </td>
      <td>
        <span class="owner-location">
          <strong>${escapeHtml(device.owner_username || `用户 #${device.owner_id}`)}</strong>
          <small>${escapeHtml(device.location || "未填写位置")}</small>
        </span>
      </td>
      <td>
        <span class="time-stack">
          <strong>${escapeHtml(formatRelative(device.last_seen_at))}</strong>
          <small>${escapeHtml(formatDate(device.last_seen_at, "尚未上报"))}</small>
        </span>
      </td>
      <td>
        <span class="table-actions">
          <button class="icon-button" type="button" data-edit-device="${escapeHtml(device.id)}">
            编辑
          </button>
          <button class="icon-button warning" type="button" data-simulate-fall="${escapeHtml(device.id)}">
            模拟跌倒
          </button>
          <button class="icon-button danger" type="button" data-delete-device="${escapeHtml(device.id)}">
            删除
          </button>
        </span>
      </td>
    </tr>
  `;
}

function renderDevices() {
  const devices = filteredDevices();
  if (!devices.length) {
    elements.deviceTableBody.innerHTML = emptyRow(
      7,
      "暂无匹配设备",
      state.devices.length
        ? "请调整搜索条件或筛选项"
        : "点击“登记设备”建立第一条设备记录",
    );
  } else {
    elements.deviceTableBody.innerHTML = devices.map(deviceRow).join("");
  }
  elements.deviceTableSummary.textContent =
    `当前显示 ${devices.length} 台，共 ${state.devices.length} 台设备`;

  elements.deviceTableBody
    .querySelectorAll("[data-edit-device]")
    .forEach((button) => {
      button.addEventListener("click", () => {
        openDeviceModal(Number(button.dataset.editDevice));
      });
    });
  elements.deviceTableBody
    .querySelectorAll("[data-delete-device]")
    .forEach((button) => {
      button.addEventListener("click", () => {
        deleteDevice(Number(button.dataset.deleteDevice));
      });
    });
  elements.deviceTableBody
    .querySelectorAll("[data-simulate-fall]")
    .forEach((button) => {
      button.addEventListener("click", () => {
        simulateFall(Number(button.dataset.simulateFall));
      });
    });
}

function filteredUsers() {
  const search = elements.userSearch.value.trim().toLowerCase();
  const status = elements.userStatusFilter.value;
  return state.users.filter((user) => {
    const searchable = [
      userName(user),
      user.phone,
      user.id,
    ]
      .join(" ")
      .toLowerCase();
    return (
      (!search || searchable.includes(search)) &&
      (!status || user.status === status)
    );
  });
}

function userRow(user) {
  const name = userName(user);
  return `
    <tr>
      <td>
        <span class="user-cell">
          <span class="avatar">${escapeHtml(getInitials(name))}</span>
          <span class="user-copy">
            <strong>${escapeHtml(name)}</strong>
            <small>用户 #${escapeHtml(user.id)} · ${escapeHtml(user.role || "user")}</small>
          </span>
        </span>
      </td>
      <td>${escapeHtml(user.phone || "未绑定")}</td>
      <td><span class="count-chip">${escapeHtml(user.device_count)}</span></td>
      <td>
        <span class="time-stack">
          <strong>${escapeHtml(formatRelative(user.last_login_at, "尚未登录"))}</strong>
          <small>${escapeHtml(formatDate(user.last_login_at, "暂无记录"))}</small>
        </span>
      </td>
      <td>
        <span class="account-status is-${user.status}">
          ${user.status === "active" ? "正常" : "已禁用"}
        </span>
      </td>
      <td>
        <span class="table-actions">
          <button class="icon-button" type="button" data-edit-user="${escapeHtml(user.id)}">
            编辑
          </button>
          <button class="icon-button danger" type="button" data-delete-user="${escapeHtml(user.id)}">
            删除
          </button>
        </span>
      </td>
    </tr>
  `;
}

function renderUsers() {
  const users = filteredUsers();
  if (!users.length) {
    elements.userTableBody.innerHTML = emptyRow(
      6,
      "暂无匹配用户",
      state.users.length
        ? "请调整搜索条件"
        : "用户首次进入微信小程序后会自动创建",
    );
  } else {
    elements.userTableBody.innerHTML = users.map(userRow).join("");
  }
  elements.userTableSummary.textContent =
    `当前显示 ${users.length} 位，共 ${state.users.length} 位用户`;

  elements.userTableBody
    .querySelectorAll("[data-edit-user]")
    .forEach((button) => {
      button.addEventListener("click", () => {
        openUserModal(Number(button.dataset.editUser));
      });
    });
  elements.userTableBody
    .querySelectorAll("[data-delete-user]")
    .forEach((button) => {
      button.addEventListener("click", () => {
        deleteUser(Number(button.dataset.deleteUser));
      });
    });
}

function filteredAlerts() {
  const status = elements.alertStatusFilter.value;
  return state.alerts.filter((event) => !status || event.status === status);
}

function alertRow(event) {
  const statusName =
    labels.eventStatus[event.status] || event.status;
  const quality =
    labels.quality[event.network_quality] || event.network_quality || "未知";
  let actions = "";
  if (event.status === "pending") {
    actions = `
      <button class="text-button success" type="button" data-alert-status="confirmed" data-alert-id="${escapeHtml(event.id)}">确认</button>
      <button class="text-button danger" type="button" data-alert-status="ignored" data-alert-id="${escapeHtml(event.id)}">忽略</button>
    `;
  } else {
    actions = `
      <button class="text-button" type="button" data-alert-status="pending" data-alert-id="${escapeHtml(event.id)}">重新打开</button>
    `;
  }
  return `
    <tr>
      <td>
        <span class="time-stack">
          <strong>${escapeHtml(formatDate(event.occurred_at))}</strong>
          <small>${escapeHtml(formatRelative(event.occurred_at))}</small>
        </span>
      </td>
      <td>
        <span class="entity-copy">
          <strong>${escapeHtml(event.display_name || event.device_name)}</strong>
          <small>${escapeHtml(event.device_name)} · ${escapeHtml(event.location || "未知位置")}</small>
        </span>
      </td>
      <td>${escapeHtml(event.owner_name || `用户 #${event.user_id}`)}</td>
      <td>
        <span class="quality-pill quality-${escapeHtml(event.network_quality || "unknown")}">
          ${escapeHtml(quality)}
        </span>
      </td>
      <td>${event.notified ? "已推送" : "未推送"}</td>
      <td>
        <span class="event-status status-${escapeHtml(event.status)}">
          ${escapeHtml(statusName)}
        </span>
      </td>
      <td><span class="alert-actions">${actions}</span></td>
    </tr>
  `;
}

function renderAlerts() {
  const alerts = filteredAlerts();
  if (!alerts.length) {
    elements.alertTableBody.innerHTML = emptyRow(
      7,
      "暂无跌倒事件",
      state.alerts.length
        ? "当前筛选条件下没有记录"
        : "算法返回 1 后，事件会出现在这里",
    );
  } else {
    elements.alertTableBody.innerHTML = alerts.map(alertRow).join("");
  }
  const pending = state.alerts.filter(
    (event) => event.status === "pending",
  ).length;
  elements.alertTableSummary.textContent =
    `当前显示 ${alerts.length} 条，共 ${state.alerts.length} 条事件，待处理 ${pending} 条`;

  elements.alertTableBody
    .querySelectorAll("[data-alert-status]")
    .forEach((button) => {
      button.addEventListener("click", () => {
        updateAlert(
          Number(button.dataset.alertId),
          button.dataset.alertStatus,
        );
      });
    });
}

function emptyRow(columns, title, copy) {
  return `
    <tr class="empty-row">
      <td colspan="${columns}">
        <span class="empty-state">
          <span class="empty-state-icon" aria-hidden="true">⌁</span>
          <strong>${escapeHtml(title)}</strong>
          <small>${escapeHtml(copy)}</small>
        </span>
      </td>
    </tr>
  `;
}

function renderDashboard() {
  renderOverview();
  renderOwnerOptions();
  renderDevices();
  renderUsers();
  renderAlerts();
}

function setLoading(isLoading) {
  elements.refreshButton.disabled = isLoading;
  elements.refreshButton.classList.toggle("is-loading", isLoading);
}

async function loadDashboard({ silent = false } = {}) {
  setLoading(true);
  try {
    const [users, devices, alerts] = await Promise.all([
      apiRequest("/api/users"),
      apiRequest("/api/devices"),
      apiRequest("/api/fall-events?limit=200"),
    ]);
    state.users = users;
    state.devices = devices;
    state.alerts = alerts;
    renderDashboard();

    const now = new Date();
    elements.lastUpdated.textContent = new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(now);
    elements.overviewDate.textContent = new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "long",
      day: "numeric",
      weekday: "long",
    }).format(now);
  } catch (error) {
    if (!silent) {
      showToast("数据加载失败", error.message, "error");
    }
  } finally {
    setLoading(false);
  }
}

function openModal(modal) {
  modal.hidden = false;
  document.body.classList.add("modal-open");
  window.setTimeout(() => {
    modal.querySelector("input:not([type='hidden']), select")?.focus();
  }, 0);
}

function closeModal(modal) {
  modal.hidden = true;
  if (!document.querySelector(".modal-backdrop:not([hidden])")) {
    document.body.classList.remove("modal-open");
  }
}

function openUserModal(userId) {
  const user = state.users.find((item) => item.id === userId);
  if (!user) {
    return;
  }
  elements.userForm.reset();
  elements.userId.value = user.id;
  elements.userNickname.value = user.nickname || "";
  elements.userPhone.value = user.phone || "";
  elements.userRole.value = user.role || "user";
  elements.userStatus.value = user.status || "active";
  elements.userModalTitle.textContent = `编辑 ${userName(user)}`;
  elements.userRecordSummary.innerHTML = `
    <strong>用户 #${escapeHtml(user.id)}</strong>
    · 微信登录用户
    · 创建于 ${escapeHtml(formatDate(user.created_at))}
  `;
  openModal(elements.userModal);
}

function updateTopicPreview() {
  const deviceName = elements.deviceUid.value.trim() || "{device_name}";
  elements.deviceTopicPreview.textContent =
    `csi/v1/devices/${deviceName}/up/#`;
}

function openDeviceModal(deviceId = null) {
  if (!state.users.length) {
    showToast(
      "无法登记设备",
      "请先让用户登录微信小程序，或准备一条用户记录。",
      "error",
    );
    return;
  }

  elements.deviceForm.reset();
  renderOwnerOptions();
  const device = state.devices.find((item) => item.id === deviceId);
  elements.deviceId.value = device?.id || "";
  elements.deviceUid.value = device?.device_uid || "";
  elements.deviceUid.disabled = Boolean(device);
  elements.deviceName.value = device?.name || "";
  elements.deviceOwner.value = device?.owner_id || state.users[0].id;
  elements.deviceStatus.value = device?.status || "enabled";
  elements.deviceLocation.value = device?.location || "";
  elements.deviceRemark.value = device?.remark || "";
  elements.deviceModalTitle.textContent = device ? "编辑设备" : "登记设备";
  elements.deviceLiveSummary.hidden = !device;
  if (device) {
    elements.deviceLiveSummary.innerHTML = `
      当前快照：
      <strong>${escapeHtml(labels.state[device.state] || device.state)}</strong>
      · 检测 ${escapeHtml(labels.detection[device.detection_state] || device.detection_state)}
      · 网络 ${escapeHtml(labels.quality[device.network_quality] || device.network_quality)}
    `;
  }
  updateTopicPreview();
  openModal(elements.deviceModal);
}

async function deleteUser(userId) {
  const user = state.users.find((item) => item.id === userId);
  if (!user) {
    return;
  }
  const impact = user.device_count
    ? `这会同时删除其 ${user.device_count} 台设备及关联跌倒事件。`
    : "该操作无法撤销。";
  if (!window.confirm(`确定删除“${userName(user)}”吗？${impact}`)) {
    return;
  }
  try {
    await apiRequest(`/api/users/${userId}`, { method: "DELETE" });
    await loadDashboard({ silent: true });
    showToast("用户已删除", impact);
  } catch (error) {
    showToast("删除失败", error.message, "error");
  }
}

async function deleteDevice(deviceId) {
  const device = state.devices.find((item) => item.id === deviceId);
  if (
    !device ||
    !window.confirm(
      `确定删除设备“${device.name}”吗？关联跌倒事件也会被删除。`,
    )
  ) {
    return;
  }
  try {
    await apiRequest(`/api/devices/${deviceId}`, { method: "DELETE" });
    await loadDashboard({ silent: true });
    showToast("设备已删除");
  } catch (error) {
    showToast("删除失败", error.message, "error");
  }
}

async function simulateFall(deviceId) {
  const device = state.devices.find((item) => item.id === deviceId);
  if (!device) {
    return;
  }
  const deviceName = device.name || device.device_uid;
  const confirmed = window.confirm(
    `确定要为设备“${deviceName}”模拟一次跌倒告警吗？这会创建一条跌倒事件，并尝试向绑定用户发送微信服务通知。`,
  );
  if (!confirmed) {
    return;
  }

  try {
    const result = await apiRequest(`/api/devices/${deviceId}/simulate-fall`, {
      method: "POST",
      body: JSON.stringify({
        remark: "管理员模拟跌倒触发",
        send_wechat: true,
      }),
    });
    await loadDashboard({ silent: true });
    const wechat = result.wechat || {};
    if (wechat.sent) {
      showToast("模拟跌倒事件已创建", "微信通知发送成功");
      return;
    }
    const reason = wechat.errmsg || wechat.reason || "微信通知未发送";
    showToast(
      "模拟跌倒事件已创建",
      `但微信通知未发送：${reason}`,
      "error",
    );
  } catch (error) {
    showToast("模拟跌倒失败", error.message, "error");
  }
}

async function updateAlert(eventId, status) {
  try {
    await apiRequest(`/api/fall-events/${eventId}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
    await loadDashboard({ silent: true });
    showToast(
      "事件状态已更新",
      `已标记为${labels.eventStatus[status] || status}`,
    );
  } catch (error) {
    showToast("更新失败", error.message, "error");
  }
}

elements.userForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const userId = elements.userId.value;
  try {
    await apiRequest(`/api/users/${userId}`, {
      method: "PUT",
      body: JSON.stringify({
        nickname: elements.userNickname.value.trim(),
        phone: elements.userPhone.value.trim(),
        role: elements.userRole.value,
        status: elements.userStatus.value,
      }),
    });
    closeModal(elements.userModal);
    await loadDashboard({ silent: true });
    showToast("用户资料已更新");
  } catch (error) {
    showToast("保存失败", error.message, "error");
  }
});

elements.deviceForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const deviceId = elements.deviceId.value;
  const payload = {
    device_uid: elements.deviceUid.value.trim(),
    name: elements.deviceName.value.trim(),
    owner_id: Number(elements.deviceOwner.value),
    status: elements.deviceStatus.value,
    location: elements.deviceLocation.value.trim(),
    remark: elements.deviceRemark.value.trim(),
  };
  try {
    await apiRequest(
      deviceId ? `/api/devices/${deviceId}` : "/api/devices",
      {
        method: deviceId ? "PUT" : "POST",
        body: JSON.stringify(payload),
      },
    );
    closeModal(elements.deviceModal);
    await loadDashboard({ silent: true });
    showToast(deviceId ? "设备资料已更新" : "设备登记成功");
  } catch (error) {
    showToast("保存失败", error.message, "error");
  }
});

elements.refreshButton.addEventListener("click", () => loadDashboard());
elements.addDeviceButton.addEventListener("click", () => openDeviceModal());
elements.deviceUid.addEventListener("input", updateTopicPreview);
elements.deviceSearch.addEventListener("input", renderDevices);
elements.stateFilter.addEventListener("change", renderDevices);
elements.detectionFilter.addEventListener("change", renderDevices);
elements.ownerFilter.addEventListener("change", renderDevices);
elements.userSearch.addEventListener("input", renderUsers);
elements.userStatusFilter.addEventListener("change", renderUsers);
elements.alertStatusFilter.addEventListener("change", renderAlerts);
elements.toastClose.addEventListener("click", () => {
  elements.toast.hidden = true;
});

document.querySelectorAll("[data-close-modal]").forEach((button) => {
  button.addEventListener("click", () => {
    closeModal(document.querySelector(`#${button.dataset.closeModal}`));
  });
});

document.querySelectorAll(".modal-backdrop").forEach((modal) => {
  modal.addEventListener("mousedown", (event) => {
    if (event.target === modal) {
      closeModal(modal);
    }
  });
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    document.querySelectorAll(".modal-backdrop:not([hidden])").forEach(
      (modal) => closeModal(modal),
    );
  }
});

const sections = document.querySelectorAll(
  "#overview, #devices, #users, #alerts",
);
const navigationLinks = document.querySelectorAll(".side-nav a");
const sectionObserver = new IntersectionObserver(
  (entries) => {
    const visible = entries
      .filter((entry) => entry.isIntersecting)
      .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) {
      return;
    }
    navigationLinks.forEach((link) => {
      link.classList.toggle(
        "is-active",
        link.getAttribute("href") === `#${visible.target.id}`,
      );
    });
  },
  { rootMargin: "-20% 0px -65% 0px", threshold: [0, 0.2, 0.6] },
);
sections.forEach((section) => sectionObserver.observe(section));

loadDashboard();
window.setInterval(() => loadDashboard({ silent: true }), 30_000);
