const $ = id => document.getElementById(id);
document.body.classList.toggle('light-mode', localStorage.getItem('v3-theme') === 'light');
fetch('/api/v3/auth/setup-status', {credentials:'same-origin'})
  .then(response => response.ok ? response.json() : Promise.reject(Error('Setup status unavailable')))
  .then(status => { if (status.initialized) location.replace('/app'); })
  .catch(error => { $('error-msg').textContent = error.message; $('error-msg').style.display = 'block'; });
$('setup-form').addEventListener('submit', async event => {
  event.preventDefault();
  const username = $('username').value.trim() || 'admin';
  const password = $('password').value;
  const error = $('error-msg');
  error.style.display = 'none';
  if (password.length < 6 || password !== $('confirm-password').value) {
    error.textContent = password.length < 6 ? 'Password must be at least 6 characters.' : 'Passwords do not match.';
    error.style.display = 'block';
    return;
  }
  $('setup-btn').disabled = true;
  $('loading').style.display = 'block';
  try {
    const response = await fetch('/api/v3/auth/setup-admin', {
      method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username,password,display_name:'Admin'})
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(data.error || 'Setup failed.');
    $('success-msg').style.display = 'block';
    location.replace('/app');
  } catch (failure) {
    error.textContent = failure.message;
    error.style.display = 'block';
    $('setup-btn').disabled = false;
    $('loading').style.display = 'none';
  }
});
