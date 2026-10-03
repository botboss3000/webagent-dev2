// Agent configuration is distinct from capability-level previews; Rust enforces every save.
const node = (tag, cls = '', text = '') => {
  const el = document.createElement(tag); el.className = cls; el.textContent = text; return el;
};
const row = (name, control, hint = '') => {
  const el = node('div', 'ac-ability-row ac-row-static');
  const label = node('div', 'ac-ability-label');
  label.append(node('div', 'ac-ability-name', name), node('div', 'ac-ability-desc', hint));
  el.append(label, control); return el;
};
const field = (name, value = '', type = 'text', maxLength = 2048) => {
  const input = node('input', 'ac-input'); input.type = type; input.value = value;
  input.maxLength = maxLength; input.setAttribute('aria-label', name); return input;
};
const message = error => error?.status === 409
  ? 'Configuration changed. Reload abilities before saving again.'
  : 'Configuration could not be saved. Check the fields and your agent authority.';
const cautions = ['cautious', 'balanced', 'trusted'];
const settingsSpec = {
  terminal_control: ['terminal_caution', 'Terminal caution'],
  codebase_admin: ['destructive_caution', 'Destructive operation caution'],
  git_control: ['notify_repo_changes', 'Notify repository changes'],
};
export function buildAdministratorControls({ ability, data, repository, request, scope,
  levelId, previewUser, status, rerender }) {
  const spec = settingsSpec[ability.id];
  if (!spec || data.authority?.can_manage !== true) return null;
  const panel = node('div', 'ac-list v3-administrator-settings');
  const enabled = !levelId && !previewUser;
  panel.append(node('p', 'ac-hint', enabled
    ? 'Agent settings. Caution never raises access above installation, account tier or capability policy.'
    : 'Agent settings are read-only while previewing a user or capability level.'));
  const [key, title] = spec;
  const current = data.settings?.[ability.id]?.[key];
  let control;
  if (ability.id === 'git_control') {
    control = field(title, '', 'checkbox'); control.checked = current === true;
  } else {
    control = node('select', 'ac-input ac-config-sel'); control.setAttribute('aria-label', title);
    for (const value of cautions) {
      const option = node('option', '', value[0].toUpperCase() + value.slice(1));
      option.value = value; control.append(option);
    }
    control.value = cautions.includes(current) ? current : 'cautious';
  }
  control.disabled = !enabled;
  const save = node('button', 'ac-btn ac-btn-sm', 'Save settings'); save.type = 'button'; save.disabled = !enabled;
  save.onclick = async () => {
    if (!enabled || save.disabled) return;
    save.disabled = true; control.disabled = true; status.textContent = 'Saving agent settings…';
    try {
      await request(scope + '/abilities/' + encodeURIComponent(ability.id) + '/settings', 'PUT',
        { expected_revision: data.revision, settings: { [key]: ability.id === 'git_control' ? control.checked : control.value } });
      await rerender({ administratorNotice: 'Agent settings saved.' });
    } catch (error) {
      status.textContent = message(error); save.disabled = !enabled; control.disabled = !enabled;
    }
  };
  panel.append(row(title, control), row('Save agent configuration', save));
  if (ability.id !== 'git_control') return panel;
  panel.append(node('p', 'ac-hint', 'Repository connections belong to this agent. Only the connection owner can edit it. Tokens are write-only.'));
  if (repository?.load_failed || !repository || repository.agent_id !== data.agent_id) {
    panel.append(node('p', 'ac-hint', 'Repository connections unavailable. Reload abilities to retry.'));
    return panel;
  }
  const connections = Array.isArray(repository.connections) ? repository.connections.slice(0, 8) : [];
  const addConnection = (connection = null) => {
    const form = node('div', 'ac-list');
    const editable = enabled && repository.can_manage === true && (!connection || connection.can_edit === true);
    const id = field('Connection ID', connection?.id || '', 'text', 80);
    id.pattern = '[A-Za-z0-9_-]{1,80}'; id.required = true; id.disabled = !!connection || !editable;
    const url = field('HTTPS repository URL', connection?.url || '', 'url');
    url.required = true; url.placeholder = 'https://example.com/team/repository';
    const username = field('Repository username', connection?.username || 'git', 'text', 128);
    // A private token is never populated from a server response or retained after submission.
    const token = field('New repository token', '', 'password', 512);
    token.autocomplete = 'new-password'; token.placeholder = 'Leave empty to keep the existing token';
    const revoked = field('Revoke connection', '', 'checkbox'); revoked.checked = connection?.revoked === true;
    for (const input of [url, username, token, revoked]) input.disabled = !editable;
    form.append(node('p', 'ac-hint', connection
      ? connection.id + ' · ' + (connection.revoked ? 'Revoked' : connection.configured
        ? connection.authenticated ? 'Authenticated' : 'Public repository' : 'Unconfigured')
      : 'New connection (maximum 8 per agent)'));
    form.append(row('Connection ID', id), row('HTTPS repository URL', url,
      'No embedded credentials, query or fragment.'), row('Username', username),
      row('New token', token, 'Use a repository-scoped token. Changing URL or username clears the saved credential unless replaced.'),
      row('Revoked', revoked));
    const submit = node('button', 'ac-btn ac-btn-sm', connection ? 'Save connection' : 'Add connection');
    submit.type = 'button'; submit.disabled = !editable;
    submit.onclick = async () => {
      if (!editable || submit.disabled) return;
      const secret = token.value; token.value = '';
      let parsed;
      try { parsed = new URL(url.value); } catch { /* Reject locally without reflecting a credential-bearing URL. */ }
      if (!/^[A-Za-z0-9_-]{1,80}$/.test(id.value) || !parsed || parsed.protocol !== 'https:' ||
          parsed.username || parsed.password || parsed.search || parsed.hash || parsed.pathname === '/' ||
          parsed.pathname.includes('%') || url.value.trim() !== url.value ||
          /[\x00-\x20\x7f]/.test(url.value) ||
          !username.value || username.value.length > 128 || /[^\x21-\x7e]|['"\\@:]/.test(username.value) ||
          (secret && (secret.length < 8 || secret.length > 512 || /[^\x21-\x7e]/.test(secret)))) {
        status.textContent = 'Use a connection ID, a clean HTTPS repository URL, a valid username and an 8–512 character token if supplied.';
        return;
      }
      submit.disabled = true;
      for (const input of [id, url, username, token, revoked]) input.disabled = true;
      status.textContent = 'Saving repository connection…';
      try {
        await request(scope + '/repository-connections/' + encodeURIComponent(id.value), 'PUT', {
          expected_revision: repository.revision, url: url.value, username: username.value,
          ...(secret ? { token: secret } : {}), revoked: revoked.checked,
        });
        await rerender({ administratorNotice: 'Repository connection saved.' });
      } catch (error) {
        status.textContent = message(error); submit.disabled = !editable;
        for (const input of [url, username, token, revoked]) input.disabled = !editable;
        id.disabled = !!connection || !editable;
      } finally { token.value = ''; }
    };
    form.append(row(connection ? 'Update connection' : 'Create connection', submit)); panel.append(form);
  };
  for (const connection of connections) addConnection(connection);
  if (connections.length < 8) addConnection();
  else panel.append(node('p', 'ac-hint', 'This agent has reached its 8-connection limit.'));
  return panel;
}
