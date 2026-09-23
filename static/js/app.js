let dbData = { resolutions: [], costumes: [] };
let currentTargetDevice = '';

document.addEventListener('DOMContentLoaded', async () => {
  await fetchDbData();
  await loadConfig();
  await loadBackupList();
  await loadCorrections();
  await loadUnrecognizedImages();
  await fetchAdbDevices();
  await checkDeviceResolution();
  await pollLogsAndStatus(); // 페이지 로드 시 상태 및 로그 즉시 동기화
});

async function pollLogsAndStatus() {
  try {
    const res = await fetch('/api/logs/poll');
    const data = await res.json();
    if (data.status === 'success') {
      if (Array.isArray(data.logs)) {
        data.logs.forEach((logMsg) => {
          addLog(logMsg, 'system'); // 웹 UI 로그창에 출력
        });
      }
      if (typeof data.is_running === 'boolean') {
        setMacroState(data.is_running);
      }
    }
  } catch (err) {
    // 통신 에러 무시 (서버 재시작 등 상황 대비)
  }
}

setInterval(pollLogsAndStatus, 3000);

// DB 데이터 불러오기 통합 함수
async function fetchDbData() {
  try {
    const response = await fetch('/api/db-data');
    const result = await response.json();
    if (result.status === 'success') {
      dbData = result;
      populateResolutionDropdown();
      renderDbTables();
      addLog('DB 데이터를 성공적으로 로드했습니다.', 'info');
    } else {
      addLog(`DB 데이터 로드 실패: ${result.message}`, 'error');
    }
  } catch (err) {
    addLog('DB 서버와 통신할 수 없습니다.', 'error');
  }
}

function populateResolutionDropdown() {
  const select = document.getElementById('resolution');
  if (!select) return;
  select.innerHTML = '';

  if (Array.isArray(dbData.resolutions)) {
    dbData.resolutions.forEach((res) => {
      const opt = document.createElement('option');
      opt.value = res.name;
      opt.innerText = `${res.name} (${res.width}x${res.height})`;
      select.appendChild(opt);
    });
  }
}

async function loadConfig() {
  try {
    const response = await fetch('/api/config');
    const config = await response.json();
    populateUI(config);
    addLog('config.yaml 설정을 로드했습니다.', 'info');
  } catch (err) {
    showMessage('Config를 불러오지 못했습니다.', true);
    addLog('config.yaml 로드 실패', 'error');
  }
}

function populateUI(config) {
  if (!config) return;

  if (config.adb_device !== undefined) {
    currentTargetDevice = config.adb_device;
    fetchAdbDevices(config.adb_device);
  }

  if (document.getElementById('adb_port'))
    document.getElementById('adb_port').value = config.adb_port ?? 5555;
  if (document.getElementById('click_delay'))
    document.getElementById('click_delay').value = config.click_delay ?? 0.5;
  if (config.resolution && document.getElementById('resolution')) {
    document.getElementById('resolution').value = config.resolution;
  }

  if (config.filter) {
    if (document.getElementById('min_5star'))
      document.getElementById('min_5star').value = config.filter.min_5star ?? 0;
    if (document.getElementById('need_5star'))
      document.getElementById('need_5star').value =
        config.filter.need_5star ?? 0;
  }

  const listContainer = document.getElementById('costume-list');
  if (listContainer) {
    listContainer.innerHTML = '';
    if (Array.isArray(config.target_costumes)) {
      config.target_costumes.forEach((item) => {
        addCostumeRow(item.name, item.is_required);
      });
    }
  }

  if (config.discord) {
    if (document.getElementById('discord_enabled'))
      document.getElementById('discord_enabled').checked =
        !!config.discord.enabled;
    if (document.getElementById('discord_bot_token'))
      document.getElementById('discord_bot_token').value =
        config.discord.bot_token ?? '';
    if (document.getElementById('discord_channel_id'))
      document.getElementById('discord_channel_id').value =
        config.discord.channel_id ?? '';
    if (document.getElementById('discord_user_id'))
      document.getElementById('discord_user_id').value =
        config.discord.user_id ?? '';
    if (document.getElementById('discord_share_error'))
      document.getElementById('discord_share_error').checked =
        !!config.discord.share_error;
    if (document.getElementById('discord_share_complete'))
      document.getElementById('discord_share_complete').checked =
        !!config.discord.share_complete;
  }

  if (config.options) {
    if (document.getElementById('auto_fhd_resolution'))
      document.getElementById('auto_fhd_resolution').checked =
        !!config.options.auto_fhd_resolution;
    if (document.getElementById('shutdown_pc_on_completion'))
      document.getElementById('shutdown_pc_on_completion').checked =
        !!config.options.shutdown_pc_on_completion;
    if (document.getElementById('auto_add_new_costume'))
      document.getElementById('auto_add_new_costume').checked =
        !!config.options.auto_add_new_costume;
    if (document.getElementById('debug_mode'))
      document.getElementById('debug_mode').checked =
        !!config.options.debug_mode;
    if (document.getElementById('ocr_test_5star'))
      document.getElementById('ocr_test_5star').checked =
        !!config.options.ocr_test_5star;
    if (document.getElementById('global_match_threshold'))
      document.getElementById('global_match_threshold').value =
        config.options.match_threshold ?? 0.8;
  }
}

function addCostumeRow(selectedName = '', isRequired = false) {
  const listContainer = document.getElementById('costume-list');
  if (!listContainer) return;

  const div = document.createElement('div');
  div.className = 'costume-item';

  let optionsHtml = '<option value="">-- 희망 5성 코스튬 선택 --</option>';
  if (Array.isArray(dbData.costumes)) {
    const fiveStarCostumes = dbData.costumes
      .filter((c) => Number(c.rarity) === 5)
      .sort((a, b) => {
        const nameA = a.full_name || a.costume_name || '';
        const nameB = b.full_name || b.costume_name || '';
        return nameA.localeCompare(nameB, 'ko-KR', { numeric: true });
      });

    fiveStarCostumes.forEach((c) => {
      const name = c.full_name || c.costume_name;
      const isSelected = name === selectedName ? 'selected' : '';
      optionsHtml += `<option value="${name}" ${isSelected}>${name}</option>`;
    });
  }

  div.innerHTML = `
        <select class="costume-name">
            ${optionsHtml}
        </select>
        <label class="form-checkbox-inline">
            <input type="checkbox" class="costume-required" ${isRequired ? 'checked' : ''}> 필수 여부
        </label>
        <button type="button" class="danger-btn" onclick="this.parentElement.remove()">삭제</button>
    `;
  listContainer.appendChild(div);
}

async function saveConfig() {
  const costumeRows = document.querySelectorAll('.costume-item');
  const targetCostumes = [];

  costumeRows.forEach((row) => {
    const name = row.querySelector('.costume-name')?.value;
    const isRequired = row.querySelector('.costume-required')?.checked;
    if (name) {
      targetCostumes.push({ name: name, is_required: isRequired });
    }
  });

  // '?'(옵셔널 체이닝)을 사용하여 HTML 요소가 없어도 에러 없이 기본값을 넣도록 안전하게 처리
  const payload = {
    adb_device: document.getElementById('adb_device')?.value?.trim() || '',
    adb_port: Number(document.getElementById('adb_port')?.value || 5555),
    click_delay: Number(document.getElementById('click_delay')?.value || 0.5),
    resolution: document.getElementById('resolution')?.value || '',
    filter: {
      min_5star: Number(document.getElementById('min_5star')?.value || 0),
      need_5star: Number(document.getElementById('need_5star')?.value || 0),
    },
    target_costumes: targetCostumes,
    discord: {
      enabled: document.getElementById('discord_enabled')?.checked || false,
      bot_token:
        document.getElementById('discord_bot_token')?.value?.trim() || '',
      channel_id:
        document.getElementById('discord_channel_id')?.value?.trim() || '',
      user_id: document.getElementById('discord_user_id')?.value?.trim() || '',
      share_error:
        document.getElementById('discord_share_error')?.checked || false,
      share_complete:
        document.getElementById('discord_share_complete')?.checked || false,
    },
    options: {
      auto_fhd_resolution:
        document.getElementById('auto_fhd_resolution')?.checked || false,
      shutdown_pc_on_completion:
        document.getElementById('shutdown_pc_on_completion')?.checked || false,
      auto_add_new_costume:
        document.getElementById('auto_add_new_costume')?.checked || false,
      debug_mode: document.getElementById('debug_mode')?.checked || false,
      ocr_test_5star:
        document.getElementById('ocr_test_5star')?.checked || false,
      // 인식률 입력창이 없으면 기본값 0.80 적용
      match_threshold: Number(
        document.getElementById('global_match_threshold')?.value || 0.8,
      ),
    },
  };

  try {
    const response = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    showMessage(result.message, result.status !== 'success');
    addLog(result.message, result.status === 'success' ? 'info' : 'error');
    loadBackupList();
  } catch (err) {
    showMessage('저장 중 오류가 발생했습니다.', true);
    addLog('설정 저장 중 오류 발생', 'error');
  }
}

async function loadBackupList() {
  try {
    const response = await fetch('/api/backups');
    const result = await response.json();
    if (result.status === 'success') {
      const select = document.getElementById('backup-select');
      if (select) {
        select.innerHTML = '<option value="">-- 백업 파일 선택 --</option>';
        result.files.forEach((file) => {
          const opt = document.createElement('option');
          opt.value = file;
          opt.innerText = file;
          select.appendChild(opt);
        });
      }
    }
  } catch (err) {
    console.error('백업 목록 로드 실패', err);
  }
}

async function restoreBackup() {
  const filename = document.getElementById('backup-select').value;
  if (!filename) {
    alert('복원할 백업 파일을 선택해주세요.');
    return;
  }

  if (!confirm(`[${filename}] 백업 데이터로 복원하시겠습니까?`)) return;

  try {
    const response = await fetch('/api/config/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: filename }),
    });
    const result = await response.json();

    if (result.status === 'success') {
      populateUI(result.config);
      showMessage(result.message);
      addLog(`[${filename}] 복원 완료`, 'info');
      loadBackupList();
    } else {
      showMessage(result.message, true);
      addLog(`복원 실패: ${result.message}`, 'error');
    }
  } catch (err) {
    showMessage('복원 실행 중 오류가 발생했습니다.', true);
    addLog('복원 실행 중 오류 발생', 'error');
  }
}

function openTab(evt, tabId) {
  const tabContents = document.getElementsByClassName('tab-content');
  for (let i = 0; i < tabContents.length; i++) {
    tabContents[i].classList.remove('active');
  }

  const tabBtns = document.getElementsByClassName('tab-btn');
  for (let i = 0; i < tabBtns.length; i++) {
    tabBtns[i].classList.remove('active');
  }

  document.getElementById(tabId).classList.add('active');
  evt.currentTarget.classList.add('active');
}

// 로그창 전체화면(모달) 토글
function toggleFullscreenLog() {
  const logSection = document.querySelector('.log-section');
  const toggleBtn = document.getElementById('toggle-log-btn');

  logSection.classList.toggle('maximized');

  if (logSection.classList.contains('maximized')) {
    toggleBtn.innerText = '닫기 (원래대로)';
    document.body.style.overflow = 'hidden';
  } else {
    toggleBtn.innerText = '전체화면';
    document.body.style.overflow = '';
  }

  const logWindow = document.getElementById('log-window');
  if (logWindow) {
    logWindow.scrollTop = logWindow.scrollHeight;
  }
}

function addLog(msg, type = 'info') {
  const logWindow = document.getElementById('log-window');
  if (!logWindow) return;

  const timeStr = new Date().toLocaleTimeString();
  const div = document.createElement('div');
  div.className = `log-entry ${type}`;
  div.innerText = `[${timeStr}] ${msg}`;

  logWindow.appendChild(div);
  logWindow.scrollTop = logWindow.scrollHeight;
}

function clearLog() {
  const logWindow = document.getElementById('log-window');
  if (logWindow) {
    logWindow.innerHTML = '';
    addLog('로그 창이 초기화되었습니다.', 'system');
  }
}

async function startMacro() {
  try {
    addLog('매크로 시작 요청 전송...', 'system');
    const response = await fetch('/api/run-action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'start' }),
    });
    const result = await response.json();

    if (result.status === 'success') {
      setMacroState(true);
      addLog('매크로가 성공적으로 시작되었습니다.', 'info');
    } else {
      addLog(`매크로 시작 실패: ${result.message}`, 'error');
    }
  } catch (err) {
    addLog('매크로 시작 중 서버 통신 오류 발생', 'error');
  }
}

async function stopMacro() {
  try {
    addLog('매크로 중지 요청 전송...', 'system');
    const response = await fetch('/api/run-action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'stop' }),
    });
    const result = await response.json();

    if (result.status === 'success') {
      setMacroState(false);
      addLog('매크로가 중지되었습니다.', 'info');
    } else {
      addLog(`매크로 중지 실패: ${result.message}`, 'error');
    }
  } catch (err) {
    addLog('매크로 중지 중 서버 통신 오류 발생', 'error');
  }
}

function setMacroState(isRunning) {
  const btnStart = document.getElementById('btn-start');
  const btnStop = document.getElementById('btn-stop');
  const statusText = document.getElementById('macro-status-text');

  if (isRunning) {
    if (btnStart) btnStart.disabled = true;
    if (btnStop) btnStop.disabled = false;
    if (statusText) {
      statusText.innerText = '실행 중...';
      statusText.className = 'status-running';
    }
  } else {
    if (btnStart) btnStart.disabled = false;
    if (btnStop) btnStop.disabled = true;
    if (statusText) {
      statusText.innerText = '중지됨';
      statusText.className = 'status-stopped';
    }
  }
}

// ADB 기기 목록 조회 및 드롭다운 채우기
async function fetchAdbDevices(targetDev = null) {
  const select = document.getElementById('adb_device');
  const btn = document.querySelector('.btn-refresh-device');
  if (!select) return;

  const currentVal =
    targetDev !== null ? targetDev : select.value || currentTargetDevice;

  if (btn) btn.innerText = '⏳ 검색 중...';

  try {
    const response = await fetch('/api/adb/devices');
    const result = await response.json();
    if (result.status === 'success') {
      const devices = result.devices || [];
      select.innerHTML = '';

      // 기본 옵션: 앱플레이어 기본값 (127.0.0.1:포트)
      const defaultOpt = document.createElement('option');
      defaultOpt.value = '';
      defaultOpt.innerText = '💻 앱플레이어 기본 (127.0.0.1:포트번호)';
      select.appendChild(defaultOpt);

      devices.forEach((dev) => {
        const opt = document.createElement('option');
        opt.value = dev.id;
        let icon = dev.type === 'usb' ? '📱' : '🌐';
        let stateText = '';
        if (dev.state === 'unauthorized') stateText = ' [⚠️승인필요]';
        else if (dev.state === 'offline') stateText = ' [⚠️오프라인]';

        opt.innerText = `${icon} [${dev.type.toUpperCase()}] ${dev.model} (${dev.id})${stateText}`;
        select.appendChild(opt);
      });

      // 기존에 설정된 값이 목록에 없으면 추가
      if (
        currentVal &&
        !Array.from(select.options).some((o) => o.value === currentVal)
      ) {
        const customOpt = document.createElement('option');
        customOpt.value = currentVal;
        customOpt.innerText = `⚙️ ${currentVal}`;
        select.appendChild(customOpt);
      }

      select.value = currentVal;
      currentTargetDevice = currentVal;
      await checkDeviceResolution();
    }
  } catch (err) {
    console.error('ADB 기기 목록 조회 실패:', err);
  } finally {
    if (btn) btn.innerText = '🔄 기기 검색';
  }
}

// 기기 변경 이벤트
async function onAdbDeviceChanged() {
  const select = document.getElementById('adb_device');
  if (!select) return;
  currentTargetDevice = select.value;

  // 포트번호 자동 추출 (127.0.0.1:5555 형태인 경우)
  if (currentTargetDevice.includes(':')) {
    const parts = currentTargetDevice.split(':');
    const port = Number(parts[1]);
    if (port && document.getElementById('adb_port')) {
      document.getElementById('adb_port').value = port;
    }
  }

  await checkDeviceResolution();
}

// 선택된 기기의 해상도 상태 조회
async function checkDeviceResolution() {
  const badge = document.getElementById('device-res-badge');
  if (!badge) return;

  try {
    const response = await fetch('/api/adb/device-resolution');
    const result = await response.json();
    if (result.status === 'success') {
      const p = result.physical_size || '미확인';
      const o = result.override_size;
      if (o) {
        badge.innerText = `✨ ${o} (16:9 맞춤 중 / 원래: ${p})`;
        badge.classList.add('active-fhd');
      } else {
        badge.innerText = `해상도: ${p} (기본값)`;
        badge.classList.remove('active-fhd');
      }
    } else {
      badge.innerText = '기기 미연결';
      badge.classList.remove('active-fhd');
    }
  } catch (err) {
    badge.innerText = '해상도 확인 불가';
    badge.classList.remove('active-fhd');
  }
}

// 16:9 FHD(1080x1920)로 수동 맞춤
async function setDeviceResolutionFHD() {
  try {
    addLog(
      '📱 기기 화면 해상도를 16:9 FHD(1080x1920)로 맞추는 중...',
      'system',
    );
    const response = await fetch('/api/adb/device-resolution/fhd', {
      method: 'POST',
    });
    const result = await response.json();
    showMessage(result.message, result.status !== 'success');
    addLog(result.message, result.status === 'success' ? 'info' : 'error');
    await checkDeviceResolution();
  } catch (err) {
    showMessage('해상도 변경 실패', true);
    addLog('해상도 변경 중 통신 에러 발생', 'error');
  }
}

// 원래 해상도로 수동 복구
async function resetDeviceResolution() {
  try {
    addLog('📱 기기 화면 해상도를 원래대로 복원하는 중...', 'system');
    const response = await fetch('/api/adb/device-resolution/reset', {
      method: 'POST',
    });
    const result = await response.json();
    showMessage(result.message, result.status !== 'success');
    addLog(result.message, result.status === 'success' ? 'info' : 'error');
    await checkDeviceResolution();
  } catch (err) {
    showMessage('해상도 복구 실패', true);
    addLog('해상도 복구 중 통신 에러 발생', 'error');
  }
}

async function connectADB() {
  try {
    addLog('ADB 연결 시도 중...', 'system');
    const response = await fetch('/api/adb/connect', { method: 'POST' });
    const result = await response.json();
    showMessage(result.message, result.status !== 'success');
    addLog(result.message, result.status === 'success' ? 'info' : 'error');
  } catch (err) {
    showMessage('ADB 연결 실패', true);
    addLog('ADB 연결 서버 통신 에러', 'error');
  }
}

async function sendADBClick() {
  const x = document.getElementById('click_x').value;
  const y = document.getElementById('click_y').value;

  if (!x || !y) {
    alert('X 좌표와 Y 좌표를 모두 입력해 주세요.');
    return;
  }

  try {
    const response = await fetch('/api/adb/click', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ x: Number(x), y: Number(y) }),
    });
    const result = await response.json();
    showMessage(result.message, result.status !== 'success');
    addLog(result.message, result.status === 'success' ? 'info' : 'error');
  } catch (err) {
    showMessage('클릭 명령 전송 중 오류가 발생했습니다.', true);
    addLog('클릭 명령 전송 실패', 'error');
  }
}

async function disconnectADB() {
  try {
    addLog('ADB 연결 해제 시도 중...', 'system');
    const response = await fetch('/api/adb/disconnect', { method: 'POST' });
    const result = await response.json();
    showMessage(result.message, result.status !== 'success');
    addLog(result.message, result.status === 'success' ? 'info' : 'error');
  } catch (err) {
    showMessage('ADB 연결 해제 실패', true);
    addLog('ADB 연결 해제 통신 에러', 'error');
  }
}

async function testDiscordMessage() {
  const botToken = document.getElementById('discord_bot_token')?.value?.trim();
  const channelId = document
    .getElementById('discord_channel_id')
    ?.value?.trim();
  const userId = document.getElementById('discord_user_id')?.value?.trim();

  if (!botToken) {
    alert('디스코드 봇 토큰(Bot Token)을 먼저 입력해 주세요.');
    return;
  }
  if (!channelId) {
    alert('전송할 디스코드 채널 ID(Channel ID)를 먼저 입력해 주세요.');
    return;
  }

  try {
    addLog('디스코드 테스트 메시지 전송 요청 중...', 'system');
    const response = await fetch('/api/discord/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        bot_token: botToken,
        channel_id: channelId,
        user_id: userId,
      }),
    });
    const result = await response.json();
    showMessage(result.message, result.status !== 'success');
    addLog(result.message, result.status === 'success' ? 'info' : 'error');
  } catch (err) {
    showMessage('디스코드 테스트 중 통신 오류 발생', true);
    addLog('디스코드 테스트 통신 오류', 'error');
  }
}

function showMessage(msg, isError = false) {
  const el = document.getElementById('status-message');
  if (!el) return;
  el.innerText = msg;
  el.style.display = 'block';
  el.style.backgroundColor = isError ? '#f8d7da' : '#d4edda';
  el.style.color = isError ? '#721c24' : '#155724';
  setTimeout(() => {
    el.style.display = 'none';
  }, 3000);
}

function renderDbTables() {
  const resTbody = document.querySelector('#table-resolutions tbody');
  if (resTbody && Array.isArray(dbData.resolutions)) {
    resTbody.innerHTML = '';
    dbData.resolutions.forEach((r) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
                <td>${r.width}</td><td>${r.height}</td><td>${r.dpi}</td>
                <td><strong>${r.name}</strong></td>
                <td><code>${r.gacha_btn_pos || '-'}</code></td>
                <td><code>${r.skip_btn_pos || '-'}</code></td>
                <td><code>${r.confirm_btn_pos || '-'}</code></td>
                <td><code>${r.result_roi || '-'}</code></td>
                <td><code>${r.screenshot_roi || '-'}</code></td>
                <td>
                  <div style="display: flex; gap: 5px;">
                    <button type="button" class="secondary-btn" style="padding: 4px 8px; font-size: 0.8rem;" onclick='editResolution(${JSON.stringify(r)})'>수정</button>
                    <button type="button" class="danger-btn" onclick="deleteResolutionRecord(${r.width}, ${r.height}, ${r.dpi})">삭제</button>
                  </div>
                </td>
            `;
      resTbody.appendChild(tr);
    });
  }

  const costumeTbody = document.querySelector('#table-costumes tbody');
  if (costumeTbody && Array.isArray(dbData.costumes)) {
    costumeTbody.innerHTML = '';
    dbData.costumes.forEach((c) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
                <td><code>${c.id || '-'}</code></td>
                <td><strong>${c.costume_name}</strong></td>
                <td>${c.rarity}★</td>
                <td><code>${c.image_filename || '-'}</code></td>
                <td>
                  <div style="display: flex; gap: 5px;">
                    <button type="button" class="secondary-btn" style="padding: 4px 8px; font-size: 0.8rem;" onclick='editCostume(${JSON.stringify(c)})'>수정</button>
                    <button type="button" class="danger-btn" onclick="deleteCostumeRecord('${c.costume_name}')">삭제</button>
                  </div>
                </td>
            `;
      costumeTbody.appendChild(tr);
    });
  }
}

function editResolution(r) {
  document.getElementById('add_res_w').value = r.width;
  document.getElementById('add_res_h').value = r.height;
  document.getElementById('add_res_dpi').value = r.dpi;
  document.getElementById('add_res_name').value = r.name;
  document.getElementById('add_res_gacha').value = r.gacha_btn_pos || '';
  document.getElementById('add_res_skip').value = r.skip_btn_pos || '';
  document.getElementById('add_res_confirm').value = r.confirm_btn_pos || '';
  document.getElementById('add_res_roi').value = r.result_roi || '';
  document.getElementById('add_res_ss_roi').value = r.screenshot_roi || '';

  document.getElementById('res-form-title').innerText =
    `해상도 수정 (${r.name})`;
  document.getElementById('res-submit-btn').innerText = '해상도 수정 저장';
  document.getElementById('res-cancel-btn').style.display = 'inline-block';
  document
    .getElementById('res-form-title')
    .scrollIntoView({ behavior: 'smooth' });
}

function resetResolutionForm() {
  document.getElementById('add_res_w').value = '';
  document.getElementById('add_res_h').value = '';
  document.getElementById('add_res_dpi').value = '';
  document.getElementById('add_res_name').value = '';
  document.getElementById('add_res_gacha').value = '';
  document.getElementById('add_res_skip').value = '';
  document.getElementById('add_res_confirm').value = '';
  document.getElementById('add_res_roi').value = '';
  document.getElementById('add_res_ss_roi').value = '';

  document.getElementById('res-form-title').innerText = '신규 해상도 추가';
  document.getElementById('res-submit-btn').innerText = '+ 해상도 추가';
  document.getElementById('res-cancel-btn').style.display = 'none';
}

async function saveResolutionRecord() {
  const payload = {
    width: Number(document.getElementById('add_res_w').value),
    height: Number(document.getElementById('add_res_h').value),
    dpi: Number(document.getElementById('add_res_dpi').value),
    name: document.getElementById('add_res_name').value,
    gacha_btn_pos: document.getElementById('add_res_gacha').value,
    skip_btn_pos: document.getElementById('add_res_skip').value,
    confirm_btn_pos: document.getElementById('add_res_confirm').value,
    result_roi: document.getElementById('add_res_roi').value,
    screenshot_roi: document.getElementById('add_res_ss_roi').value,
  };
  try {
    const res = await fetch('/api/db/resolutions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const result = await res.json();
    if (result.status === 'success') {
      await fetchDbData();
      resetResolutionForm();
      addLog(result.message, 'info');
    } else {
      alert(`저장 실패: ${result.message}`);
    }
  } catch (err) {
    addLog('해상도 저장 중 오류 발생', 'error');
  }
}

async function deleteResolutionRecord(w, h, dpi) {
  if (!confirm(`${w}x${h} (${dpi}DPI) 해상도를 삭제하시겠습니까?`)) return;
  const res = await fetch('/api/db/resolutions/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ width: w, height: h, dpi: dpi }),
  });
  const result = await res.json();
  if (result.status === 'success') {
    await fetchDbData();
    addLog('해상도 데이터가 삭제되었습니다.', 'system');
  }
}

function editCostume(c) {
  document.getElementById('edit_costume_id').value = c.id;
  document.getElementById('add_costume_name').value = c.costume_name;
  document.getElementById('add_costume_rarity').value = c.rarity;
  document.getElementById('add_costume_img').value = c.image_filename || '';

  document.getElementById('costume-form-title').innerText =
    `코스튬 수정 (ID: ${c.id})`;
  document.getElementById('costume-submit-btn').innerText = '코스튬 수정 저장';
  document.getElementById('costume-cancel-btn').style.display = 'inline-block';
  document
    .getElementById('costume-form-title')
    .scrollIntoView({ behavior: 'smooth' });
}

function resetCostumeForm() {
  document.getElementById('edit_costume_id').value = '';
  document.getElementById('add_costume_name').value = '';
  document.getElementById('add_costume_rarity').value = 5;
  document.getElementById('add_costume_img').value = '';

  document.getElementById('costume-form-title').innerText = '신규 코스튬 추가';
  document.getElementById('costume-submit-btn').innerText = '+ 코스튬 추가';
  document.getElementById('costume-cancel-btn').style.display = 'none';
}

async function saveCostumeRecord() {
  const costumeId = document.getElementById('edit_costume_id').value;
  const payload = {
    id: costumeId ? Number(costumeId) : null,
    costume_name: document.getElementById('add_costume_name').value,
    rarity: Number(document.getElementById('add_costume_rarity').value),
    image_filename: document.getElementById('add_costume_img').value,
  };

  try {
    const res = await fetch('/api/db/costumes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const result = await res.json();
    if (result.status === 'success') {
      await fetchDbData();
      resetCostumeForm();
      addLog(result.message, 'info');
    } else {
      alert(`저장 실패: ${result.message}`);
    }
  } catch (err) {
    addLog('코스튬 저장 중 오류 발생', 'error');
  }
}

async function deleteCostumeRecord(costumeName) {
  if (!confirm(`[${costumeName}] 코스튬을 삭제하시겠습니까?`)) return;
  const res = await fetch('/api/db/costumes/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ costume_name: costumeName }),
  });
  const result = await res.json();
  if (result.status === 'success') {
    await fetchDbData();
    addLog('코스튬 데이터가 삭제되었습니다.', 'system');
  }
}

async function loadCorrections() {
  try {
    const response = await fetch('/api/db/corrections');
    const result = await response.json();
    if (result.status === 'success') {
      renderCorrectionTable('char', result.char_corrections);
      renderCorrectionTable('costume', result.costume_corrections);
    }
  } catch (err) {
    console.error('보정 목록 로드 실패', err);
  }
}

function renderCorrectionTable(type, dataList) {
  const tbody = document.querySelector(`#table-${type}-corrections tbody`);
  if (!tbody) return;
  tbody.innerHTML = '';
  dataList.forEach((item) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><code>${item.wrong_text}</code></td>
      <td><strong>${item.correct_text}</strong></td>
      <td><button type="button" class="danger-btn" onclick="deleteCorrection('${type}', '${item.wrong_text}')">삭제</button></td>
    `;
    tbody.appendChild(tr);
  });
}

async function addCorrection(type) {
  const wrongText = document.getElementById(`${type}-wrong`).value;
  const correctText = document.getElementById(`${type}-correct`).value;
  if (!wrongText || !correctText) return;
  try {
    const res = await fetch('/api/db/corrections', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        type: type,
        wrong_text: wrongText,
        correct_text: correctText,
      }),
    });
    const result = await res.json();
    if (result.status === 'success') {
      document.getElementById(`${type}-wrong`).value = '';
      document.getElementById(`${type}-correct`).value = '';
      await loadCorrections();
      addLog(result.message, 'info');
    } else {
      alert(`추가 실패: ${result.message}`);
    }
  } catch (err) {
    addLog('보정 단어 추가 중 오류 발생', 'error');
  }
}

async function deleteCorrection(type, wrongText) {
  if (!confirm(`'${wrongText}' 보정 규칙을 삭제하시겠습니까?`)) return;
  try {
    const res = await fetch('/api/db/corrections/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: type, wrong_text: wrongText }),
    });
    const result = await res.json();
    if (result.status === 'success') {
      await loadCorrections();
      addLog(result.message, 'system');
    }
  } catch (err) {
    addLog('보정 단어 삭제 중 오류 발생', 'error');
  }
}

async function loadUnrecognizedImages() {
  const container = document.getElementById('unrecognized-container');
  if (!container) return;

  try {
    const res = await fetch('/api/unrecognized/list');
    const data = await res.json();

    if (data.status === 'success') {
      if (data.files.length === 0) {
        container.innerHTML =
          '<p style="color: #777;">미인식된 코스튬 이미지가 없습니다.</p>';
        return; // 파일이 없을 때는 굳이 로그를 안 띄워도 깔끔합니다.
      }

      container.innerHTML = '';
      data.files.forEach((filename) => {
        const itemDiv = document.createElement('div');
        itemDiv.className = 'costume-item';
        itemDiv.style.display = 'flex';
        itemDiv.style.alignItems = 'center';
        itemDiv.style.gap = '15px';

        itemDiv.innerHTML = `
          <img src="/static/unrecognized/${filename}" alt="미인식 이미지" style="width: 70px; height: 140px; object-fit: contain; background: #000; border-radius: 4px;">
          <div style="flex: 1; display: flex; gap: 10px; align-items: center;">
            <input type="text" id="char_${filename}" placeholder="캐릭터명 (예: 라텔)" style="flex: 1; padding: 8px;">
            <input type="text" id="costume_name_${filename}" placeholder="코스튬명 (예: 풀_파티)" style="flex: 1; padding: 8px;">
            <select id="rarity_${filename}" style="width: 80px; padding: 8px;">
              <option value="5">5성</option>
              <option value="4">4성</option>
              <option value="3">3성</option>
            </select>
            <div style="display: flex; gap: 5px;">
              <button type="button" class="btn-db-add" onclick="registerUnrecognized('${filename}')">DB 등록</button>
              <button type="button" class="danger-btn" style="padding: 0 15px;" onclick="deleteUnrecognized('${filename}')">삭제</button>
            </div>
          </div>
        `;
        container.appendChild(itemDiv);
      });
      addLog('미인식 이미지 목록을 새로고침했습니다.', 'info');
    }
  } catch (err) {
    console.error('미인식 이미지 목록 로드 실패', err);
    addLog('미인식 이미지 목록 새로고침 중 오류 발생', 'error');
  }
}

// 미인식 이미지 파일 삭제 함수
async function deleteUnrecognized(filename) {
  if (!confirm(`해당 미인식 이미지를 삭제하시겠습니까?\n(${filename})`)) return;

  try {
    const res = await fetch('/api/unrecognized/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: filename }),
    });
    const result = await res.json();

    if (result.status === 'success') {
      addLog(`미인식 파일 삭제 완료: ${filename}`, 'system');
      await loadUnrecognizedImages(); // 목록 새로고침
    } else {
      alert(`삭제 실패: ${result.message}`);
    }
  } catch (err) {
    alert('서버 통신 중 오류가 발생했습니다.');
  }
}

async function registerUnrecognized(filename) {
  const charNameInput = document
    .getElementById(`char_${filename}`)
    .value.trim();
  const costumeNameInput = document
    .getElementById(`costume_name_${filename}`)
    .value.trim();
  const rarityInput = document.getElementById(`rarity_${filename}`).value;

  if (!charNameInput || !costumeNameInput) {
    alert('캐릭터명과 코스튬명을 모두 입력해주세요.');
    return;
  }

  const payload = {
    filename: filename,
    char_name: charNameInput,
    costume_name: costumeNameInput,
    rarity: Number(rarityInput),
  };

  try {
    const res = await fetch('/api/unrecognized/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const result = await res.json();

    if (result.status === 'success') {
      alert(result.message);
      addLog(result.message, 'info');
      await loadUnrecognizedImages();
      await fetchDbData();
    } else {
      alert(`등록 실패: ${result.message}`);
    }
  } catch (err) {
    alert('서버 통신 중 오류가 발생했습니다.');
  }
}

// =========================================================
// 5성 OCR 테스트 로그 모달 관리
// =========================================================
function openOcrLogModal() {
  const modal = document.getElementById('ocr-log-modal');
  if (modal) {
    modal.style.display = 'flex';
    loadOcrTestLog();
  }
}

function closeOcrLogModal() {
  const modal = document.getElementById('ocr-log-modal');
  if (modal) {
    modal.style.display = 'none';
  }
}

async function loadOcrTestLog() {
  const viewer = document.getElementById('ocr-log-viewer');
  const info = document.getElementById('ocr-log-info');
  if (!viewer) return;

  viewer.textContent = '로그를 불러오는 중...';
  try {
    const res = await fetch('/api/ocr-test-log?lines=300');
    const data = await res.json();
    if (data.status === 'success') {
      viewer.textContent = data.content || '(로그 내용이 비어 있습니다)';
      if (info) {
        info.textContent = data.exists
          ? `총 ${data.total_lines}줄 (최근 최대 300줄 표시)`
          : '기록된 로그 파일 없음';
      }
      viewer.scrollTop = viewer.scrollHeight;
    } else {
      viewer.textContent = `로그 로드 실패: ${data.message}`;
    }
  } catch (err) {
    viewer.textContent = '로그 서버와 통신할 수 없습니다.';
  }
}

async function clearOcrTestLog() {
  if (!confirm('정말로 ocr_test.log 파일의 모든 내용을 비우시겠습니까?')) {
    return;
  }
  try {
    const res = await fetch('/api/ocr-test-log/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    const data = await res.json();
    if (data.status === 'success') {
      alert(data.message);
      loadOcrTestLog();
    } else {
      alert(`초기화 실패: ${data.message}`);
    }
  } catch (err) {
    alert('서버 통신 중 오류가 발생했습니다.');
  }
}
