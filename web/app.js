const tokenInput = document.querySelector('#token');
const tokenStatus = document.querySelector('#token-status');
const list = document.querySelector('#incident-list');
const message = document.querySelector('#message');
let token = localStorage.getItem('opsbridge-token') || '';
tokenInput.value = token;

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}
function authHeaders() { return { Authorization: `Bearer ${token}` }; }
function displayTime(value) {
  if (!value) return '—';
  return new Intl.DateTimeFormat('pt-BR', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(value));
}
async function checkHealth() {
  const label = document.querySelector('#health-label');
  const dot = document.querySelector('.dot');
  try {
    const response = await fetch('/health');
    if (!response.ok) throw new Error();
    label.textContent = 'API disponível'; dot.style.background = '#64d9a5'; dot.style.boxShadow = '0 0 12px #64d9a555';
  } catch {
    label.textContent = 'API indisponível'; dot.style.background = '#f07882';
  }
}
async function loadIncidents() {
  if (!token) { message.textContent = 'Informe o token para carregar os incidentes.'; list.innerHTML = ''; return; }
  message.textContent = 'Carregando incidentes…';
  try {
    const status = document.querySelector('#status-filter').value;
    const response = await fetch(`/api/incidents?status=${status}`, { headers: authHeaders() });
    if (!response.ok) throw new Error(response.status === 401 ? 'Token inválido.' : `Falha ao carregar (${response.status}).`);
    const data = await response.json();
    const incidents = data.incidents || [];
    document.querySelector('#open-count').textContent = incidents.filter((x) => x.status === 'open').length;
    document.querySelector('#critical-count').textContent = incidents.filter((x) => x.status === 'open' && x.severity === 'critical').length;
    document.querySelector('#resolved-count').textContent = incidents.filter((x) => x.status === 'resolved').length;
    message.textContent = incidents.length ? `${incidents.length} incidente(s) carregado(s).` : '';
    list.innerHTML = incidents.length ? incidents.map((incident) => `
      <article class="incident">
        <span class="severity ${escapeHtml(incident.severity)}">${escapeHtml(incident.severity)}</span>
        <div class="incident-main"><h3>${escapeHtml(incident.alert_name)} <span class="badge ${escapeHtml(incident.status)}">${incident.status === 'open' ? 'aberto' : 'resolvido'}</span></h3>
          <p>${escapeHtml(incident.summary)}</p><p class="incident-meta">${escapeHtml(incident.source)} · ${escapeHtml(incident.instance)} · visto ${displayTime(incident.last_seen)}</p></div>
        <div class="actions">${incident.status === 'open' ? `<button class="resolve" data-id="${incident.id}">Resolver</button>` : ''}</div>
      </article>`).join('') : '<div class="empty">Nenhum incidente neste filtro. Envie um alerta de demonstração para começar.</div>';
    list.querySelectorAll('.resolve').forEach((button) => button.addEventListener('click', () => resolveIncident(button.dataset.id)));
  } catch (error) {
    message.textContent = error.message;
    list.innerHTML = '';
  }
}
async function resolveIncident(id) {
  const response = await fetch(`/api/incidents/${id}/resolve`, { method: 'PATCH', headers: authHeaders() });
  if (!response.ok) { message.textContent = `Não foi possível resolver (${response.status}).`; return; }
  await loadIncidents();
}
document.querySelector('#save-token').addEventListener('click', () => {
  token = tokenInput.value.trim();
  localStorage.setItem('opsbridge-token', token);
  tokenStatus.textContent = token ? 'Token salvo neste navegador.' : 'Informe um token para consultar a API.';
  loadIncidents();
});
document.querySelector('#refresh').addEventListener('click', loadIncidents);
document.querySelector('#status-filter').addEventListener('change', loadIncidents);
checkHealth(); loadIncidents();
