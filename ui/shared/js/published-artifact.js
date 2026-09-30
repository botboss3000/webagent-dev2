// Render a durable public file reference. The content broker requires bearer
// authentication, so media is fetched as a Blob instead of using the grant as src.
export function renderPublishedArtifact(payload, grant) {
  if (!payload || typeof payload.artifact_id !== 'string'
      || !['artifact', 'attachment'].includes(payload.source_type)) return null;
  const name = typeof payload.original_name === 'string' && payload.original_name
    ? payload.original_name : 'Generated file';
  const mime = typeof payload.mime_type === 'string' ? payload.mime_type : '';
  const card = document.createElement('div');
  card.className = 'chat-published-artifact';
  const title = document.createElement('strong');
  title.textContent = name;
  card.appendChild(title);
  const preview = ['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif'].includes(mime.toLowerCase())
    ? document.createElement('img')
    : mime.startsWith('audio/') ? document.createElement('audio')
    : mime.startsWith('video/') ? document.createElement('video') : null;
  if (preview) {
    preview.className = 'chat-published-artifact-preview';
    if (preview.tagName !== 'IMG') preview.controls = true;
    else preview.alt = name;
    card.appendChild(preview);
    grant(payload).then(url => { if (card.isConnected && url) preview.src = url; })
      .catch(() => {});
  }
  const open = document.createElement('button');
  open.type = 'button';
  open.textContent = 'Download file';
  open.addEventListener('click', async () => {
    open.disabled = true;
    try {
      const url = await grant(payload);
      if (url) {
        const link = document.createElement('a');
        link.href = url;
        link.download = name;
        link.click();
      }
    } finally { open.disabled = false; }
  });
  card.appendChild(open);
  return card;
}

export async function requestPublishedArtifactGrant(payload, options = {}) {
  const response = await fetch('/api/v1/content/grants', {
    method: 'POST',
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    body: JSON.stringify({
      artifact_id: payload.artifact_id,
      source_type: payload.source_type,
    }),
  });
  if (!response.ok) throw new Error('Generated file is unavailable');
  const url = (await response.json()).url;
  const content = await fetch(url, { headers: options.headers || {} });
  if (!content.ok) throw new Error('Generated file is unavailable');
  const blobUrl = URL.createObjectURL(await content.blob());
  window.addEventListener('pagehide', () => URL.revokeObjectURL(blobUrl), { once: true });
  return blobUrl;
}
